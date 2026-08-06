"""Tests for a whole review: coverage, verification, scoring and the summary.

The bug that prompted this project was a three-file pull request that reviewed
two files and said nothing about the third. test_coverage_is_reported_honestly
is the regression test for exactly that.
"""

import pytest

from senrew import agent, config, github, llm, severity, tools
from senrew.codebase import LocalCodebase
from senrew.models import Finding, Review

PR = {"number": 7, "title": "Add refund endpoint", "body": "",
      "head": {"sha": "a" * 40, "ref": "feature/refunds"}, "draft": False}

FILES = [
    {"filename": "src/app.py", "status": "modified", "changes": 3, "patch": "@@\n+x = 1\n"},
    {"filename": "src/util.py", "status": "modified", "changes": 2, "patch": "@@\n+y = 2\n"},
    {"filename": "logo.png", "status": "added", "changes": 0},
]


def call(name, **args):
    return {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}


def scripted(monkeypatch, replies):
    """Make llm.chat play a fixed list of replies."""
    queue = list(replies)
    monkeypatch.setattr(
        llm, "chat",
        lambda *a, **k: queue.pop(0) if queue else call("finish", summary=""),
    )


def review(monkeypatch, replies, files=None, codebase=None):
    scripted(monkeypatch, replies)
    return agent.review_pull_request(
        "octocat/demo", PR, codebase, files if files is not None else FILES
    )


# --- coverage: the bug this project exists to fix --------------------------


def test_a_file_never_concluded_on_is_named_as_unreviewed(monkeypatch):
    """Reading a file is not judging it.

    The agent opens src/util.py but never records a finding or an all-clear
    for it. That file has not been reviewed, and the summary must say so
    rather than let it pass as clean.
    """
    result = review(monkeypatch, [
        call("list_changed_files"),
        call("read_diff", path="src/app.py"),
        call("no_issues_in", path="src/app.py", reason="trivial"),
        call("read_diff", path="src/util.py"),
        call("finish", summary="done"),          # refused: util.py unaccounted
        call("finish", summary="done anyway"),   # second attempt always lands
        call("finish", summary="nudge pass"),    # nudge pass, still no verdict
        call("finish", summary="nudge pass"),
    ])

    assert result.files_changed == 3
    assert result.files_missed == ["src/util.py"]
    assert "`src/util.py` - **not reviewed**" in result.summary


def test_a_skipped_file_is_nudged_and_can_be_recovered(monkeypatch):
    """The second pass is what turns a near-miss into full coverage."""
    result = review(monkeypatch, [
        call("list_changed_files"),
        call("read_diff", path="src/app.py"),
        call("no_issues_in", path="src/app.py", reason="trivial"),
        call("finish", summary="done"),          # refused: util.py unaccounted
        call("finish", summary="done"),
        # nudge pass picks it up and concludes on it
        call("read_diff", path="src/util.py"),
        call("no_issues_in", path="src/util.py", reason="just a constant"),
        call("finish", summary="now done"),
    ])

    assert result.files_missed == []
    assert set(result.files_clean) == {"src/app.py", "src/util.py"}
    assert "reviewed, no issues: just a constant" in result.summary


def test_binary_files_are_named_not_hidden(monkeypatch):
    result = review(monkeypatch, [
        call("list_changed_files"),
        call("read_diff", path="src/app.py"),
        call("read_diff", path="src/util.py"),
        call("finish", summary="done"),
    ])

    assert result.files_unreviewable[0]["filename"] == "logo.png"
    assert "logo.png" in result.summary


def test_a_pull_request_with_no_reviewable_code_is_skipped(monkeypatch):
    result = review(monkeypatch, [call("finish", summary="")],
                    files=[{"filename": "logo.png", "status": "added", "changes": 0}])

    assert result.status == "skipped"
    assert "No reviewable code" in result.summary
    assert "logo.png" in result.summary


# --- verification ----------------------------------------------------------


def test_the_verifier_drops_a_finding_it_cannot_support(monkeypatch):
    """The reason the verifier exists, proved end to end.

    Finding ids are generated when the finding is recorded, so a fixed script
    cannot name one. This model reads the id back out of the verifier's task
    text - which is also how a real model gets it.
    """
    import re

    replies = [
        call("list_changed_files"),
        call("read_diff", path="src/app.py"),
        call("read_diff", path="src/util.py"),
        call("record_finding", path="src/app.py", line=1, category="security",
             title="Possible SQL injection", explanation="looks interpolated"),
        call("record_finding", path="src/util.py", line=2, category="bug",
             title="Off by one in the loop bound", explanation="reads one past the end"),
        call("finish", summary="found two"),
    ]

    def model(contents, system="", tool_schemas=None, usage=None):
        if replies:
            return replies.pop(0)

        text = "".join(p["text"] for c in contents for p in c.get("parts") or []
                       if p.get("text"))
        listed = re.findall(r"- \[([0-9a-f]{4,})\] \S+?:\d+ - (.+)", text)
        if not listed:
            return call("finish", summary="nothing to verify")

        for finding_id, title in listed:
            if "SQL" in title:
                return call("reject_finding", finding_id=finding_id,
                            reason="Read the file: the value is whitelisted above.")
        return call("finish", summary="checked")

    monkeypatch.setattr(llm, "chat", model)
    result = agent.review_pull_request("octocat/demo", PR, None, FILES)

    assert result.candidates == 2
    assert result.rejected == 1
    assert [f.title for f in result.findings] == ["Off by one in the loop bound"]
    assert "SQL" not in result.summary


