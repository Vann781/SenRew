"""Tests for push detection and the severity formula.

The watcher tests use real git repositories in a temp directory, because the
thing worth testing is that we read git state correctly - including packed
refs, which a hand-rolled .git/refs reader would miss.
"""

import subprocess

import pytest

import watcher
from senrew import severity
from senrew.models import Finding


# --- push detection --------------------------------------------------------


def test_detect_pushes_spots_a_changed_branch():
    assert watcher.detect_pushes({"main": "aaa"}, {"main": "bbb"}) == ["main"]


def test_detect_pushes_spots_a_new_branch():
    assert watcher.detect_pushes({"main": "aaa"}, {"main": "aaa", "dev": "ccc"}) == ["dev"]


def test_detect_pushes_ignores_an_unchanged_branch():
    assert watcher.detect_pushes({"main": "aaa"}, {"main": "aaa"}) == []


def test_detect_pushes_ignores_a_deleted_branch():
    """A branch that disappeared has nothing to review."""
    assert watcher.detect_pushes({"main": "aaa", "old": "bbb"}, {"main": "aaa"}) == []


def test_detect_pushes_on_first_run_reports_everything():
    """Which is why the watcher seeds itself before looping."""
    assert watcher.detect_pushes({}, {"main": "aaa"}) == ["main"]


# --- remote parsing --------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:Vann781/root-access.git", "Vann781/root-access"),
    ("https://github.com/Vann781/root-access.git", "Vann781/root-access"),
    ("https://github.com/Vann781/root-access", "Vann781/root-access"),
    ("ssh://git@github.com/owner/repo.git", "owner/repo"),
])
def test_remote_slug_parses_github_urls(tmp_path, url, expected):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)

    assert watcher.remote_slug(tmp_path) == expected


def test_remote_slug_returns_none_for_a_non_github_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://gitlab.com/owner/repo.git"], check=True)

    assert watcher.remote_slug(tmp_path) is None


def test_remote_slug_returns_none_with_no_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert watcher.remote_slug(tmp_path) is None


def test_remote_refs_of_a_repo_with_no_remote_is_empty(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert watcher.remote_refs(tmp_path) == {}


def test_remote_refs_survives_a_directory_that_is_not_a_repo(tmp_path):
    """A stray folder must not take the whole watcher down."""
    assert watcher.remote_refs(tmp_path / "nope") == {}


# --- finding repositories --------------------------------------------------


def test_find_repos_accepts_a_repo_directly(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert watcher.find_repos([str(tmp_path)]) == [tmp_path.resolve()]


def test_find_repos_looks_one_level_down(tmp_path):
    """So you can point it at a folder full of projects."""
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        subprocess.run(["git", "init", "-q", str(tmp_path / name)], check=True)
    (tmp_path / "not-a-repo").mkdir()

    found = {p.name for p in watcher.find_repos([str(tmp_path)])}

    assert found == {"alpha", "beta"}


def test_find_repos_ignores_a_missing_path():
    assert watcher.find_repos(["/definitely/not/here"]) == []


# --- severity: ported from CodeSentinel as the regression anchor -----------


def test_the_documented_worked_example():
    """A missing authorisation check in src/payments/refund.py scores 72.5."""
    score = severity.compute_score(
        "major", "possible", "security", "src/payments/refund.py", "module"
    )
    assert score == 72.5
    assert severity.band_for_score(score) == "high"


def test_no_impact_scores_zero():
    assert severity.compute_score("none", "certain", "bug", "src/app.py") == 0.0


def test_band_boundaries():
    assert severity.band_for_score(80.0) == "critical"
    assert severity.band_for_score(79.9) == "high"
    assert severity.band_for_score(60.0) == "high"
    assert severity.band_for_score(59.9) == "medium"
    assert severity.band_for_score(35.0) == "medium"
    assert severity.band_for_score(34.9) == "low"


def test_the_security_path_multiplier_is_exact():
    plain = severity.compute_score("moderate", "possible", "bug", "src/utils.py")
    sensitive = severity.compute_score("moderate", "possible", "bug", "src/auth/login.py")
    assert sensitive == pytest.approx(plain * severity.SECURITY_PATH_MULTIPLIER, abs=0.1)


def test_the_test_file_multiplier_is_exact():
    normal = severity.compute_score("moderate", "possible", "bug", "src/app.py")
    in_test = severity.compute_score("moderate", "possible", "bug", "src/app_test.py")
    assert in_test == pytest.approx(normal * severity.TEST_FILE_MULTIPLIER, abs=0.1)


@pytest.mark.parametrize("path", ["src/app.py", "src/latest_release.py", "contest.py"])
def test_ordinary_files_are_not_mistaken_for_tests(path):
    """'latest' and 'contest' contain 'test' but are not test files."""
    assert severity.is_test_file(path) is False


def test_unknown_labels_do_not_crash():
    score = severity.compute_score("catastrophic", "maybe", "vibes", "a.py", "galaxy")
    assert 0.0 <= score <= 100.0


def test_every_combination_stays_in_range():
    for impact in severity.IMPACT_WEIGHT:
        for likelihood in severity.LIKELIHOOD_WEIGHT:
            for category in severity.CATEGORY_MULTIPLIER:
                for radius in severity.RADIUS_MULTIPLIER:
                    score = severity.compute_score(
                        impact, likelihood, category, "src/app.py", radius
                    )
                    assert 0.0 <= score <= 100.0


def test_meets_minimum_filters_correctly():
    high = severity.score_finding(
        Finding("src/auth.py", 1, "security", "t", "x",
                impact="severe", likelihood="likely")
    )
    low = severity.score_finding(
        Finding("src/a.py", 1, "style", "t", "x", impact="minor", likelihood="rare")
    )
    assert severity.meets_minimum(high, "medium") is True
    assert severity.meets_minimum(low, "high") is False


def test_fingerprint_is_stable_across_line_moves():
    a = Finding("a.py", 1, "bug", "Off by one", "x")
    b = Finding("a.py", 99, "bug", "Off by one", "x")
    assert a.fingerprint() == b.fingerprint()
