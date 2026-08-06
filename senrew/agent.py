"""The agent loop, and the two agents that use it.

The loop is the whole idea, and it is deliberately short:

    ask the model  ->  it calls tools  ->  run them  ->  give back results
                   ->  repeat until it says finish

The reviewer explores the code and records findings. The verifier is then
given those findings and the same reading tools, and told to disprove them.
The verifier is what makes this worth building: it can open the file and see
that a value is checked against a whitelist, instead of guessing.
"""

import re
from typing import Any, Callable

from senrew import config, github, llm, severity, syntax, tools
from senrew.models import Finding, Review
from senrew.prompts import load


class StepLimit(RuntimeError):
    """The agent used its whole step budget without finishing."""


def run_agent(
    system: str,
    task: str,
    tool_names: list[str],
    run: tools.Run,
    usage: llm.Usage,
    max_steps: int = 0,
) -> int:
    """Drive one agent until it calls finish. Returns steps used."""
    limit = max_steps or config.MAX_STEPS
    contents: list[dict[str, Any]] = [{"role": "user", "parts": [{"text": task}]}]
    schemas = tools.schemas(tool_names)

    for step in range(1, limit + 1):
        reply = llm.chat(contents, system, schemas, usage)

        # Appended verbatim. Rebuilding this from extracted text drops the
        # thoughtSignature that thinking models need to keep their reasoning.
        contents.append(reply)

        calls = llm.calls_of(reply)
        if not calls:
            # No tool call means the model is talking rather than working.
            # Treat it as done; whatever it recorded already stands.
            run.summary = run.summary or llm.text_of(reply)
            return step

        results = [
            llm.tool_result(call, tools.dispatch(call, run))
            for call in calls
        ]
        contents.append({"role": "user", "parts": results})

        if run.done:
            return step

    raise StepLimit(f"agent used all {limit} steps without finishing")


