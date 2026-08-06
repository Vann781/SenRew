"""Everything that talks to GitHub.

Uses the user's own token, which never leaves their machine.
"""

import json
import random
import time
from typing import Any

import requests

from senrew import config
from senrew.models import Finding, Review

TIMEOUT = 20
MAX_RETRIES = 3
MAX_PAGES = 5  # 500 items, far more than one review needs


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SenRew",
    }


def _request(method: str, path: str, params=None, body=None) -> requests.Response:
    """One GitHub call, retrying server errors and rate limits.

    4xx comes back rather than raising, because the caller needs to handle
    422 specially when posting a review.
    """
    url = f"{config.GITHUB_API}{path}"
    last_error = ""

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.request(
                method, url, headers=_headers(), params=params,
                data=json.dumps(body) if body is not None else None, timeout=TIMEOUT,
            )

            if response.status_code == 403 and "rate limit" in response.text.lower():
                if attempt < MAX_RETRIES:
                    time.sleep(min(int(response.headers.get("Retry-After", 30)), 60))
                    continue

            if response.status_code < 500:
                return response
            last_error = f"{response.status_code}: {response.text[:200]}"

        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < MAX_RETRIES:
            time.sleep(random.uniform(0, min(8.0, 2**attempt)))

    raise RuntimeError(f"GitHub request failed after retries: {last_error}")


def _split(repository: str) -> tuple[str, str]:
    owner, _, name = repository.partition("/")
    if not owner or not name:
        raise ValueError(f"Expected 'owner/repo', got: {repository}")
    return owner, name


# --- reading ---------------------------------------------------------------


def get_pull_request(repository: str, number: int) -> dict[str, Any]:
    owner, name = _split(repository)
    response = _request("GET", f"/repos/{owner}/{name}/pulls/{number}")
    if response.status_code != 200:
        raise RuntimeError(
            f"Could not fetch {repository}#{number}: "
            f"{response.status_code} {response.text[:200]}"
        )
    return response.json()


def list_open_pull_requests(repository: str) -> list[dict[str, Any]]:
    """Every open pull request, most recently updated first.

    Paginated: GitHub's default page size is 30, so an unpaginated call
    silently misses pull requests on a busy repository.
    """
    owner, name = _split(repository)
    found: list[dict[str, Any]] = []

    for page in range(1, MAX_PAGES + 1):
        response = _request(
            "GET", f"/repos/{owner}/{name}/pulls",
            params={"state": "open", "per_page": 100, "page": page,
                    "sort": "updated", "direction": "desc"},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Could not list pull requests for {repository}: "
                f"{response.status_code} {response.text[:200]}"
            )
        batch = response.json()
        found.extend(batch)
        if len(batch) < 100:
            break

    return found


def get_changed_files(repository: str, number: int) -> list[dict[str, Any]]:
    """Every changed file with its patch.

    Paginated for the same reason as above - without it a large pull request
    is silently truncated and only partly reviewed, which looks like it worked.

    Binary and empty files come back with no 'patch' key. They are returned
    as-is; the tool layer reports them as unreviewable rather than dropping
    them, so a skipped file is never invisible.
    """
    owner, name = _split(repository)
    files: list[dict[str, Any]] = []

    for page in range(1, MAX_PAGES + 1):
        response = _request(
            "GET", f"/repos/{owner}/{name}/pulls/{number}/files",
            params={"per_page": 100, "page": page},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Could not fetch files for {repository}#{number}: "
                f"{response.status_code} {response.text[:200]}"
            )
        batch = response.json()
        files.extend(batch)
        if len(batch) < 100:
            break

    return files


# --- posting ---------------------------------------------------------------


def render_comment(finding: Finding) -> str:
    """The markdown body of one inline comment."""
    parts = [
        f"**{finding.severity_band.upper()} · {finding.category.title()} · "
        f"score {finding.severity_score}**",
        "",
        finding.title,
        "",
        finding.explanation,
    ]
    if finding.suggested_fix:
        parts += ["", "**Suggested fix:**", "```suggestion", finding.suggested_fix, "```"]
    if finding.verdict == "confirmed" and finding.verdict_reason:
        parts += ["", f"<sub>Verified: {finding.verdict_reason}</sub>"]
    parts += ["", "<sub>SenRew</sub>"]
    return "\n".join(parts)


def build_comments(review: Review) -> list[dict[str, Any]]:
    """Findings as GitHub inline comment objects.

    Uses 'line' and 'side' rather than the legacy 'position' field, which
    counts lines within the diff rather than the file.
    """
    comments = []
    for finding in review.findings[: config.MAX_COMMENTS_PER_REVIEW]:
        if finding.line <= 0:
            continue  # no usable position; it stays in the summary
        comments.append({
            "path": finding.file_path,
            "line": finding.line,
            "side": "RIGHT",
            "body": render_comment(finding),
        })
    return comments


def preview(review: Review) -> str:
    """Exactly what post_review would send, without sending it.

    Reuses build_comments deliberately: a preview built by separate code
    would be a preview of something else.
    """
    comments = build_comments(review)
    lines = ["=" * 70, "DRY RUN - not posted", "=" * 70, review.summary,
             f"--- {len(comments)} inline comment(s) ---"]
    for comment in comments:
        lines += ["", f"{comment['path']}:{comment['line']}", comment["body"]]

    without_line = [f for f in review.findings if f.line <= 0]
    if without_line:
        lines += ["", f"({len(without_line)} finding(s) had no line number and "
                      f"would appear in the summary only)"]
    lines.append("=" * 70)
    return "\n".join(lines)


def post_review(repository: str, number: int, review: Review, head_sha: str) -> dict[str, Any]:
    """Post ONE review containing every inline comment.

    Never post comments individually in a loop: GitHub applies undocumented
    secondary rate limits to content creation.

    If GitHub rejects a comment position with 422 the whole request fails and
    every other comment is lost with it, so we retry once with everything
    folded into the body. A positioning problem should cost the position, not
    the finding.
    """
    owner, name = _split(repository)
    path = f"/repos/{owner}/{name}/pulls/{number}/reviews"
    comments = build_comments(review)

    response = _request("POST", path, body={
        "commit_id": head_sha, "event": "COMMENT",
        "body": review.summary, "comments": comments,
    })
    if response.status_code in (200, 201):
        return response.json()

    if response.status_code == 422 and comments:
        body = [review.summary, "", "---", ""]
        for finding in review.findings:
            body += [f"### {finding.severity_band.upper()} · {finding.file_path}:"
                     f"{finding.line}", "", finding.title, "", finding.explanation, ""]
        response = _request("POST", path, body={
            "commit_id": head_sha, "event": "COMMENT", "body": "\n".join(body),
        })
        if response.status_code in (200, 201):
            return response.json()

    raise RuntimeError(
        f"Could not post review to {repository}#{number}: "
        f"{response.status_code} {response.text[:300]}"
    )
