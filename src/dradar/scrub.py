"""Secret handling for uploads.

Two artifact classes, two mechanisms (design doc):

- **Integrity-critical** (model.patch): its bytes must match the target repo
  exactly or `git apply` fails during grading. NEVER rewrite it. Instead
  DETECT secrets and reject/quarantine on a hit (`scan_secrets`). Detection is
  high-precision so a legitimate patch (editing a lockfile hash, a fixture
  email) is not falsely rejected.

- **Display** (trajectory.json, result.json): shown in the public viewer, so
  redact destructively (`scrub_bytes`). Over-redaction is acceptable here.

Both run client-side before upload AND server-side before storage. Neither
path ever bypasses on non-UTF-8 input: bytes are decoded with
``surrogateescape`` so arbitrary bytes round-trip while ASCII-shaped secrets
are still caught.
"""

import re
from pathlib import Path

# High-precision credential shapes — used to DETECT secrets in a patch. Kept
# conservative: every entry matches something that is unambiguously a
# credential, never ordinary source content, so scan_secrets does not falsely
# reject a valid patch. (label, pattern)
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SK-ANT", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),  # before generic sk-
    ("SK", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}")),
    ("GHP", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("GH-PAT", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # ChatGPT/Codex OAuth session blobs (Fernet-style, opaque, no key= label).
    ("FERNET", re.compile(r"gAAAAA[A-Za-z0-9_-]{40,}")),
    ("BEARER", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    ("AUTH-HEADER", re.compile(r"(?i)authorization[\"']?\s*[:=]\s*[\"']?[^\s\"']{12,}")),
    ("KEY-ASSIGN", re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)"
        r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{16,}=*"
    )),
]

# Destructive rewrites for DISPLAY artifacts only. (pattern, replacement).
# Reuses the secret patterns plus PII that is fine to redact in a trajectory.
_SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), "[REDACTED-SK-ANT]"),
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}"), "[REDACTED-SK]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED-GHP]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED-GH-PAT]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED-JWT]"),
    (re.compile(r"gAAAAA[A-Za-z0-9_-]{40,}"), "[REDACTED-TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{20,}=*"), r"\1[REDACTED-BEARER]"),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)[^\s\"']{12,}"), r"\1[REDACTED-AUTH]"),
    (re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)"
        r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/-]{16,}=*"
    ), r"\1[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED-EMAIL]"),
]

# Home-dir paths reveal local usernames; container paths (/app, /logs) are fine.
_HOME_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")


def _decode(data: bytes) -> str:
    # surrogateescape round-trips arbitrary bytes: no scrub/scan bypass on
    # non-UTF-8 input, and re-encoding reproduces the original bytes exactly.
    return data.decode("utf-8", errors="surrogateescape")


def scan_secrets(data: bytes) -> list[str]:
    """Return the labels of any credential shapes found. Non-empty => the
    caller (patch handler) should reject/quarantine rather than store."""
    text = _decode(data)
    return [label for label, pat in _SECRET_PATTERNS if pat.search(text)]


def scrub_text(text: str) -> str:
    for pat, repl in _SCRUB_PATTERNS:
        text = pat.sub(repl, text)
    return _HOME_RE.sub("/[HOME]", text)


def scrub_bytes(data: bytes) -> bytes:
    """Destructively redact a display artifact. Never bypasses: arbitrary
    bytes round-trip via surrogateescape while ASCII secrets are redacted."""
    return scrub_text(_decode(data)).encode("utf-8", errors="surrogateescape")


def scrub_file(source: Path, target: Path) -> None:
    """Scrub a display artifact from source into target (target dir must exist)."""
    target.write_bytes(scrub_bytes(source.read_bytes()))