def test_a_rejected_finding_never_reaches_the_comments():
    """Rejection has to remove it from what gets posted, not just label it."""
    run = tools.Run(codebase=None, files=FILES)
    tools.record_finding(run, path="src/app.py", line=1, category="security",
                         title="SQL injection", explanation="looks interpolated")
    tools.reject_finding(run, run.findings[0].finding_id, "whitelisted two lines up")

    kept = [f for f in run.findings if f.verdict != "rejected"]
    review_obj = Review(repository="a/b", pr_number=1, head_sha="x", findings=kept)

    assert kept == []
    assert github.build_comments(review_obj) == []


def test_verification_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "VERIFY", False)

    result = review(monkeypatch, [
        call("list_changed_files"),
        call("read_diff", path="src/app.py"),
        call("read_diff", path="src/util.py"),
        call("record_finding", path="src/app.py", line=1, category="bug",
             title="A real problem", explanation="x"),
        call("finish", summary="done"),
    ])

    assert result.rejected == 0
    assert len(result.findings) == 1


def test_a_refusal_fails_the_review_rather_than_faking_a_clean_one(monkeypatch):
    """Publishing 'no issues' over code that was never read is the worst outcome."""
    def blocked(*a, **k):
        raise llm.ModelBlocked("safety filter")

    monkeypatch.setattr(llm, "chat", blocked)
    result = agent.review_pull_request("octocat/demo", PR, None, FILES)

    assert result.status == "failed"
    assert "refused" in result.error


def test_running_out_of_steps_keeps_the_findings_so_far(monkeypatch):
    """A step limit is a budget, not a failure."""
    monkeypatch.setattr(config, "MAX_STEPS", 3)

    result = review(monkeypatch, [
        call("list_changed_files"),
        call("record_finding", path="src/app.py", line=1, category="bug",
             title="Found before the ceiling", explanation="x"),
        call("read_diff", path="src/app.py"),
        call("read_diff", path="src/util.py"),
    ] + [call("search_repo", query="x") for _ in range(20)])

    assert result.status == "completed"
    assert result.candidates == 1


# --- scoring and trimming --------------------------------------------------


def test_findings_are_scored_in_code_and_sorted_worst_first():
    findings = [
        Finding("src/a.py", 1, "style", "Minor", "x", impact="minor", likelihood="rare"),
        Finding("src/auth/login.py", 2, "security", "Serious", "x",
                impact="severe", likelihood="certain"),
    ]
    scored = severity.score_all(findings)

    assert scored[0].title == "Serious"
    assert scored[0].severity_band == "critical"
    assert scored[0].severity_score > scored[1].severity_score


def test_the_summary_admits_what_it_suppressed():
    review_obj = Review(repository="a/b", pr_number=1, head_sha="x",
                        files_changed=1, files_reviewed=1)
    review_obj.findings = [Finding("a.py", 1, "bug", "One", "x", severity_band="high")]

    summary = agent.build_summary(review_obj, suppressed=4)

    assert "4 lower-scoring finding(s) were not posted" in summary


def test_a_clean_review_lists_the_files_it_cleared():
    """'No issues' is only believable if it says which files it looked at."""
    review_obj = Review(
        repository="a/b", pr_number=1, head_sha="x",
        files_changed=2, files_reviewed=2,
        files_clean={"src/app.py": "adds a constant", "src/util.py": "renames a local"},
    )
    summary = agent.build_summary(review_obj)

    assert "No issues found" in summary
    assert "`src/app.py` - reviewed, no issues: adds a constant" in summary
    assert "`src/util.py` - reviewed, no issues: renames a local" in summary


# --- posting ---------------------------------------------------------------


def test_a_finding_with_no_line_never_becomes_an_inline_comment():
    """GitHub rejects a comment with no position, losing the whole batch."""
    review_obj = Review(repository="a/b", pr_number=1, head_sha="x")
    review_obj.findings = [
        Finding("a.py", 12, "bug", "Positioned", "x"),
        Finding("a.py", 0, "bug", "No position", "x"),
    ]

    comments = github.build_comments(review_obj)

    assert len(comments) == 1
    assert comments[0]["line"] == 12


def test_the_comment_cap_is_applied_when_building_comments():
    review_obj = Review(repository="a/b", pr_number=1, head_sha="x")
    review_obj.findings = [
        Finding("a.py", n, "bug", f"Problem {n}", "x") for n in range(1, 40)
    ]

    assert len(github.build_comments(review_obj)) == config.MAX_COMMENTS_PER_REVIEW


# --- the offline demo path -------------------------------------------------


def test_the_canned_model_completes_a_full_run(monkeypatch):
    """The no-API-key path has to actually work, or the quickstart is a lie."""
    monkeypatch.setattr(config, "USE_FAKE_MODEL", True)
    from senrew import demo

    result = agent.review_pull_request(
        "senrew/demo", demo.PR, LocalCodebase(demo.REPO_DIR), demo.FILES
    )

    assert result.status == "completed"
    assert result.candidates == 2
    assert result.rejected == 1, "the verifier should throw out the SQL false alarm"
    assert len(result.findings) == 1
    assert result.files_reviewed == 2
