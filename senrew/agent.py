"""The agent loop, and the two agents that use it.

The loop is the whole idea, and it is deliberately short:

    ask the model  ->  it calls tools  ->  run them  ->  give back results
                   ->  repeat until it says finish

The reviewer explores the code and records findings. The verifier is then
given those findings and the same reading tools, and told to disprove them.
The verifier is what makes this worth building: it can open the file and see
that a value is checked against a whitelist, instead of guessing.
"""

from typing import Any, Callable

from senrew import config, github, llm, severity, tools
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

    # --- pass 1: review ----------------------------------------------------
    try:
        steps = run_agent(
            load("reviewer"),
            _review_task(repository, pr, reviewable),
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

    # A file the agent never opened was not reviewed, whatever it claims.
    missed = [name for name in reviewable if name not in run.diffs_read]

    # --- pass 1b: one nudge about anything it skipped -----------------------
    if missed:
        note(f"! {len(missed)} changed file(s) never opened, asking again")
        run.done = False
        try:
            steps += run_agent(
                load("reviewer"),
                _missed_task(missed), tools.REVIEWER_TOOLS, run, usage,
                max_steps=max(3, config.MAX_STEPS // 2),
            )
        except (StepLimit, llm.ModelBlocked) as exc:
            note(f"! second pass stopped: {exc}")
        missed = [name for name in reviewable if name not in run.diffs_read]

    review.candidates = len(run.findings)
    review.files_reviewed = len(run.diffs_read)
    review.files_missed = missed

    # --- pass 2: verify ----------------------------------------------------
    if run.findings and config.VERIFY:
        run.done = False
        try:
            steps += run_agent(
                load("verifier"),
                _verify_task(run.findings), tools.VERIFIER_TOOLS, run, usage,
            )
        except (StepLimit, llm.ModelBlocked) as exc:
            # Keep the findings but do not claim they were checked.
            note(f"! verification stopped: {exc}")
            for finding in run.findings:
                if finding.verdict == "unchecked":
                    finding.verdict_reason = "verification did not complete"

    kept = [f for f in run.findings if f.verdict != "rejected"]
    review.rejected = len(run.findings) - len(kept)

    # --- score and trim ----------------------------------------------------
    scored = severity.score_all(kept)
    above_floor = [f for f in scored if severity.meets_minimum(f, config.MIN_SEVERITY_TO_POST)]
    review.findings = above_floor[: config.MAX_COMMENTS_PER_REVIEW]

    review.steps = steps
    review.cost_usd = usage.cost_usd
    review.summary = build_summary(review, suppressed=len(above_floor) - len(review.findings))
    return review


# --- task prompts ----------------------------------------------------------


def _review_task(repository: str, pr: dict, reviewable: list[str]) -> str:
    listing = "\n".join(f"  - {name}" for name in reviewable)
    return (
        f"Review pull request #{pr['number']} in {repository}.\n\n"
        f"Title: {pr.get('title') or '(none)'}\n"
        f"Description:\n<untrusted>\n{(pr.get('body') or '(none)')[:1500]}\n</untrusted>\n\n"
        f"These {len(reviewable)} changed file(s) must each be opened with "
        f"read_diff before you finish:\n{listing}\n\n"
        f"Read the diff of every one. Where a diff alone cannot tell you "
        f"whether something is really a problem, use read_file and "
        f"search_repo to check before deciding. Record only what you can "
        f"support, then call finish."
    )


def _missed_task(missed: list[str]) -> str:
    listing = "\n".join(f"  - {name}" for name in missed)
    return (
        f"You finished without opening these changed file(s):\n{listing}\n\n"
        f"Read each one with read_diff now and record anything worth raising. "
        f"If they are all fine, call finish."
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

    # Coverage, always stated. A review that quietly skipped a file reads as a
    # review that found less - which is how the bug that prompted this project
    # stayed invisible.
    body.append(f"**Coverage:** opened {review.files_reviewed} of "
                f"{review.files_changed} changed file(s).")

    if review.files_missed:
        body.append("")
        body.append("Not opened: " + ", ".join(f"`{n}`" for n in review.files_missed))
    if review.files_unreviewable:
        body.append("")
        for entry in review.files_unreviewable:
            body.append(f"Not reviewable: `{entry['filename']}` - {entry['reason']}")

    if review.candidates:
        body += ["", f"<sub>{review.candidates} candidate finding(s), "
                     f"{review.rejected} rejected after checking the code. "
                     f"{review.steps} agent steps, ${review.cost_usd:.4f}.</sub>"]

    return "\n".join(body) + "\n"
