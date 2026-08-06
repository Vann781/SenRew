"""Does this file actually parse?

A file that cannot be parsed is broken, full stop. There is no judgement to
make and no reason to ask a model about it - so this asks a parser instead.
That makes the answer deterministic: the same broken file is reported on every
run, rather than whenever the model happens to think it is worth mentioning.

Two tiers:

  stdlib    Python, JSON, TOML, XML. Always available, no dependencies.
  external  JavaScript, Ruby, PHP, Go. Only when that toolchain happens to be
            installed. A missing toolchain is not a finding - it just means we
            fall back to the model for those files.

Everything else returns None. You cannot parse Rust without a Rust compiler,
and pretending otherwise would produce confident nonsense.
"""

import ast
import json
import shutil
import subprocess
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path

TIMEOUT = 10

# Extension -> (executable, args builder). Each command must exit non-zero and
# say something useful on stderr when the file does not parse.
EXTERNAL = {
    ".js": ("node", lambda p: ["node", "--check", p]),
    ".mjs": ("node", lambda p: ["node", "--check", p]),
    ".cjs": ("node", lambda p: ["node", "--check", p]),
    ".rb": ("ruby", lambda p: ["ruby", "-c", p]),
    ".php": ("php", lambda p: ["php", "-l", p]),
    ".go": ("gofmt", lambda p: ["gofmt", "-e", p]),
}


def _strip_bom(source: str) -> str:
    """Remove a leading byte-order mark.

    Python reads a BOM happily from a file but ast.parse rejects it in a
    string, so leaving it in would report a syntax error on a file that runs
    perfectly well. A real pull request in testing had exactly this.
    """
    return source[1:] if source.startswith("﻿") else source


def _python(source: str) -> str | None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return f"{where}: {exc.msg}"
    except ValueError as exc:
        # e.g. source containing null bytes
        return str(exc)
    return None


def _json(source: str) -> str | None:
    try:
        json.loads(source)
    except json.JSONDecodeError as exc:
        return f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return None


def _toml(source: str) -> str | None:
    try:
        tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        return str(exc)
    return None


def _xml(source: str) -> str | None:
    try:
        ElementTree.fromstring(source)
    except ElementTree.ParseError as exc:
        return str(exc)
    return None


STDLIB = {
    ".py": _python, ".pyi": _python,
    ".json": _json,
    ".toml": _toml,
    ".xml": _xml, ".svg": _xml,
}


def _readable(output: str, tmp_path: str) -> str:
    """Turn a toolchain's error dump into one useful line.

    These tools print the offending file path, the source line, a caret, then
    the actual message several lines down. Taking the first line alone yields
    "tmp8446huxk.js:1", which tells the reader nothing - so find the line that
    names the error and pair it with the line number.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "does not parse"

    name = Path(tmp_path).name
    number = ""
    for line in lines:
        if line.startswith(name) and ":" in line:
            tail = line[len(name):].lstrip(":").split(":")[0]
            if tail.isdigit():
                number = tail
                break

    message = next(
        (line for line in lines if "error" in line.lower()),
        lines[-1],
    ).replace(tmp_path, name)

    return (f"line {number}: {message}" if number else message)[:300]


def _external(suffix: str, source: str) -> str | None:
    """Run an installed toolchain's syntax check, if there is one."""
    binary, build = EXTERNAL[suffix]
    if not shutil.which(binary):
        return None  # not installed: not our problem, and not a finding

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            tmp = handle.name

        result = subprocess.run(
            build(tmp), capture_output=True, text=True, timeout=TIMEOUT
        )
        if result.returncode == 0:
            return None

        return _readable(result.stderr or result.stdout or "", tmp)

    except (OSError, subprocess.SubprocessError):
        # A checker that crashes or hangs tells us nothing about the file.
        return None
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def can_check(path: str) -> bool:
    """True if this file has a checker we can actually run."""
    suffix = Path(path).suffix.lower()
    if suffix in STDLIB:
        return True
    return suffix in EXTERNAL and bool(shutil.which(EXTERNAL[suffix][0]))


def check(path: str, source: str) -> str | None:
    """Return a readable syntax error, or None if it parses or cannot be checked.

    None deliberately covers both "fine" and "no checker available". A caller
    that reported the second as a problem would flag every .rs file on a
    machine without Rust installed.
    """
    if not source or not source.strip():
        return None  # an empty file is not a syntax error

    suffix = Path(path).suffix.lower()
    source = _strip_bom(source)

    if suffix in STDLIB:
        return STDLIB[suffix](source)
    if suffix in EXTERNAL:
        return _external(suffix, source)
    return None
