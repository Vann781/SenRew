"""A scripted stand-in for Gemini, so the agent runs with no API key.

This is NOT a simulation of a model. It replays a fixed sequence of tool calls
so you can watch the loop work, and so the tests can drive it without a
network. Every reply is announced as canned by the CLI.

It reads the real file list out of the conversation, so the script still makes
sense against whatever pull request you point it at.
"""

import re
from typing import Any

# Which step of the script we are on is derived from how many times the model
# has already spoken, so no state is carried between calls.


def chat(contents, system="", tools=None, usage=None) -> dict[str, Any]:
    """Return the next scripted reply."""
    names = {t["name"] for t in (tools or [])}
    turn = sum(1 for c in contents if c.get("role") == "model")

    if "reject_finding" in names:
        return _verifier(turn, contents)
    if "record_finding" in names:
        return _reviewer(turn, contents)
    return _say("(canned) no tools available, nothing to do")


# --- building replies ------------------------------------------------------


def _call(name: str, **args) -> dict[str, Any]:
    return {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}


def _calls(*pairs) -> dict[str, Any]:
    """Several tools at once - the parallel-call case the dispatcher handles."""
    return {"role": "model",
            "parts": [{"functionCall": {"name": n, "args": a}} for n, a in pairs]}


def _say(text: str) -> dict[str, Any]:
    return {"role": "model", "parts": [{"text": text}]}


# --- reading the conversation ----------------------------------------------


def _results(contents, tool_name: str) -> list[str]:
    """Every result returned by one tool so far."""
    out = []
    for content in contents:
        for part in content.get("parts") or []:
            response = part.get("functionResponse")
            if response and response.get("name") == tool_name:
                out.append(str(response["response"].get("result", "")))
    return out


def _reviewable(contents) -> list[str]:
    """Changed files that actually have a diff, from list_changed_files."""
    results = _results(contents, "list_changed_files")
    if not results:
        return []

    files = []
    for line in results[0].splitlines():
        if "cannot review" in line:
            continue  # binary or empty; there is nothing to read
        name = line.split(" (")[0].strip()
        if name:
            files.append(name)
    return files


def _prompt_text(contents) -> str:
    return "".join(
        part["text"]
        for content in contents
        for part in content.get("parts") or []
        if part.get("text")
    )


# --- the reviewer script ---------------------------------------------------


def _reviewer(turn: int, contents) -> dict[str, Any]:
    """List the files, read each diff, read one file properly, record, finish."""
    if turn == 0:
        return _call("list_changed_files")

    files = _reviewable(contents)
    if not files:
        return _call("finish", summary="Nothing reviewable in this pull request.")

    # One read_diff per changed file, so the offline run has full coverage.
    if turn <= len(files):
        return _call("read_diff", path=files[turn - 1])

    # The point of the whole project: open a file to see code the diff hides.
    if turn == len(files) + 1:
        return _call("read_file", path=files[-1])

    if turn == len(files) + 2:
        return _calls(*_canned_findings(files))

    # Account for every file the canned findings did not cover, so the offline
    # run satisfies the same completeness rule a real one has to.
    if turn == len(files) + 3:
        covered = {files[0], files[-1]}
        rest = [f for f in files if f not in covered]
        if rest:
            return _calls(*[
                ("no_issues_in", {"path": f, "reason": "canned: nothing to raise"})
                for f in rest
            ])

    return _call("finish", summary="Canned review complete.")


def _canned_findings(files: list[str]) -> list[tuple[str, dict]]:
    """One plausible real bug, and one false alarm for the verifier to reject."""
    findings = [("record_finding", {
        "path": files[0],
        "line": 1,
        "category": "security",
        "title": "Endpoint does not check the record belongs to the caller",
        "explanation": (
            "This finding is canned - it exists so the loop, the verifier and "
            "the severity scoring can be demonstrated without an API key. "
            "Being logged in is checked; owning the record is not."
        ),
        "impact": "major",
        "likelihood": "possible",
        "blast_radius": "module",
    })]

    if len(files) > 1:
        # Deliberately wrong. The verifier reads the file and throws it out,
        # which is the behaviour worth showing.
        findings.append(("record_finding", {
            "path": files[-1],
            "line": 1,
            "category": "security",
            "title": "Possible SQL injection from an interpolated table name",
            "explanation": (
                "This finding is canned AND deliberately wrong. The value is "
                "validated before use, but that check is not visible in the "
                "diff - only in the file."
            ),
            "impact": "severe",
            "likelihood": "likely",
            "blast_radius": "system",
        }))
    return findings


# --- the verifier script ---------------------------------------------------


def _verifier(turn: int, contents) -> dict[str, Any]:
    """Read the code, then confirm or reject each finding.

    Findings arrive as '- [id] path:line - title', so the ids can be recovered
    without any state shared with the reviewer.
    """
    listed = re.findall(r"- \[([0-9a-f]{4,})\] (\S+?):\d+ - (.+)", _prompt_text(contents))

    if not listed:
        return _call("finish", summary="Nothing to verify.")

    if turn == 0:
        # Look at the file behind the most suspicious claim first.
        target = next((p for _i, p, t in listed if re.search(r"sql|inject", t, re.I)),
                      listed[0][1])
        return _call("read_file", path=target)

    if turn == 1:
        verdicts = []
        for finding_id, _path, title in listed:
            if re.search(r"sql|inject", title, re.I):
                verdicts.append(("reject_finding", {
                    "finding_id": finding_id,
                    "reason": "Read the file: the value is checked against a "
                              "hardcoded whitelist a few lines above, so it "
                              "cannot be attacker-controlled. Not reachable.",
                }))
            else:
                verdicts.append(("confirm_finding", {
                    "finding_id": finding_id,
                    "reason": "Read the surrounding lines and the claim holds.",
                }))
        return _calls(*verdicts)

    return _call("finish", summary="Verification complete.")
