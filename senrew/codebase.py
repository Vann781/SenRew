"""Reading and searching the code the agent is reviewing.

Two backends behind one small interface:

  LocalCodebase   reads your working tree. Instant, free, no rate limits.
                  This is the payoff for being a local tool.
  GitHubCodebase  reads the repository over the API, for when there is no
                  local clone.

Both refuse to leave the repository. The agent decides which paths to open
based on text it read out of a diff, and that text comes from whoever opened
the pull request - so "read this file" is untrusted input. Without the check
below, a diff could talk the agent into reading ~/.ssh/id_rsa and quoting it
back in a public review comment.
"""

import base64
from pathlib import Path
from typing import Any

import requests

from senrew import config

# Never walked when searching. Saves time and avoids dumping build junk.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".aws-sam", ".idea", ".vscode", "vendor",
}

# Searched and read as text; everything else is treated as binary.
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".rs", ".c",
    ".h", ".cpp", ".cs", ".php", ".sh", ".yml", ".yaml", ".json", ".toml",
    ".md", ".txt", ".cfg", ".ini", ".sql", ".html", ".css",
}


class PathRefused(Exception):
    """The agent asked for a path outside the repository."""


def _clip(text: str) -> str:
    """Keep one tool result from swallowing the whole context window."""
    if len(text) <= config.MAX_TOOL_CHARS:
        return text
    return text[: config.MAX_TOOL_CHARS] + "\n... [truncated]"


def _numbered(text: str, start_line: int, end_line: int) -> str:
    """Render a slice of a file with line numbers.

    Line numbers matter: the agent has to cite a real line for the finding to
    become an inline comment, and counting lines itself is exactly the sort of
    thing models get wrong.
    """
    lines = text.splitlines()
    start = max(1, start_line)
    end = min(len(lines), end_line if end_line > 0 else len(lines))

    if start > len(lines):
        return f"(file has only {len(lines)} lines)"

    capped = min(end, start + config.MAX_FILE_LINES - 1)
    body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, capped + 1))

    if capped < end:
        body += f"\n... [truncated at {config.MAX_FILE_LINES} lines, file has {len(lines)}]"
    return body


class LocalCodebase:
    """The working tree on disk."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        """Turn an agent-supplied path into a real one, or refuse.

        strict=False so a missing file resolves and reports 'not found'
        rather than raising something that looks like a refusal.
        """
        candidate = (self.root / path).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise PathRefused(
                f"'{path}' is outside the repository. Only files inside "
                f"{self.root.name} can be read."
            )
        return candidate

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"(no such file: {path})"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(could not read {path}: {exc})"
        return _clip(_numbered(text, start_line, end_line))

    def read_raw(self, path: str) -> str | None:
        """The file exactly as it is, for a parser rather than for reading.

        read_file numbers and truncates, which is right for a model and wrong
        for anything that has to parse the result. Same path guard applies.
        """
        try:
            target = self._resolve(path)
        except PathRefused:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None  # binary, or unreadable: nothing to parse

    def search(self, query: str, max_results: int = 0) -> str:
        limit = max_results or config.MAX_SEARCH_RESULTS
        hits: list[str] = []

        for file in sorted(self.root.rglob("*")):
            if len(hits) >= limit:
                break
            if not file.is_file() or file.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in file.parts):
                continue

            try:
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            rel = file.relative_to(self.root).as_posix()
            for number, line in enumerate(lines, 1):
                if query in line:
                    hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if len(hits) >= limit:
                        break

        if not hits:
            return f"No matches for {query!r}."
        return _clip("\n".join(hits))


class GitHubCodebase:
    """The repository as GitHub sees it, at one commit."""

    def __init__(self, repository: str, ref: str, session: Any = None):
        self.repository = repository
        self.ref = ref
        self.http = session or requests

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "SenRew",
        }

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        if path.startswith("/") or ".." in Path(path).parts:
            raise PathRefused(f"'{path}' is not a repository-relative path.")

        response = self.http.get(
            f"{config.GITHUB_API}/repos/{self.repository}/contents/{path}",
            headers=self._headers(),
            params={"ref": self.ref},
            timeout=20,
        )
        if response.status_code == 404:
            return f"(no such file: {path})"
        if response.status_code != 200:
            return f"(could not read {path}: HTTP {response.status_code})"

        payload = response.json()
        if payload.get("encoding") != "base64":
            return f"(unsupported encoding for {path})"

        raw = base64.b64decode(payload["content"])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"(binary file: {path})"

        return _clip(_numbered(text, start_line, end_line))

    def read_raw(self, path: str) -> str | None:
        """The file at the pull request's head commit, unmodified."""
        if path.startswith("/") or ".." in Path(path).parts:
            return None

        response = self.http.get(
            f"{config.GITHUB_API}/repos/{self.repository}/contents/{path}",
            headers=self._headers(),
            params={"ref": self.ref},
            timeout=20,
        )
        if response.status_code != 200:
            return None

        payload = response.json()
        if payload.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(payload["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None  # binary

    def search(self, query: str, max_results: int = 0) -> str:
        limit = max_results or config.MAX_SEARCH_RESULTS
        response = self.http.get(
            f"{config.GITHUB_API}/search/code",
            headers=self._headers(),
            params={"q": f"{query} repo:{self.repository}", "per_page": limit},
            timeout=20,
        )
        if response.status_code == 403:
            return "(code search rate limit reached - try again shortly)"
        if response.status_code != 200:
            return f"(search failed: HTTP {response.status_code})"

        items = response.json().get("items", [])
        if not items:
            return f"No matches for {query!r}."
        return _clip("\n".join(item["path"] for item in items[:limit]))
