"""Invariant extraction — the safety net.

An invariant is a piece of information that MUST survive compression:
numbers, file paths, URLs, model IDs, versions, ports, hashes, explicit constraints.

These are extracted deterministically (regex + heuristics), never by an LLM,
so the verifier can prove coverage.

Every sub-pattern is defined EXACTLY ONCE as a module constant. The public
``PATTERNS`` dict and the private combined scanner are both built from those
constants, so the two can never diverge again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Sub-pattern constants (single source of truth)
# ---------------------------------------------------------------------------

# URL: scheme + everything that is not whitespace / quote / angle bracket / "]".
# Parentheses ARE allowed inside; trailing punctuation is stripped afterwards.
_URL = r"https?://[^\s'\"<>\]`]+"

# Path — several shapes. Order matters inside the alternation.
_PATH_WIN = r"(?<![\w])[A-Za-z]:[\\/][^\s'\"<>|\r\n]+"
_PATH_UNC = r"(?<![\w])\\\\[^\s'\"<>|\r\n]+"
_PATH_UNIX = r"(?<![\w~])/(?:[^\s'\"<>|\r\n/]+/)+[^\s'\"<>|\r\n/]+"
_PATH_TILDE = r"(?<![\w])~/(?:[^\s'\"<>|\r\n/]+/)*[^\s'\"<>|\r\n/]+"
_PATH_DOT = r"(?<![\w])\.\.?/(?:[^\s'\"<>|\r\n/]+/)*[^\s'\"<>|\r\n/]+"
# Relative path: needs >= 1 slash AND a file extension on the last segment.
_PATH_REL = (
    r"(?<![\w])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*\.[A-Za-z]\w{0,7}(?![\w])"
)
_PATH = "(?:" + "|".join(
    (_PATH_WIN, _PATH_UNC, _PATH_UNIX, _PATH_TILDE, _PATH_DOT, _PATH_REL)
) + ")"

# IPv4 with optional CIDR. Octets are range-validated in code.
_IP = r"(?<![\w.])\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?(?![\w.])"

# Version: "v"-prefixed with >= 2 parts, OR >= 3 parts without prefix.
# Two-part plain decimals (94.2, 0.5) are numbers, not versions.
_VERSION = (
    r"(?<![\w])(?:v\d+(?:\.\d+){1,3}(?!\.\d)|\d+(?:\.\d+){2,3}(?!\.\d))(?![\w])"
)

# Hex hash: 7..64 hex chars, must contain >= 1 digit AND >= 1 a-f letter.
# The lookaheads keep a pure-digit run (epoch, id) out of this category.
_HEX_HASH = (
    r"(?<![\w])(?=[0-9a-f]{7,64}(?![0-9a-f]))(?=[0-9a-f]{0,63}[a-f])"
    r"[0-9a-f]{7,64}(?![\w])"
)

# Port: the word "port", optional separator, the number.
_PORT = r"(?<![\w])(?i:port)\s*[=:]?\s*\d{1,5}(?![\w])"

# Model id: org/name or org/name/sub, optional ":tag" suffix.
_MODEL_ID = (
    r"(?<![\w./-])[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?(?::[A-Za-z0-9][A-Za-z0-9._-]*)?(?![\w])"
)

# Env var: must contain "_" or be immediately followed by "=".
# ERROR / INFO / TODO / WARNING / HTTP / JSON are therefore NOT env vars.
_ENV_VAR = (
    r"(?<![\w])(?:[A-Z][A-Z0-9]*_[A-Z0-9_]*|[A-Z][A-Z0-9_]{3,}(?=\s*=))(?![\w])"
)

# Number: optional sign, digits, optional fraction. A trailing sentence period
# is allowed ("after 3." -> 3) but "1.2.3" must not become "1.2".
_NUMBER = r"(?<![\w.])-?\d+(?:\.\d+)?(?!\.\d)(?![\w])"

# Timestamps: matched ONLY to be consumed. Their digit groups (2026, 10, 24, 09,
# 12, 03...) are fragments, not facts; as separate numbers they flooded the anchor.
_DATETIME = (
    r"(?<![\w.])(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?|\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?![\w])"
)

# Constraint keywords. Longest alternatives first so "must not" wins over "must".
_CONSTRAINT_KEYWORDS = re.compile(
    r"\b(?:do not|don't|must not|never|avoid|refuse|always|required|obligatory|"
    r"blocking|blocker|pending|awaiting|exact|verbatim|byte-identical|must)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public pattern dict — built from the constants above (never hand-written)
# ---------------------------------------------------------------------------

PATTERNS: dict[str, re.Pattern[str]] = {
    "url": re.compile(_URL),
    "path": re.compile(_PATH),
    "ip": re.compile(_IP),
    "version": re.compile(_VERSION),
    "hex_hash": re.compile(_HEX_HASH),
    "port": re.compile(_PORT),
    "model_id": re.compile(_MODEL_ID),
    "env_var": re.compile(_ENV_VAR),
    "number": re.compile(_NUMBER),
    "datetime": re.compile(_DATETIME),
}

# One pass for all structured values instead of one pass per pattern.
# Alternation order decides the winner at a given start position.
_COMBINED = re.compile(
    "(?P<url>" + _URL + ")"
    "|(?P<path>" + _PATH + ")"
    "|(?P<ip>" + _IP + ")"
    "|(?P<version>" + _VERSION + ")"
    "|(?P<hex_hash>" + _HEX_HASH + ")"
    "|(?P<port>" + _PORT + ")"
    "|(?P<model_id>" + _MODEL_ID + ")"
    "|(?P<env_var>" + _ENV_VAR + ")"
    "|(?P<datetime>" + _DATETIME + ")"
    "|(?P<number>" + _NUMBER + ")"
)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_MODEL_ID_STOPLIST = {
    "and/or", "tcp/ip", "i/o", "w/o", "n/a", "yes/no", "true/false",
    "input/output", "output/input", "on/off", "up/down", "min/max",
}
_CONTENT_TYPE_PREFIXES = {
    "application", "text", "image", "audio", "video", "multipart",
}
_DATE_MODEL = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")
_CIDR_MODEL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
_EXT_RE = re.compile(r"\.[A-Za-z]\w{0,7}$")
_TRAILING_PUNCT = ".,;:!?"


def _strip_url_tail(value: str) -> str:
    """Trim trailing punctuation and unbalanced ')' from a URL."""
    value = value[:2000]
    prev = None
    while value and value != prev:
        prev = value
        value = value.rstrip(_TRAILING_PUNCT)
        while value.endswith(")") and value.count(")") > value.count("("):
            value = value[:-1]
            value = value.rstrip(_TRAILING_PUNCT)
    return value


def _valid_ip(value: str) -> bool:
    """IPv4 with optional /CIDR; octets must be 0-255."""
    net = value.split("/", 1)[0]
    parts = net.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        if int(part) > 255:
            return False
    if "/" in value:
        try:
            cidr = int(value.split("/", 1)[1])
        except ValueError:
            return False
        if cidr > 32:
            return False
    return True


def _valid_port(value: str) -> bool:
    """The value is the full match, e.g. 'port 8080'. The number must be 1-65535."""
    digits = re.findall(r"\d+", value)
    if not digits:
        return False
    try:
        port = int(digits[-1])
    except ValueError:
        return False
    return 1 <= port <= 65535


def _valid_model_id(value: str) -> bool:
    """Reject stoplist entries, content types, dates, CIDRs and digit-only parts."""
    low = value.lower()
    if low in _MODEL_ID_STOPLIST:
        return False
    parts = value.split("/")
    if parts[0].lower() in _CONTENT_TYPE_PREFIXES:
        return False
    if _DATE_MODEL.match(value) or _CIDR_MODEL.match(value):
        return False
    for part in parts:
        if not part:
            return False
        if not any(ch.isalpha() for ch in part):
            return False
    return True


def _post_process(kind: str, value: str) -> str | None:
    """Normalize a raw match. Returns None when the match must be dropped."""
    value = value.strip()
    if not value:
        return None
    if kind == "url":
        value = _strip_url_tail(value)
        return value or None
    if kind == "ip":
        return value if _valid_ip(value) else None
    if kind == "port":
        return value if _valid_port(value) else None
    if kind == "model_id":
        return value if _valid_model_id(value) else None
    if kind == "path":
        # A relative path must carry a file extension; otherwise it is a model id.
        if (
            "/" in value
            and not _EXT_RE.search(value)
            and not value.startswith(("/", "~", ".", "\\"))
            and not re.match(r"^[A-Za-z]:[\\/]", value)
        ):
            return None
        return value
    return value


# ---------------------------------------------------------------------------
# Invariant
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Invariant:
    """One must-survive fact, with the exact string used for verification."""

    kind: str
    value: str
    priority: int = 1  # 3 = critical (constraint), 2 = structured, 1 = numeric

    def key(self) -> str:
        return f"{self.kind}:{self.value}"


# ---------------------------------------------------------------------------
# Constraint extraction
# ---------------------------------------------------------------------------

def _constraint_value(text: str, start: int) -> str:
    """Return the constraint text starting at `start`, up to newline/sentence end.

    A dot only ends the constraint when it is followed by whitespace or the end
    of the text. Dots inside tokens (config.yaml, v1.2) are preserved.
    """
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            break
        if ch in ".!?;":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "" or nxt.isspace():
                break
        i += 1
    raw = text[start:i]
    return raw.strip().strip(_TRAILING_PUNCT + " \t")


def _extract_constraints(text: str) -> list[tuple[str, int]]:
    """Return [(value, priority)] for every explicit constraint found."""
    out: list[tuple[str, int]] = []
    pos = 0
    while True:
        match = _CONSTRAINT_KEYWORDS.search(text, pos)
        if match is None:
            break
        value = _constraint_value(text, match.start())
        if value:
            out.append((value[:200].strip(), 3))
            pos = match.start() + len(value)
        else:
            pos = match.end()
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract(text: str, limit: int | None = None) -> list[Invariant]:
    """Extract all invariants from a text, deduplicated.

    Args:
        text: source content
        limit: stop after this many invariants (None = no limit). The anchor
            builder may use it on huge inputs, but the verifier must NOT, or it
            would prove coverage of a truncated set.
    """
    found: dict[str, Invariant] = {}

    for value, priority in _extract_constraints(text):
        found.setdefault(f"constraint:{value}", Invariant("constraint", value, priority))
        if limit and len(found) >= limit:
            return sorted(found.values(), key=lambda i: (-i.priority, i.kind, i.value))

    for match in _COMBINED.finditer(text):
        kind = match.lastgroup or "value"
        if kind == "datetime":
            continue  # consumed so its digits never become number invariants
        value = _post_process(kind, match.group(0))
        if not value:
            continue
        priority = 1 if kind == "number" else 2
        found.setdefault(f"{kind}:{value}", Invariant(kind, value, priority))
        if limit and len(found) >= limit:
            break

    return sorted(found.values(), key=lambda i: (-i.priority, i.kind, i.value))


# ---------------------------------------------------------------------------
# Coverage — boundary aware
# ---------------------------------------------------------------------------

def _present(value: str, kind: str, haystack: str) -> bool:
    """Boundary-aware presence test.

    A hit is accepted only when the characters immediately before and after the
    match are not word characters. For numeric kinds the match must also not be
    adjacent to a dot followed by a digit ("5" is not in "15" nor in "1.5").
    """
    if not value:
        return False
    needle_len = len(value)
    start = 0
    while True:
        idx = haystack.find(value, start)
        if idx < 0:
            return False
        end = idx + needle_len
        ok = True
        if idx > 0:
            prev = haystack[idx - 1]
            if prev.isalnum() or prev == "_":
                ok = False
        if end < len(haystack):
            nxt = haystack[end]
            if nxt.isalnum() or nxt == "_":
                ok = False
        if ok and kind in ("number", "version", "ip", "port"):
            if idx > 1 and haystack[idx - 1] == "." and haystack[idx - 2].isdigit():
                ok = False
            if end + 1 < len(haystack) and haystack[end] == "." and haystack[end + 1].isdigit():
                ok = False
        if ok:
            return True
        start = idx + 1


# Public alias — ctx_verify reuses it.
present = _present


def _index_haystack(haystack: str) -> dict[str, set[str]]:
    """Build {kind: {values}} from a single extraction pass."""
    index: dict[str, set[str]] = {}
    for inv in extract(haystack):
        index.setdefault(inv.kind, set()).add(inv.value)
    return index


# Above this haystack size the extract-based index is used instead of one scan
# per invariant (measured in P1, see REPAIR_REPORT.md).
_FAST_HAYSTACK_CHARS = 50_000


def coverage(invariants: list[Invariant], haystack: str) -> dict:
    """Check which invariants are present in the given text.

    Two coverage numbers are reported because they answer different questions:

      coverage          raw fraction of all invariants present
      coverage_weighted fraction weighted by priority (constraint=3, structured=2,
                        number=1). A missing "never use basic auth" matters far more
                        than a missing table row count, so the weighted number is the
                        one that reflects real context quality.
    """
    use_index = len(haystack) > _FAST_HAYSTACK_CHARS
    index = _index_haystack(haystack) if use_index else None

    def is_present(inv: Invariant) -> bool:
        if index is not None and inv.kind != "constraint":
            return inv.value in index.get(inv.kind, set())
        return _present(inv.value, inv.kind, haystack)

    missing = [i for i in invariants if not is_present(i)]
    critical = [i for i in invariants if i.priority == 3]
    critical_missing = [i for i in critical if not is_present(i)]
    # Structured values (paths, ports, versions, URLs) are the ones a model actually
    # reasons about. Plain numbers are counted but weighted far less.
    structured = [i for i in invariants if i.priority >= 2]
    structured_missing = [i for i in structured if not is_present(i)]
    total = len(invariants)
    weight_total = sum(i.priority for i in invariants)
    weight_present = sum(i.priority for i in invariants if is_present(i))
    return {
        "total": total,
        "present": total - len(missing),
        "missing": [{"kind": i.kind, "value": i.value, "priority": i.priority} for i in missing],
        "critical_missing": len(critical_missing),
        "coverage": round((total - len(missing)) / total, 4) if total else 1.0,
        "coverage_weighted": round(weight_present / weight_total, 4) if weight_total else 1.0,
        "coverage_structured": (
            round((len(structured) - len(structured_missing)) / len(structured), 4)
            if structured
            else 1.0
        ),
        "structured_total": len(structured),
        "lossless": not missing,
    }
