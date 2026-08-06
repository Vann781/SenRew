"""Tests for the deterministic syntax check and per-file accounting.

Both exist because of one real complaint: a pull request containing
`test5.py` — a file with `print"hello"` in it, which cannot be parsed at all —
was reviewed and the file was never mentioned. The model had read the diff. It
simply decided, that run, that it was not worth raising.

A parser does not have moods, so the check no longer asks one.
"""

import shutil
from types import SimpleNamespace

import pytest

from senrew import agent, config, llm, severity, syntax, tools
from senrew.models import Finding, Review

# Verbatim from the pull request that prompted this.
TEST5 = 'print"hello"\n\nprint(""hello)\n\nprint("hello");\n\n\nprint("hello")'


# --- the file that started it ----------------------------------------------


def test_the_unparseable_file_is_caught():
    error = syntax.check("test5.py", TEST5)
    assert error is not None
    assert "line 1" in error


def test_valid_python_passes():
    assert syntax.check("ok.py", "def f():\n    return 1\n") is None


def test_a_byte_order_mark_is_not_a_syntax_error():
    """A real pull request had a BOM on a working file.

    Python reads a BOM happily from disk but ast.parse rejects it in a string,
    so not stripping it would report a syntax error on a file that runs.
    """
    assert syntax.check("refund.py", "﻿from flask import request\nx = 1\n") is None


def test_an_empty_file_is_not_a_syntax_error():
    assert syntax.check("empty.py", "") is None
    assert syntax.check("blank.py", "  \n\n  ") is None


# --- other stdlib formats --------------------------------------------------


@pytest.mark.parametrize("name,source", [
    ("a.json", '{"a": 1}'),
    ("a.toml", "a = 1"),
    ("a.xml", "<a><b/></a>"),
])
def test_valid_files_pass(name, source):
    assert syntax.check(name, source) is None


@pytest.mark.parametrize("name,source", [
    ("a.json", '{"a": 1,}'),
    ("a.toml", "a = = 1"),
    ("a.xml", "<a><b></a>"),
])
def test_broken_files_are_caught(name, source):
    assert syntax.check(name, source) is not None


# --- what we cannot check --------------------------------------------------


def test_a_language_with_no_checker_returns_nothing():
    """Reporting 'cannot check' as a finding would flag every .rs file."""
    assert syntax.check("main.rs", "fn main( {") is None
    assert syntax.can_check("main.rs") is False


def test_an_uninstalled_toolchain_is_skipped_not_reported(monkeypatch):
    """A missing compiler says nothing about the file."""
    monkeypatch.setattr(syntax.shutil, "which", lambda _b: None)

    assert syntax.check("broken.js", "function f( {") is None
    assert syntax.can_check("broken.js") is False


def test_a_checker_that_crashes_is_not_a_finding(monkeypatch):
    monkeypatch.setattr(syntax.shutil, "which", lambda _b: "/usr/bin/node")

    def boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(syntax.subprocess, "run", boom)
    assert syntax.check("x.js", "function f( {") is None


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_an_external_checker_reports_a_readable_message():
    """Not 'tmp8446huxk.js:1', which tells the reader nothing."""
    error = syntax.check("broken.js", "function f( {")
    assert error and "Error" in error


# --- severity --------------------------------------------------------------


def test_a_parser_finding_keeps_its_score_in_a_test_file():
    """test5.py would otherwise lose 30% purely for starting with 'test'."""
    broken = severity.score_finding(Finding(
        "test5.py", 1, "bug", "does not parse", "x",
        impact="major", likelihood="certain", blast_radius="single_file",
        deterministic=True,
    ))
    opinion = severity.score_finding(Finding(
        "test5.py", 1, "bug", "a judgement call", "x",
        impact="major", likelihood="certain", blast_radius="single_file",
    ))

    assert broken.severity_score > opinion.severity_score
    assert broken.severity_band == "critical"


def test_ordinary_findings_still_get_the_test_discount():
    normal = severity.compute_score("major", "certain", "bug", "src/app.py")
    in_test = severity.compute_score("major", "certain", "bug", "tests/test_app.py")
    assert in_test < normal


# --- the parser pass inside a review ---------------------------------------


class FakeCodebase:
    def __init__(self, files):
        self.files = files

    def read_raw(self, path):
        return self.files.get(path)


def test_check_syntax_produces_a_confirmed_finding():
    found = agent.check_syntax(
        FakeCodebase({"test5.py": TEST5}), ["test5.py"], lambda _m: None
    )

    assert len(found) == 1
    assert found[0].deterministic is True
    assert found[0].verdict == "confirmed"     # a parser already settled it
    assert found[0].line == 1


def test_check_syntax_ignores_files_it_cannot_read():
    found = agent.check_syntax(
        FakeCodebase({}), ["gone.py"], lambda _m: None
    )
    assert found == []


