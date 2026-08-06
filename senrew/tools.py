"""The tools the agent can call, and the context they act on.

A tool is a plain function plus a JSON schema. The registry is a dict. There
is no framework here on purpose - the whole point of the rewrite was that the
agent loop should be readable in one sitting.

Two of the tools are sinks: record_finding and finish do not return
information, they change the run. That is how structured output comes out of
a tool-calling agent, and it is why JSON mode is not needed (Gemini will not
accept responseMimeType and tools together anyway).
"""

from dataclasses import dataclass, field
from typing import Any, Callable

from senrew import config
from senrew.codebase import PathRefused
from senrew.models import Finding

# name -> (function, schema)
TOOLS: dict[str, tuple[Callable, dict]] = {}


def tool(name: str, description: str, properties: dict, required: list[str] | None = None):
    """Register a tool. The schema is what Gemini is shown."""
    def wrap(fn):
        TOOLS[name] = (fn, {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        })
        return fn
    return wrap


@dataclass
class Run:
    """Everything one agent run reads and produces."""

    codebase: Any
    files: list[dict]                       # changed files, from GitHub
    findings: list[Finding] = field(default_factory=list)
    diffs_read: set[str] = field(default_factory=set)
    done: bool = False
    summary: str = ""
    on_tool: Callable[[str, dict], None] | None = None

    def file(self, path: str) -> dict | None:
        """Find a changed file by path, tolerating a leading a/ or b/.

        Every comparison is anchored on a "/" boundary. Matching a bare
        suffix instead would make "watch_test.py" match a file called
        "test.py", and then the whole review goes quietly wrong: the diff is
        recorded against the wrong file, coverage reports the real one as
        never opened, and any finding is posted on a line in a file that
        never had that code.
        """
        wanted = path.lstrip("./")
        for entry in self.files:
            name = entry.get("filename", "")
            if not name:
                continue
            if (name == wanted
                    or name.endswith("/" + wanted)     # model gave a short path
                    or wanted.endswith("/" + name)):   # model gave a/ or b/ prefix
                return entry
        return None

    def finding(self, finding_id: str) -> Finding | None:
        return next((f for f in self.findings if f.finding_id == finding_id), None)


# --- reading tools ---------------------------------------------------------


@tool(
    "list_changed_files",
    "List every file changed by this pull request, with how many lines "
    "changed. Call this first.",
    {},
)
def list_changed_files(run: Run) -> str:
    if not run.files:
        return "This pull request changes no files."

    lines = []
    for entry in run.files:
        name = entry.get("filename", "?")
        status = entry.get("status", "modified")
        changes = entry.get("changes", 0)

        if not entry.get("patch"):
            # Named explicitly rather than hidden. A file that cannot be
            # reviewed should be visible as such, not silently absent.
            why = "binary or empty - GitHub sent no diff text, cannot review"
            lines.append(f"{name} ({status}, {changes} changes) - {why}")
        else:
            lines.append(f"{name} ({status}, {changes} changes)")

    return "\n".join(lines)


@tool(
    "read_diff",
    "Read the diff of one changed file - only the lines this pull request "
    "added or removed.",
    {"path": {"type": "string", "description": "Path as shown by list_changed_files"}},
    ["path"],
)
def read_diff(run: Run, path: str) -> str:
    entry = run.file(path)
    if entry is None:
        return f"'{path}' is not part of this pull request."

    run.diffs_read.add(entry["filename"])
    patch = entry.get("patch")
    if not patch:
        return f"'{path}' has no diff text (binary or empty file)."
    return patch[: config.MAX_TOOL_CHARS]


@tool(
    "read_file",
    "Read a file from the repository, including code this pull request did "
    "NOT change. Use it to check whether a concern is already handled "
    "somewhere the diff does not show.",
    {
        "path": {"type": "string", "description": "Repository-relative path"},
        "start_line": {"type": "integer", "description": "First line (default 1)"},
        "end_line": {"type": "integer", "description": "Last line (default end of file)"},
    },
    ["path"],
)
def read_file(run: Run, path: str, start_line: int = 1, end_line: int = 0) -> str:
    try:
        return run.codebase.read_file(path, start_line, end_line)
    except PathRefused as exc:
        return f"Refused: {exc}"


@tool(
    "search_repo",
    "Search the repository for a literal string - a function name, a "
    "constant, a call site. Use it to find where something is defined or used.",
    {
        "query": {"type": "string", "description": "Literal text to look for"},
        "max_results": {"type": "integer", "description": "Cap on matches"},
    },
    ["query"],
)
def search_repo(run: Run, query: str, max_results: int = 0) -> str:
    try:
        return run.codebase.search(query, max_results)
    except PathRefused as exc:
        return f"Refused: {exc}"


# --- sinks -----------------------------------------------------------------


