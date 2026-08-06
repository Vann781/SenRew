"""Data models. Plain dataclasses, no network or API imports."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
import hashlib
import uuid

CATEGORIES = ("bug", "security", "performance", "style", "maintainability")
IMPACT_LEVELS = ("none", "minor", "moderate", "major", "severe")
LIKELIHOOD_LEVELS = ("rare", "unlikely", "possible", "likely", "certain")
BLAST_RADIUS_LEVELS = ("single_line", "single_function", "single_file", "module", "system")
SEVERITY_BANDS = ("critical", "high", "medium", "low")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Finding:
    """One problem the agent found."""

    file_path: str
    line: int
    category: str
    title: str
    explanation: str

    # What the model judged. The severity number is computed from these.
    impact: str = "moderate"
    likelihood: str = "possible"
    blast_radius: str = "single_function"
    suggested_fix: str = ""

    # Filled in by severity.py. The model never supplies a number.
    severity_score: float = 0.0
    severity_band: str = "low"

    # Filled in by the verifier.
    verdict: str = "unchecked"  # confirmed | rejected | unchecked
    verdict_reason: str = ""

    # Short id the verifier uses to refer back to this finding.
    finding_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def fingerprint(self) -> str:
        """Stable id for this problem, so it is not reported twice."""
        raw = f"{self.file_path}|{self.category}|{self.title}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Review:
    """One complete review of one pull request."""

    repository: str
    pr_number: int
    head_sha: str

    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    status: str = "completed"  # completed | failed | skipped

    # Evidence the agent checked its own work.
    candidates: int = 0
    rejected: int = 0

    # Coverage: which changed files the agent actually opened.
    files_changed: int = 0
    files_reviewed: int = 0
    files_missed: list[str] = field(default_factory=list)
    files_unreviewable: list[dict] = field(default_factory=list)

    steps: int = 0
    cost_usd: float = 0.0
    error: str = ""
    created_at: str = field(default_factory=utc_now)
    review_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def counts_by_band(self) -> dict[str, int]:
        counts = {band: 0 for band in SEVERITY_BANDS}
        for finding in self.findings:
            if finding.severity_band in counts:
                counts[finding.severity_band] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
