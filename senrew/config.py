"""All configuration in one place, read from environment variables.

A .env file in the project root is loaded if present. Real environment
variables always win, so exporting a value in your shell overrides the file
without editing it.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Read key=value pairs from .env. Hand-rolled to avoid a dependency."""
    path = ROOT / ".env"
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


_load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() == "true"


# --- Model -----------------------------------------------------------------

GEMINI_API_KEY = _get("GEMINI_API_KEY")

# Verified against the live API (models.list plus a real generateContent call
# with tools). Model names change and old ones get retired.
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-3.5-flash")
MAX_OUTPUT_TOKENS = _get_int("MAX_OUTPUT_TOKENS", 4096)
TEMPERATURE = float(_get("TEMPERATURE", "0"))
MAX_RETRIES = _get_int("MAX_RETRIES", 3)

# The single most important safety valve in this project. An agent loop with
# no ceiling will happily keep calling tools until your quota is gone.
MAX_STEPS = _get_int("MAX_STEPS", 12)

# Free Gemini keys allow only a handful of requests per MINUTE, and an agent
# spends one per step - so a real review will sit and wait more than once.
# Google says how long to wait; these just bound how often we obey it.
RATE_LIMIT_RETRIES = _get_int("RATE_LIMIT_RETRIES", 6)
RATE_LIMIT_MAX_WAIT = _get_int("RATE_LIMIT_MAX_WAIT", 75)

# Canned model output instead of a real call. Lets someone run the demo
# before they have an API key.
USE_FAKE_MODEL = _get_bool("USE_FAKE_MODEL")

# Rough Gemini pricing, USD per million tokens, for the cost line only.
PRICE_INPUT_PER_MTOK = float(_get("PRICE_INPUT_PER_MTOK", "1.50"))
PRICE_OUTPUT_PER_MTOK = float(_get("PRICE_OUTPUT_PER_MTOK", "9.00"))


# --- GitHub ----------------------------------------------------------------

GITHUB_TOKEN = _get("GITHUB_TOKEN")
GITHUB_API = "https://api.github.com"


# --- Tool limits -----------------------------------------------------------
# Uncapped tool output is the other way an agent loop gets expensive: one
# 5000-line file read lands in the context of every later step.

MAX_FILE_LINES = _get_int("MAX_FILE_LINES", 400)
MAX_SEARCH_RESULTS = _get_int("MAX_SEARCH_RESULTS", 20)
MAX_TOOL_CHARS = _get_int("MAX_TOOL_CHARS", 12000)


# --- Review behaviour ------------------------------------------------------

MIN_SEVERITY_TO_POST = _get("MIN_SEVERITY_TO_POST", "low")
MAX_COMMENTS_PER_REVIEW = _get_int("MAX_COMMENTS_PER_REVIEW", 15)
VERIFY = _get_bool("VERIFY", True)


# --- Watcher ---------------------------------------------------------------

WATCH_INTERVAL_SECONDS = _get_int("WATCH_INTERVAL_SECONDS", 15)
STATE_DIR = Path(_get("SENREW_STATE_DIR", str(Path.home() / ".senrew")))
