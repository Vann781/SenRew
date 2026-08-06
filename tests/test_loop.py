"""Tests for the agent loop and tool dispatch.

The model is scripted. These are about what the loop does with a reply, not
about whether Gemini produces a good one. No network.
"""

import pytest

from senrew import agent, llm, tools
from senrew.models import Finding

FILES = [
    {"filename": "src/app.py", "status": "modified", "changes": 3, "patch": "@@\n+x = 1\n"},
    {"filename": "src/util.py", "status": "modified", "changes": 2, "patch": "@@\n+y = 2\n"},
    {"filename": "logo.png", "status": "added", "changes": 0},
]


class ScriptedModel:
    """Plays a fixed list of replies and remembers what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, contents, system="", tool_schemas=None, usage=None):
        self.seen.append(list(contents))
        if not self.replies:
            return say("out of script")
        return self.replies.pop(0)


def call(name, **args):
    return {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}


def say(text):
    return {"role": "model", "parts": [{"text": text}]}


@pytest.fixture
def run():
    return tools.Run(codebase=None, files=FILES)


def drive(monkeypatch, replies, run, **kwargs):
    """Run the loop against a scripted model."""
    model = ScriptedModel(replies)
    monkeypatch.setattr(llm, "chat", model)
    steps = agent.run_agent("sys", "task", tools.REVIEWER_TOOLS, run, llm.Usage(), **kwargs)
    return steps, model


# --- termination -----------------------------------------------------------


def test_loop_stops_when_the_agent_calls_finish(monkeypatch, run):
    steps, _ = drive(monkeypatch, [
        call("list_changed_files"),
        call("no_issues_in", path="src/app.py", reason="fine"),
        call("no_issues_in", path="src/util.py", reason="fine"),
        call("finish", summary="all good"),
    ], run)

    assert steps == 4
    assert run.done is True
    assert run.summary == "all good"


def test_loop_stops_when_the_model_stops_calling_tools(monkeypatch, run):
    """A model that just talks is finished, whatever it meant."""
    steps, _ = drive(monkeypatch, [call("list_changed_files"), say("Looks fine to me.")], run)

    assert steps == 2
    assert run.summary == "Looks fine to me."


def test_step_limit_is_enforced(monkeypatch, run):
    """Without this the loop runs until the quota is gone."""
    forever = [call("list_changed_files") for _ in range(50)]

    with pytest.raises(agent.StepLimit, match="4 steps"):
        drive(monkeypatch, forever, run, max_steps=4)


def test_step_limit_keeps_whatever_was_found_first(monkeypatch, run):
    """Hitting the ceiling must not throw away work already done."""
    replies = [
        call("record_finding", path="src/app.py", line=1, category="bug",
             title="A real problem", explanation="because"),
    ] + [call("list_changed_files") for _ in range(20)]

    with pytest.raises(agent.StepLimit):
        drive(monkeypatch, replies, run, max_steps=3)

    assert [f.title for f in run.findings] == ["A real problem"]


# --- the conversation ------------------------------------------------------


def test_model_reply_is_appended_verbatim(monkeypatch, run):
    """thoughtSignature must survive.

    Gemini's thinking models return an opaque signature on their parts. If we
    rebuild the reply from its text instead of passing it back unchanged, the
    model loses its own reasoning chain partway through the run.
    """
    reply = {
        "role": "model",
        "parts": [{"functionCall": {"name": "list_changed_files", "args": {}},
                   "thoughtSignature": "OPAQUE-SIGNATURE"}],
    }
    _steps, model = drive(monkeypatch, [reply, call("finish", summary="")], run)

    second_turn = model.seen[1]
    assert reply in second_turn, "the reply object itself must go back in"
    assert second_turn[1]["parts"][0]["thoughtSignature"] == "OPAQUE-SIGNATURE"


def test_tool_result_echoes_the_call_id(monkeypatch, run):
    """Gemini tags each call with an id. Parallel calls mispair without it."""
    reply = {"role": "model", "parts": [
        {"functionCall": {"name": "list_changed_files", "args": {}, "id": "abc123"}},
    ]}
    _steps, model = drive(monkeypatch, [reply, call("finish", summary="")], run)

    result_part = model.seen[1][2]["parts"][0]
    assert result_part["functionResponse"]["id"] == "abc123"


def test_parallel_calls_all_run_and_answer_in_order(monkeypatch, run):
    reply = {"role": "model", "parts": [
        {"functionCall": {"name": "read_diff", "args": {"path": "src/app.py"}, "id": "1"}},
        {"functionCall": {"name": "read_diff", "args": {"path": "src/util.py"}, "id": "2"}},
    ]}
    _steps, model = drive(monkeypatch, [reply, call("finish", summary="")], run)

    parts = model.seen[1][2]["parts"]
    assert [p["functionResponse"]["id"] for p in parts] == ["1", "2"]
    assert run.diffs_read == {"src/app.py", "src/util.py"}


# --- dispatch is forgiving -------------------------------------------------


def test_unknown_tool_becomes_a_message_not_a_crash(run):
    result = tools.dispatch({"name": "make_coffee", "args": {}}, run)
    assert "no tool named" in result


def test_wrong_arguments_become_a_message(run):
    result = tools.dispatch({"name": "read_diff", "args": {"wrong": 1}}, run)
    assert "ERROR" in result and "read_diff" in result


def test_a_raising_tool_becomes_a_message(monkeypatch, run):
    def boom(_run):
        raise ValueError("disk on fire")

    monkeypatch.setitem(tools.TOOLS, "list_changed_files",
                        (boom, tools.TOOLS["list_changed_files"][1]))

    result = tools.dispatch({"name": "list_changed_files", "args": {}}, run)
    assert "ValueError" in result and "disk on fire" in result


# --- the tools themselves --------------------------------------------------


def test_list_changed_files_names_unreviewable_files(run):
    listing = tools.list_changed_files(run)
    assert "logo.png" in listing
    assert "cannot review" in listing


def test_read_diff_records_coverage(run):
    tools.read_diff(run, "src/app.py")
    assert run.diffs_read == {"src/app.py"}


def test_read_diff_of_a_file_not_in_the_pr(run):
    assert "not part of this pull request" in tools.read_diff(run, "other.py")


def test_read_diff_of_a_binary_file_explains_itself(run):
    assert "no diff text" in tools.read_diff(run, "logo.png")


# --- filename matching ------------------------------------------------------
# Found by a live watcher run: a pull request containing both test.py and
# watch_test.py attributed watch_test.py's diff and its finding to test.py,
# then reported watch_test.py as never opened.


@pytest.fixture
def similar_names():
    """A PR with one filename that is a bare suffix of another."""
    return tools.Run(codebase=None, files=[
        {"filename": "test.py", "status": "added", "changes": 0},          # no patch
        {"filename": "watch_test.py", "status": "added", "changes": 4,
         "patch": "@@\n+def apply_discount(price, percent):\n"},
    ])


def test_a_longer_filename_is_not_matched_to_a_shorter_one(similar_names):
    assert similar_names.file("watch_test.py")["filename"] == "watch_test.py"
    assert similar_names.file("test.py")["filename"] == "test.py"


def test_reading_a_diff_credits_the_right_file(similar_names):
    """Coverage was wrong because the read was credited to the other file."""
    similar_names.diffs_read.clear()
    tools.read_diff(similar_names, "watch_test.py")

    assert similar_names.diffs_read == {"watch_test.py"}


def test_a_finding_is_anchored_to_the_file_it_names(similar_names):
    """Posted against the wrong file, this becomes a comment on code that
    never contained the problem - and GitHub 422s the whole batch."""
    tools.record_finding(
        similar_names, path="watch_test.py", line=6, category="bug",
        title="apply_discount allows negative prices", explanation="no bound on percent",
    )

    assert similar_names.findings[0].file_path == "watch_test.py"


def test_a_prefixed_path_still_resolves(similar_names):
    """Models copy 'a/' and 'b/' prefixes straight out of diff headers."""
    assert similar_names.file("a/watch_test.py")["filename"] == "watch_test.py"
    assert similar_names.file("./watch_test.py")["filename"] == "watch_test.py"


def test_a_short_path_still_resolves_to_a_nested_file():
    run = tools.Run(codebase=None, files=[
        {"filename": "src/payments/refund.py", "changes": 3, "patch": "@@\n+x\n"},
    ])
    assert run.file("refund.py")["filename"] == "src/payments/refund.py"


def test_an_unrelated_file_still_does_not_match(similar_names):
    assert similar_names.file("totally_other.py") is None


def test_record_finding_rejects_a_file_outside_the_pr(run):
    """Otherwise the agent drifts into reviewing the whole repository."""
    result = tools.record_finding(
        run, path="somewhere/else.py", line=3, category="bug",
        title="Not our business", explanation="x",
    )
    assert "Rejected" in result
    assert run.findings == []


def test_record_finding_deduplicates(run):
    for _ in range(2):
        tools.record_finding(run, path="src/app.py", line=1, category="bug",
                             title="Same problem", explanation="x")
    assert len(run.findings) == 1


def test_verdict_tools_need_a_real_id(run):
    assert "No finding" in tools.confirm_finding(run, "nope", "reason")
    assert "No finding" in tools.reject_finding(run, "nope", "reason")


def test_reject_marks_the_finding(run):
    run.findings.append(Finding("src/app.py", 1, "bug", "A problem", "x"))
    finding_id = run.findings[0].finding_id

    tools.reject_finding(run, finding_id, "Read the file, already handled")

    assert run.findings[0].verdict == "rejected"
    assert "already handled" in run.findings[0].verdict_reason
