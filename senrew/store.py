"""Remembering what has already been reviewed.

A JSON file under ~/.senrew/. Keyed on the head commit, so a pull request is
re-reviewed only when new commits land - and restarting the watcher never
re-reviews everything it already did.
"""

import json
from typing import Any

from senrew import config
from senrew.models import Review

PATH = config.STATE_DIR / "reviews.json"


def _load() -> dict[str, Any]:
    if not PATH.exists():
        return {}
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt state file must not stop the tool working. Worst case we
        # review something twice, which is better than refusing to start.
        return {}


def _key(repository: str, pr_number: int, head_sha: str) -> str:
    return f"{repository}#{pr_number}@{head_sha}"


def already_reviewed(repository: str, pr_number: int, head_sha: str) -> bool:
    """True if this exact commit has been reviewed already."""
    if not head_sha:
        return False
    return _key(repository, pr_number, head_sha) in _load()


def save(review: Review) -> None:
    """Record a finished review. Never raises - losing the record must not
    lose a review that has already been posted."""
    try:
        data = _load()
        data[_key(review.repository, review.pr_number, review.head_sha)] = {
            "review_id": review.review_id,
            "status": review.status,
            "findings": len(review.findings),
            "created_at": review.created_at,
        }
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"  (warning: could not save state: {exc})")
