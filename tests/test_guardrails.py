"""Tests for the limits on what a tool may read and return.

The agent chooses paths from text it read out of a diff, and that diff was
written by whoever opened the pull request. "read this file" is therefore
untrusted input, and these tests are the reason it cannot be talked into
reading a private key and quoting it into a public review comment.
"""

import pytest

from senrew import config
from senrew.codebase import LocalCodebase, PathRefused


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "\n".join(f"line {n}" for n in range(1, 51)), encoding="utf-8"
    )
    (tmp_path / "secret.txt").write_text("inside the repo", encoding="utf-8")
    # A file the agent must never reach, one level up.
    (tmp_path.parent / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    return LocalCodebase(tmp_path)


# --- path safety -----------------------------------------------------------


@pytest.mark.parametrize("path", [
    "../id_rsa",
    "../../id_rsa",
    "src/../../id_rsa",
    "./src/./../../id_rsa",
])
def test_paths_escaping_the_repo_are_refused(repo, path):
    with pytest.raises(PathRefused):
        repo.read_file(path)


def test_an_absolute_path_outside_the_repo_is_refused(repo, tmp_path):
    with pytest.raises(PathRefused):
        repo.read_file(str(tmp_path.parent / "id_rsa"))


def test_the_refusal_does_not_leak_the_contents(repo):
    try:
        repo.read_file("../id_rsa")
    except PathRefused as exc:
        assert "PRIVATE KEY" not in str(exc)


def test_normal_paths_still_work(repo):
    assert "line 1" in repo.read_file("src/app.py")
    assert "inside the repo" in repo.read_file("secret.txt")


def test_a_missing_file_reports_rather_than_refuses(repo):
    """'not found' and 'not allowed' are different answers to the agent."""
    assert "no such file" in repo.read_file("src/nope.py")


# --- size caps -------------------------------------------------------------


def test_file_reads_are_capped(repo, monkeypatch):
    """One huge read would otherwise sit in the context of every later step."""
    monkeypatch.setattr(config, "MAX_FILE_LINES", 10)

    text = repo.read_file("src/app.py")

    assert "truncated" in text
    assert "line 11" not in text


def test_a_line_range_is_honoured(repo):
    text = repo.read_file("src/app.py", start_line=5, end_line=7)
    assert "line 5" in text and "line 7" in text
    assert "line 4" not in text and "line 8" not in text


def test_output_is_line_numbered(repo):
    """The agent has to cite a real line, and counting them itself goes wrong."""
    assert "5  line 5" in repo.read_file("src/app.py", start_line=5, end_line=5)


def test_a_range_past_the_end_of_the_file_is_explained(repo):
    assert "only 50 lines" in repo.read_file("src/app.py", start_line=999)


# --- search ----------------------------------------------------------------


def test_search_finds_matches_with_line_numbers(repo):
    assert "src/app.py:7:" in repo.search("line 7")


def test_search_results_are_capped(repo):
    assert len(repo.search("line", max_results=3).splitlines()) == 3


def test_search_with_no_matches_says_so(repo):
    assert "No matches" in repo.search("nothing here matches this")


def test_search_skips_noise_directories(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("needle", encoding="utf-8")
    (tmp_path / "real.py").write_text("needle", encoding="utf-8")

    results = LocalCodebase(tmp_path).search("needle")

    assert "real.py" in results
    assert "node_modules" not in results
