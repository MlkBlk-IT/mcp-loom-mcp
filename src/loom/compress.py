"""Lossless compression — moves, never summarizes.

Two operations, both non-destructive:
  1. Tool-output cleansing: keep exit codes, diffs, exact values, paths; drop repetitive
     stdout noise. The dropped text still exists in the drawer, retrievable byte-identical.
  2. Addressable history: the bulky part becomes an anchor + IDs; the raw bytes stay in
     the store.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from loom import anchors as anchor_mod
from loom.profile import Profile, get_profile

# Cheap, high-signal markers. Plain substrings — no regex per line.
KEEP_MARKERS = (
    "exit_code",
    "exit code",
    "exit status",
    "exitcode",
    "traceback",
    "error",
    "exception",
    "fatal",
    "critical",
    "warning",
    "diff --git",
    "--- a/",
    "+++ b/",
    "created",
    "modified",
    "deleted",
    "renamed",
    "failed",
    "denied",
    "timeout",
)

# Lines that changed in a diff (+/-).
KEEP_LINE = re.compile(r"^[+-]\s+\S")
CONSTRAINT_HINT = re.compile(
    r"(?i)\b(?:never|do not|don't|must not|avoid|refuse|always|required|must|"
    r"blocking|blocker|exact|verbatim|byte-identical)\b"
)

MAX_ANCHOR_LINES = 400
# A fact longer than this is clipped in the anchor (full line stays in the drawer)
# and scored lower: very long lines are low information density.
MAX_FACT_CHARS = 300
LINES_PER_CHUNK = 10
MAX_CHUNK_BYTES = 16 * 1024  # 16 KiB — a retrieval unit must stay small enough to re-read
MAX_INVARIANTS_IN_ANCHOR = 200
MAX_NUMBERS_IN_ANCHOR = 30
# Rare, cheap, high-value kinds: reserved a slice of the budget so a few hundred
# paths/env vars (sorted before them by kind name) can never starve them.
RARE_KINDS = ("port", "ip", "url", "hex_hash")
RARE_SHARE = 0.10

# Share of the anchor budget reserved per invariant priority. Constraints are the
# must-keep section; numbers are the least valuable per character.
PRIORITY_BUDGET = {3: 0.45, 2: 0.40, 1: 0.15}


def _split_units(content: str) -> list[dict[str, Any]]:
    """Split content into retrievable units.

    GUARANTEE: concatenating the units reproduces `content` byte-for-byte.
    Split is done on line boundaries with a fixed chunk size, so retrieval is granular
    (a log becomes hundreds of addressable pieces, not one blob).
    """
    if not content:
        return []
    if "\n" not in content:
        return [{"text": content}]

    lines = content.splitlines(keepends=True)
    units: list[dict[str, Any]] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        buf.append(line)
        # Size is measured in UTF-8 BYTES, not characters: a chunk must stay
        # under the limit on disk, and UTF-8 can use up to 4 bytes per char.
        size += len(line.encode("utf-8"))
        # Split on line count OR byte size, whichever comes first. A log with very long
        # lines (JSON blobs) would otherwise produce multi-MB chunks that are useless
        # as retrieval units.
        if len(buf) >= LINES_PER_CHUNK or size >= MAX_CHUNK_BYTES:
            units.append({"text": "".join(buf)})
            buf = []
            size = 0
    if buf:
        units.append({"text": "".join(buf)})

    if not units:
        units = [{"text": content}]
    # Safety net: if the split lost anything, fall back to the whole blob.
    if "".join(u["text"] for u in units) != content:
        units = [{"text": content}]
    return units


def _score_line(line: str) -> int:
    """Cheap relevance score for anchor selection. Higher = more likely a fact."""
    low = line.lower()
    score = 0
    for marker in KEEP_MARKERS:
        if marker in low:
            score += 3
    if KEEP_LINE.match(line):
        score += 3
    if CONSTRAINT_HINT.search(line):
        score += 5
    if anchor_mod.PATTERNS["path"].search(line):
        score += 2
    if anchor_mod.PATTERNS["url"].search(line):
        score += 2
    if anchor_mod.PATTERNS["version"].search(line):
        score += 1
    return score


def _clip_fact(line: str) -> str:
    return line if len(line) <= MAX_FACT_CHARS else line[: MAX_FACT_CHARS - 1] + "\u2026"


def _fit_facts(facts: list[tuple[int, str]], budget_chars: int) -> list[str]:
    """Keep the highest-scoring facts that fit the budget, returned in original order.

    `budget_chars == 0` means UNLIMITED. Selection is by score (ties: earliest
    first), NOT by position: a FAILED line near the end must beat a PASSED line
    near the top.
    """
    if not budget_chars:
        return [text for _, text in facts]
    order = sorted(range(len(facts)), key=lambda i: (-facts[i][0], i))
    keep: set[int] = set()
    used = 0
    for i in order:
        cost = len(facts[i][1]) + 3
        if used + cost > budget_chars:
            continue
        keep.add(i)
        used += cost
    return [facts[i][1] for i in sorted(keep)]


def _fit_lines(lines: list[str], budget_chars: int) -> list[str]:
    """Take lines in order while they fit in the budget, and report the rest."""
    if not budget_chars:
        return lines
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 3 > budget_chars:
            break
        kept.append(line)
        used += len(line) + 3
    return kept


def _select_invariants(invariants: list[anchor_mod.Invariant], budget_chars: int) -> list[str]:
    """Pick the invariants that fit in the anchor, highest priority first.

    `budget_chars == 0` means UNLIMITED (same convention as `_fit_lines`).
    Every invariant that is not listed — whether dropped by the budget or by
    MAX_INVARIANTS_IN_ANCHOR — is counted in the "N more invariants" note, and
    that note is itself kept inside the budget.
    """
    if not invariants:
        return []

    unlimited = not budget_chars
    if not unlimited:
        # Plain numbers are the lowest-value invariants (latencies, ids, counts). Cap
        # them and prefer the longest (most specific); the rest stay in the drawer.
        others = [i for i in invariants if i.kind != "number"]
        nums = sorted(
            (i for i in invariants if i.kind == "number"),
            key=lambda i: (-len(i.value), i.value),
        )[:MAX_NUMBERS_IN_ANCHOR]
        keep_ids = {id(i) for i in others + nums}
        invariants = [i for i in invariants if id(i) in keep_ids] + [
            i for i in invariants if id(i) not in keep_ids
        ]
        capped = len(invariants) - len(keep_ids)
        invariants = invariants[: len(keep_ids)]
    else:
        capped = 0
    lines: list[str] = []
    used = 0
    for inv in invariants:
        if not unlimited and len(lines) >= MAX_INVARIANTS_IN_ANCHOR:
            break
        line = f"- [{inv.kind}] {inv.value}"
        if not unlimited and used + len(line) + 1 > budget_chars:
            continue
        lines.append(line)
        used += len(line) + 1

    dropped = len(invariants) + capped - len(lines)
    if dropped:
        note = f"- ... {dropped} more invariants in the drawer (retrieve with ctx_get)"
        if not unlimited:
            # The note must fit: trim invariant lines until it does.
            while lines and used + len(note) + 1 > budget_chars:
                last = lines.pop()
                used -= len(last) + 1
                dropped += 1
                note = f"- ... {dropped} more invariants in the drawer (retrieve with ctx_get)"
        lines.append(note)
    return lines


def _truncate_sections(anchor: str, max_chars: int) -> str:
    """Cut an over-long anchor without losing a whole section.

    A flat `anchor[:max_chars]` used to be able to drop the entire Facts section
    (or the tail of Invariants). Here the anchor is cut line by line, and a
    section that would be emptied is replaced by an explicit pointer to the
    drawer instead of vanishing. The "... N more X" notes are counted INSIDE the
    budget: they are what tells the model the drawer still holds something.
    """
    if len(anchor) <= max_chars:
        return anchor

    lines = anchor.split("\n")
    kept: list[str] = []
    used = 0
    current_section = ""
    dropped_in_section: dict[str, int] = {}

    for line in lines:
        if line.startswith("**") and line.endswith("**"):
            current_section = line.strip("*")
            dropped_in_section.setdefault(current_section, 0)
        if used + len(line) + 1 > max_chars:
            if current_section:
                dropped_in_section[current_section] = dropped_in_section.get(current_section, 0) + 1
            continue
        kept.append(line)
        used += len(line) + 1

    notes = [
        f"- ... {n} more {sec.lower()} in the drawer (retrieve with ctx_get)"
        for sec, n in dropped_in_section.items()
        if n > 0
    ]
    if not notes:
        return "\n".join(kept)

    # The notes must fit too: drop the last kept lines until they do.
    notes_len = sum(len(n) + 1 for n in notes)
    while kept and used + notes_len > max_chars:
        last = kept.pop()
        used -= len(last) + 1
    kept.extend(notes)
    return "\n".join(kept)


def build_anchor(
    content: str, profile: Profile | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Build the in-context anchor and the retrievable chunks.

    Returns:
        (anchor_text, chunks) — chunks hold every byte of the original content.
        Each chunk additionally carries "ref" (a content-addressed pointer
        "<sha12>#<index>") and "source_sha" (the full sha256 of the content).
    """
    profile = profile or get_profile()
    units = _split_units(content)

    # Single invariant pass over the whole content (not per line).
    invariants = anchor_mod.extract(content)

    # Line selection: score every line once, keep the best ones.
    scored: list[tuple[int, str]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        s = _score_line(line)
        if len(line) > MAX_FACT_CHARS:
            s = (s + 1) // 2
        if s > 0:
            scored.append((s, _clip_fact(line.strip())))

    # Keep the highest-scoring lines, in original order, up to the cap.
    limit = MAX_ANCHOR_LINES
    if profile.max_anchor_chars:
        limit = min(limit, max(20, profile.max_anchor_chars // 12))
    chosen_idx = sorted(range(len(scored)), key=lambda i: (-scored[i][0], i))[:limit]
    chosen = set(chosen_idx)
    facts = [scored[i] for i in range(len(scored)) if i in chosen]

    # --- footer (built FIRST, never truncated) -------------------------------
    # The footer is the pointer to the drawer. It is deterministic and
    # DB-independent: same content + same profile => same bytes, so the anchor
    # can sit in a stable prompt prefix (prefix-cache friendly).
    from loom.tokenizer import count_tokens

    source_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    sha12 = source_sha[:12]
    for i, unit in enumerate(units):
        unit["ref"] = f"{sha12}#{i}"
        unit["source_sha"] = source_sha
        unit["idx"] = i
    total_tokens = count_tokens(content)
    footer = (
        f"drawer: {len(units)} chunks, ~{total_tokens} tokens total; "
        f"refs {sha12}#0..{sha12}#{max(0, len(units) - 1)}; "
        "retrieve with ctx_get(ref) or ctx_search(query)"
    )

    # --- budget --------------------------------------------------------------
    # The footer is reserved FIRST, then the rest is split between Invariants
    # (55%) and Facts (45%).
    # NOTE: a budget of 0 means UNLIMITED for both _select_invariants and
    # _fit_lines, so a real (but tiny) budget must never be expressed as 0.
    budget = profile.max_anchor_chars or 0
    rare_inv = [i for i in invariants if i.kind in RARE_KINDS]
    rare_budget = 0
    if budget:
        body_budget = max(0, budget - len(footer) - 1)
        inv_budget = max(1, int(body_budget * 0.55))
        if rare_inv:
            need = sum(len(i.kind) + len(i.value) + 6 for i in rare_inv) + 1
            rare_budget = min(need, int(body_budget * RARE_SHARE))
        fact_budget = max(1, body_budget - inv_budget - rare_budget)
    else:
        body_budget = 0
        inv_budget = 0
        fact_budget = 0

    anchor_parts: list[str] = []
    if invariants:
        anchor_parts.append("**Invariants**")
        if rare_budget:
            anchor_parts.extend(_select_invariants(rare_inv, rare_budget))
            rest = [i for i in invariants if i.kind not in RARE_KINDS]
            anchor_parts.extend(_select_invariants(rest, inv_budget))
        else:
            anchor_parts.extend(_select_invariants(invariants, inv_budget))
    if facts:
        anchor_parts.append("**Facts**")
        kept_facts = _fit_facts(facts, fact_budget)
        anchor_parts.extend(f"- {f}" for f in kept_facts)
        if len(kept_facts) < len(facts):
            anchor_parts.append(
                f"- ... {len(facts) - len(kept_facts)} more facts in the drawer "
                "(retrieve with ctx_get)"
            )

    anchor = "\n".join(anchor_parts).strip()
    if profile.max_anchor_chars and len(anchor) > body_budget:
        anchor = _truncate_sections(anchor, body_budget)

    # Never return an empty anchor: it would claim "0 tokens" and lose the pointer
    # to the drawer. Fall back to the highest-scoring lines.
    if not anchor.strip():
        fallback = [scored[i][1] for i in range(len(scored))][:20] if scored else []
        anchor = "\n".join(["**Facts**", *(f"- {f}" for f in fallback)])
        if not anchor.strip():
            anchor = "(no extractable facts - content is retrievable in full via ctx_get)"

    # The footer is always the LAST line and is never truncated.
    anchor = f"{anchor}\n{footer}" if anchor else footer
    if profile.max_anchor_chars and len(anchor) > profile.max_anchor_chars:
        # Last-resort safety net: cut the body, never the footer.
        over = len(anchor) - profile.max_anchor_chars
        body = anchor[: len(anchor) - len(footer) - 1]
        anchor = f"{body[: max(0, len(body) - over)]}\n{footer}"

    return anchor, units