def review_pull_request(
    repository: str,
    pr: dict[str, Any],
    codebase: Any,
    files: list[dict],
    on_tool: Callable[[str, dict], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> Review:
    """Review one pull request end to end."""
    note = on_note or (lambda _m: None)
    usage = llm.Usage()

    review = Review(
        repository=repository,
        pr_number=pr["number"],
        head_sha=pr["head"]["sha"],
        files_changed=len(files),
    )
    review.files_unreviewable = [
        {"filename": f.get("filename", "?"),
         "reason": "binary or empty - GitHub sent no diff text"}
        for f in files if not f.get("patch")
    ]

    reviewable = [f["filename"] for f in files if f.get("patch")]
    if not reviewable:
        review.status = "skipped"
        review.summary = _no_code_summary(review)
        return review

    run = tools.Run(codebase=codebase, files=files, on_tool=on_tool)

    # --- pass 0: does it even parse? ---------------------------------------
    # A parser, not a model. Deterministic, free, and it cannot change its
    # mind between runs - which is how an unparseable file went unreported.
    broken = check_syntax(codebase, reviewable, note)
    run.findings.extend(broken)

    # --- pass 1: review ----------------------------------------------------
    try:
        steps = run_agent(
            load("reviewer"),
            _review_task(repository, pr, reviewable, broken),
            tools.REVIEWER_TOOLS, run, usage,
        )
    except StepLimit as exc:
        # Not a failure. Whatever was recorded before the limit still counts,
        # and saying so is more useful than throwing the work away.
        note(f"! {exc} - keeping what it found so far")
        steps = config.MAX_STEPS
    except llm.ModelBlocked as exc:
        review.status = "failed"
        review.error = f"model refused: {exc}"
        review.summary = "SenRew could not review this pull request."
        return review

    # A file with no finding and no explicit all-clear was not concluded on,
    # whatever the agent claims. Opening it is not the same as judging it.
    missed = run.unaccounted()

    # --- pass 1b: one nudge about anything it never concluded on ------------
    if missed:
        note(f"! {len(missed)} changed file(s) not accounted for, asking again")
        run.done = False
        try:
            steps += run_agent(
                load("reviewer"),
                _missed_task(missed), tools.REVIEWER_TOOLS, run, usage,
                max_steps=max(3, config.MAX_STEPS // 2),
            )
        except (StepLimit, llm.ModelBlocked) as exc:
            note(f"! second pass stopped: {exc}")
        missed = run.unaccounted()

    review.candidates = len(run.findings)
    review.files_reviewed = len(run.diffs_read)
    review.files_missed = missed

    # --- pass 2: verify ----------------------------------------------------
    # Parser findings are excluded. There is nothing for a model to confirm
    # about a SyntaxError, and asking spends a step to invite doubt.
    checkable = [f for f in run.findings if not f.deterministic]

    if checkable and config.VERIFY:
        run.done = False
        try:
            steps += run_agent(
                load("verifier"),
                _verify_task(checkable), tools.VERIFIER_TOOLS, run, usage,
            )
        except (StepLimit, llm.ModelBlocked) as exc:
            # Keep the findings but do not claim they were checked.
            note(f"! verification stopped: {exc}")
            for finding in checkable:
                if finding.verdict == "unchecked":
                    finding.verdict_reason = "verification did not complete"

    kept = [f for f in run.findings if f.verdict != "rejected"]
    review.rejected = len(run.findings) - len(kept)

    # --- score and trim ----------------------------------------------------
    scored = severity.score_all(kept)
    above_floor = [f for f in scored if severity.meets_minimum(f, config.MIN_SEVERITY_TO_POST)]
    review.findings = above_floor[: config.MAX_COMMENTS_PER_REVIEW]

    # Every reviewable file has to end up somewhere in the report. A file
    # whose only finding was rejected, or scored below the floor, would
    # otherwise drop out of the coverage list entirely and read as though it
    # had never been part of the pull request.
    review.files_clean = dict(run.cleared)
    published = {f.file_path for f in review.findings}

    for name in reviewable:
        if name in published or name in review.files_clean or name in missed:
            continue
        withdrawn = any(
            f.file_path == name and f.verdict == "rejected" for f in run.findings
        )
        review.files_clean[name] = (
            "raised, then withdrawn after checking the code" if withdrawn
            else "reviewed; nothing above the reporting threshold"
        )

    review.steps = steps
    review.cost_usd = usage.cost_usd
    review.summary = build_summary(review, suppressed=len(above_floor) - len(review.findings))
    return review


# --- the parser pass -------------------------------------------------------


def check_syntax(codebase: Any, reviewable: list[str], note) -> list[Finding]:
    """Findings for files that do not parse. No model involved."""
    found: list[Finding] = []

    for name in reviewable:
        if not syntax.can_check(name):
            continue  # no parser for this language; the agent still reads it

        source = codebase.read_raw(name) if codebase else None
        if source is None:
            continue

        error = syntax.check(name, source)
        if not error:
            continue

        note(f"! {name} does not parse: {error}")
        found.append(Finding(
            file_path=name,
            line=_line_of(error),
            category="bug",
            title=f"{name} does not parse",
            explanation=(
                f"This file is not valid and cannot run or be imported as "
                f"written. A parser reports: {error}"
            ),
            # Certainty is total - this is a parser's verdict, not an opinion.
            impact="major",
            likelihood="certain",
            blast_radius="single_file",
            verdict="confirmed",
            verdict_reason=f"Checked with a parser: {error}",
            deterministic=True,
        ))

    return found


def _line_of(error: str) -> int:
    """Pull the line number out of a checker's message, or 0."""
    match = re.search(r"line (\d+)", error)
    return int(match.group(1)) if match else 0


# --- task prompts ----------------------------------------------------------


def _review_task(repository: str, pr: dict, reviewable: list[str],
                 already_found: list[Finding]) -> str:
    listing = "\n".join(f"  - {name}" for name in reviewable)

    known = ""
    if already_found:
        names = ", ".join(sorted({f.file_path for f in already_found}))
        known = (
            f"\nAlready reported by a parser, do NOT report again: {names} "
            f"do not parse. You may still raise other, separate problems in "
            f"them.\n"
        )

    return (
        f"Review pull request #{pr['number']} in {repository}.\n\n"
        f"Title: {pr.get('title') or '(none)'}\n"
        f"Description:\n<untrusted>\n{(pr.get('body') or '(none)')[:1500]}\n</untrusted>\n\n"
        f"These {len(reviewable)} changed file(s) must EACH be accounted for "
        f"before you finish:\n{listing}\n{known}\n"
        f"Read the diff of every one. Where a diff alone cannot tell you "
        f"whether something is really a problem, use read_file and "
        f"search_repo to check before deciding.\n\n"
        f"Every file above must end with either a record_finding or a "
        f"no_issues_in call. Then call finish."
    )


def _missed_task(missed: list[str]) -> str:
    listing = "\n".join(f"  - {name}" for name in missed)
    return (
        f"You finished without concluding anything about these changed "
        f"file(s):\n{listing}\n\n"
        f"Read each one with read_diff now. For each, either record_finding "
        f"if something is wrong, or no_issues_in saying why it is fine. Then "
        f"call finish."
    )


def _verify_task(findings: list[Finding]) -> str:
    listing = "\n".join(
        f"- [{f.finding_id}] {f.file_path}:{f.line} - {f.title}\n"
        f"    claim: {f.explanation}"
        for f in findings
    )
    return (
        f"Another reviewer produced the {len(findings)} finding(s) below. "
        f"Roughly one in five is wrong - usually because the reviewer only "
        f"saw the diff and missed something nearby that already handles the "
        f"case.\n\n{listing}\n\n"
        f"For each one: open the actual file with read_file and check the "
        f"surrounding code. Does something already handle this? Do the lines "
        f"named really support the claim? Then call confirm_finding or "
        f"reject_finding for every id above, and finish.\n\n"
        f"A check that confirms everything has done nothing."
    )


# --- output ----------------------------------------------------------------


def _no_code_summary(review: Review) -> str:
    listed = "\n".join(
        f"- `{f['filename']}` - {f['reason']}" for f in review.files_unreviewable
    )
    return (
        "## SenRew\n\nNo reviewable code in this pull request.\n\n"
        f"{review.files_changed} file(s) changed, none with readable diff text:\n{listed}\n"
    )


def build_summary(review: Review, suppressed: int = 0) -> str:
    """The markdown that goes at the top of the review."""
    total = len(review.findings)
    counts = review.counts_by_band()

    if total == 0:
        body = ["## SenRew", "", "No issues found worth raising.", ""]
    else:
        body = ["## SenRew", "", f"Found **{total}** issue{'s' if total != 1 else ''}:", ""]
        body += [f"- {band.title()}: {counts[band]}"
                 for band in ("critical", "high", "medium", "low") if counts[band]]
        body.append("")

    if suppressed:
        body += [f"{suppressed} lower-scoring finding(s) were not posted, to keep "
                 f"the review readable.", ""]

    # Coverage, stated per file. A review that quietly skipped a file reads as
    # a review that found less, which is how this whole class of bug stayed
    # invisible - so every changed file is listed with what happened to it.
    with_findings: dict[str, int] = {}
    for finding in review.findings:
        with_findings[finding.file_path] = with_findings.get(finding.file_path, 0) + 1

    body += ["", "**Coverage**", ""]

    for path, count in sorted(with_findings.items()):
        body.append(f"- `{path}` - {count} finding{'s' if count != 1 else ''}")
    for path, reason in sorted(review.files_clean.items()):
        # A file can be both: the agent often marks a file clean after the
        # parser already flagged it. Report the finding, not the all-clear.
        if path in with_findings:
            continue
        body.append(f"- `{path}` - reviewed, no issues"
                    + (f": {reason}" if reason else ""))
    for path in sorted(review.files_missed):
        body.append(f"- `{path}` - **not reviewed**")
    for entry in review.files_unreviewable:
        body.append(f"- `{entry['filename']}` - not reviewable: {entry['reason']}")

    if review.candidates:
        body += ["", f"<sub>{review.candidates} candidate finding(s), "
                     f"{review.rejected} rejected after checking the code. "
                     f"{review.steps} agent steps, ${review.cost_usd:.4f}.</sub>"]

    return "\n".join(body) + "\n"
