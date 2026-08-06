"""Severity scoring.

The model never gives us a severity number. It gives labels - how bad the
problem is (impact) and whether it will actually happen (likelihood) - and we
compute the number here.

Three things follow. The same bug always scores the same. We can tune it by
changing one number instead of rewriting a prompt. And we can show the exact
arithmetic to anyone who disagrees with a score.
"""

from senrew.models import Finding, SEVERITY_BANDS

IMPACT_WEIGHT = {"none": 0, "minor": 15, "moderate": 40, "major": 70, "severe": 100}

LIKELIHOOD_WEIGHT = {
    "rare": 0.2, "unlikely": 0.4, "possible": 0.6, "likely": 0.8, "certain": 1.0,
}

# How far the problem reaches.
RADIUS_MULTIPLIER = {
    "single_line": 0.85, "single_function": 1.00, "single_file": 1.05,
    "module": 1.15, "system": 1.30,
}

CATEGORY_MULTIPLIER = {
    "security": 1.25, "bug": 1.15, "performance": 1.00,
    "maintainability": 0.85, "style": 0.60,
}

# The same bug in an auth or payment file is worse than elsewhere.
SECURITY_SENSITIVE_WORDS = (
    "auth", "login", "session", "token", "password", "crypto",
    "payment", "billing", "admin", "permission", "migration",
)

SECURITY_PATH_MULTIPLIER = 1.20
TEST_FILE_MULTIPLIER = 0.70

BAND_THRESHOLDS = (("critical", 80.0), ("high", 60.0), ("medium", 35.0), ("low", 0.0))


def is_security_sensitive(file_path: str) -> bool:
    """True if the path suggests security or money is involved."""
    lowered = file_path.lower()
    return any(word in lowered for word in SECURITY_SENSITIVE_WORDS)


def is_test_file(file_path: str) -> bool:
    """True if the file looks like a test."""
    lowered = file_path.lower()
    return (
        "/test" in lowered
        or lowered.startswith("test")
        or "_test." in lowered
        or ".test." in lowered
        or ".spec." in lowered
    )


def band_for_score(score: float) -> str:
    """Turn a 0-100 score into a severity band."""
    for band, threshold in BAND_THRESHOLDS:
        if score >= threshold:
            return band
    return "low"


def compute_score(
    impact: str,
    likelihood: str,
    category: str,
    file_path: str,
    blast_radius: str = "single_function",
) -> float:
    """Calculate a 0-100 severity score from the model's labels.

    Unknown labels fall back to middle values rather than crashing, because
    models occasionally return something we did not offer.
    """
    score = IMPACT_WEIGHT.get(impact, 40) * LIKELIHOOD_WEIGHT.get(likelihood, 0.6)
    score *= RADIUS_MULTIPLIER.get(blast_radius, 1.0)
    score *= CATEGORY_MULTIPLIER.get(category, 1.0)

    if is_security_sensitive(file_path):
        score *= SECURITY_PATH_MULTIPLIER
    if is_test_file(file_path):
        score *= TEST_FILE_MULTIPLIER

    return round(min(max(score, 0.0), 100.0), 1)


def score_finding(finding: Finding) -> Finding:
    """Fill in severity_score and severity_band."""
    finding.severity_score = compute_score(
        finding.impact, finding.likelihood, finding.category,
        finding.file_path, finding.blast_radius,
    )
    finding.severity_band = band_for_score(finding.severity_score)
    return finding


def score_all(findings: list[Finding]) -> list[Finding]:
    """Score every finding and return them worst-first."""
    for finding in findings:
        score_finding(finding)
    return sorted(findings, key=lambda f: f.severity_score, reverse=True)


def meets_minimum(finding: Finding, minimum_band: str) -> bool:
    """True if this finding is at least as serious as the minimum band."""
    order = list(SEVERITY_BANDS)  # critical, high, medium, low
    try:
        return order.index(finding.severity_band) <= order.index(minimum_band)
    except ValueError:
        return True