def test_the_verifier_never_sees_a_parser_finding(monkeypatch):
    """Nothing for a model to confirm about a SyntaxError, and asking costs a step."""
    seen = []

    def model(contents, system="", tool_schemas=None, usage=None):
        seen.append(system)
        return {"role": "model", "parts": [
            {"functionCall": {"name": "finish", "args": {"summary": "x"}}}]}

    monkeypatch.setattr(llm, "chat", model)
    monkeypatch.setattr(config, "VERIFY", True)

    files = [{"filename": "test5.py", "status": "added", "changes": 8,
              "patch": "@@\n+print\"hello\"\n"}]
    pr = {"number": 1, "title": "t", "body": "", "head": {"sha": "a" * 40}}

    result = agent.review_pull_request(
        "o/r", pr, FakeCodebase({"test5.py": TEST5}), files
    )

    assert any("does not parse" in f.title for f in result.findings)
    assert not any("verifier" in (s or "").lower() or "disprove" in (s or "").lower()
                   for s in seen), "the verifier must not run for parser-only findings"


def test_the_syntax_finding_survives_a_model_that_says_nothing(monkeypatch):
    """The whole point: it no longer depends on the model noticing."""
    monkeypatch.setattr(llm, "chat", lambda *a, **k: {"role": "model", "parts": [
        {"functionCall": {"name": "finish", "args": {"summary": "nothing to say"}}}]})

    files = [{"filename": "test5.py", "status": "added", "changes": 8,
              "patch": "@@\n+print\"hello\"\n"}]
    pr = {"number": 1, "title": "t", "body": "", "head": {"sha": "a" * 40}}

    result = agent.review_pull_request(
        "o/r", pr, FakeCodebase({"test5.py": TEST5}), files
    )

    assert [f.severity_band for f in result.findings] == ["critical"]
    assert "test5.py" in result.summary


# --- accounting for every file ---------------------------------------------


@pytest.fixture
def run():
    return tools.Run(codebase=None, files=[
        {"filename": "a.py", "changes": 2, "patch": "@@\n+x = 1\n"},
        {"filename": "b.py", "changes": 2, "patch": "@@\n+y = 2\n"},
        {"filename": "logo.png", "changes": 0},
    ])


def test_finish_refuses_while_a_file_is_unaccounted(run):
    result = tools.finish(run, "done")

    assert run.done is False
    assert "a.py" in result and "b.py" in result


def test_finish_succeeds_on_the_second_attempt(run):
    """It nudges once. It must never deadlock."""
    tools.finish(run, "done")
    tools.finish(run, "done")

    assert run.done is True


def test_finish_is_immediate_once_everything_is_accounted(run):
    tools.record_finding(run, path="a.py", line=1, category="bug",
                         title="A problem", explanation="x")
    tools.no_issues_in(run, path="b.py", reason="just a constant")

    tools.finish(run, "done")

    assert run.done is True
    assert run.unaccounted() == []


def test_a_binary_file_never_counts_as_unaccounted(run):
    """'Every file' cannot include a PNG."""
    tools.no_issues_in(run, path="a.py", reason="fine")
    tools.no_issues_in(run, path="b.py", reason="fine")

    assert run.unaccounted() == []


def test_no_issues_in_rejects_a_file_outside_the_pr(run):
    assert "not part of this pull request" in tools.no_issues_in(
        run, path="elsewhere.py", reason="fine"
    )
    assert run.cleared == {}


def test_reading_a_file_alone_does_not_account_for_it(run):
    """The exact gap this closes."""
    tools.read_diff(run, "a.py")
    assert "a.py" in run.unaccounted()


def test_a_file_whose_finding_was_rejected_still_appears(monkeypatch):
    """It was reviewed. It must not vanish from the coverage list.

    Found in the demo output: the verifier rejected the only finding on
    exporter.py, so the file was neither 'has findings' nor 'cleared' nor
    'missed', and disappeared from the report entirely.
    """
    monkeypatch.setattr(config, "USE_FAKE_MODEL", True)
    from senrew import demo
    from senrew.codebase import LocalCodebase

    result = agent.review_pull_request(
        "senrew/demo", demo.PR, LocalCodebase(demo.REPO_DIR), demo.FILES
    )

    assert "src/reports/exporter.py" in result.summary
    assert "withdrawn after checking" in result.summary


def test_a_file_is_listed_once_even_if_also_marked_clean():
    """The agent often clears a file the parser already flagged."""
    review_obj = Review(
        repository="a/b", pr_number=1, head_sha="x", files_changed=1,
        findings=[Finding("test5.py", 1, "bug", "does not parse", "x",
                          severity_band="critical")],
        files_clean={"test5.py": "already noted as not parsing"},
    )
    summary = agent.build_summary(review_obj)

    assert summary.count("`test5.py`") == 1
    assert "- `test5.py` - 1 finding" in summary