@tool(
    "record_finding",
    "Record a real problem you have verified in the code. Judge impact (how "
    "bad if it happens) and likelihood (whether it happens at all) "
    "separately. Do not supply a severity number - it is calculated.",
    {
        "path": {"type": "string", "description": "File the problem is in"},
        "line": {"type": "integer", "description": "Line in the NEW version of the file"},
        "category": {"type": "string",
                     "description": "bug | security | performance | style | maintainability"},
        "title": {"type": "string", "description": "One sentence saying what is wrong"},
        "explanation": {"type": "string",
                        "description": "Why this is a problem in THIS code. Name the "
                                       "actual variables and functions you read."},
        "suggested_fix": {"type": "string", "description": "Corrected line(s), or empty"},
        "impact": {"type": "string", "description": "none | minor | moderate | major | severe"},
        "likelihood": {"type": "string",
                       "description": "rare | unlikely | possible | likely | certain"},
        "blast_radius": {"type": "string",
                         "description": "single_line | single_function | single_file | "
                                        "module | system"},
    },
    ["path", "line", "category", "title", "explanation"],
)
def record_finding(
    run: Run, path: str, line: int, category: str, title: str, explanation: str,
    suggested_fix: str = "", impact: str = "moderate",
    likelihood: str = "possible", blast_radius: str = "single_function",
) -> str:
    entry = run.file(path)
    if entry is None:
        # The agent may only report on files this pull request touched.
        # Without this it drifts into reviewing the whole repository.
        return (f"Rejected: '{path}' is not changed by this pull request. "
                f"Only report problems in changed files.")

    finding = Finding(
        file_path=entry["filename"],
        line=max(int(line or 0), 0),
        category=category.lower().strip(),
        title=title.strip()[:200],
        explanation=explanation.strip()[:1500],
        suggested_fix=suggested_fix.strip()[:1500],
        impact=impact.lower().strip(),
        likelihood=likelihood.lower().strip(),
        blast_radius=blast_radius.lower().strip(),
    )

    # Same problem reported twice is one problem.
    if any(f.fingerprint() == finding.fingerprint() for f in run.findings):
        return "Already recorded - skipped."

    run.findings.append(finding)
    return f"Recorded as [{finding.finding_id}]."


@tool(
    "confirm_finding",
    "Confirm a finding after checking it against the real code. Say what you "
    "actually verified.",
    {
        "finding_id": {"type": "string", "description": "The id in square brackets"},
        "reason": {"type": "string", "description": "What you checked and what you saw"},
    },
    ["finding_id", "reason"],
)
def confirm_finding(run: Run, finding_id: str, reason: str) -> str:
    finding = run.finding(finding_id)
    if finding is None:
        return f"No finding with id {finding_id}."
    finding.verdict = "confirmed"
    finding.verdict_reason = reason.strip()[:500]
    return f"[{finding_id}] confirmed."


@tool(
    "reject_finding",
    "Reject a finding that is wrong, or that you cannot support from the code "
    "you can read. Rejecting freely is correct - a wrong published finding "
    "costs more than a missed one.",
    {
        "finding_id": {"type": "string", "description": "The id in square brackets"},
        "reason": {"type": "string", "description": "Why it does not hold"},
    },
    ["finding_id", "reason"],
)
def reject_finding(run: Run, finding_id: str, reason: str) -> str:
    finding = run.finding(finding_id)
    if finding is None:
        return f"No finding with id {finding_id}."
    finding.verdict = "rejected"
    finding.verdict_reason = reason.strip()[:500]
    return f"[{finding_id}] rejected."


@tool(
    "finish",
    "Finish. Call this when you have reviewed every changed file and recorded "
    "everything worth raising. Finding nothing is a valid and common result.",
    {"summary": {"type": "string", "description": "One or two sentences on what you did"}},
    ["summary"],
)
def finish(run: Run, summary: str = "") -> str:
    run.done = True
    run.summary = summary.strip()
    return "Done."


# --- dispatch --------------------------------------------------------------

REVIEWER_TOOLS = ["list_changed_files", "read_diff", "read_file", "search_repo",
                  "record_finding", "finish"]
VERIFIER_TOOLS = ["read_diff", "read_file", "search_repo",
                  "confirm_finding", "reject_finding", "finish"]


def schemas(names: list[str]) -> list[dict]:
    return [TOOLS[n][1] for n in names if n in TOOLS]


def dispatch(call: dict[str, Any], run: Run) -> Any:
    """Run one tool call and return its result.

    A tool never raises out of here. An agent that can read the error has a
    chance of recovering; an exception just ends the review.
    """
    name = call.get("name", "")
    args = dict(call.get("args") or {})

    if run.on_tool:
        run.on_tool(name, args)

    entry = TOOLS.get(name)
    if entry is None:
        return f"ERROR: no tool named '{name}'."

    try:
        return entry[0](run, **args)
    except TypeError as exc:
        return f"ERROR: wrong arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
        return f"ERROR: {name} failed: {type(exc).__name__}: {exc}"
