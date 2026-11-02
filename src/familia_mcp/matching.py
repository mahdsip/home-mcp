"""Fuzzy matching of spoken names to devices (shared by all modules).

Voice transcription splits or joins words ("pi monitor" vs "pimonitor"), adds
accents and articles ("la luz del salón"). Matching runs in three levels and
stops at the first level with candidates:

  1. exact alphanumeric skeleton ("Luz Salón" == "luz salon"), or the same
     meaningful words ("la luz del salón" == "Luz Salón")
  2. every meaningful query word appears in the name's words
  3. one skeleton contains the other (min. 2 chars)

One candidate -> match. Several at level 2/3 -> "ambiguous" (the model then
asks the user). None -> "unknown" with the list of known names.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Sequence, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

T = TypeVar("T")

# Words ignored when matching (articles/prepositions). Matching aid only; the
# user-facing answer language is handled by the orchestrator.
STOPWORDS = frozenset(
    {
        "el", "la", "los", "las", "un", "una", "de", "del", "al", "en", "mi", "mis",
        "the", "a", "an", "of", "in", "my",
    }
)
_WORD_SPLIT = re.compile(r"[^0-9a-z]+")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def normalize(text: str) -> str:
    """Lowercase, strip accents and keep only letters/digits."""
    return "".join(c for c in _fold(text) if c.isalnum())


def words(text: str) -> set[str]:
    return {w for w in _WORD_SPLIT.split(_fold(text)) if w}


def resolve(
    query: str,
    items: Sequence[T],
    names: Callable[[T], Sequence[str]],
    label: Callable[[T], str],
    kind: str = "device",
) -> T:
    q_skel = normalize(query)
    q_words = words(query) - STOPWORDS
    if not q_skel:
        raise ToolError(f"{kind.capitalize()} name is empty.")

    def level(item: T) -> int:
        """Best (lowest) match level over all names of an item; 0 = no match."""
        found = []
        for name in names(item):
            skel = normalize(name)
            if not skel:
                continue
            if skel == q_skel or (q_words and q_words == words(name) - STOPWORDS):
                found.append(1)
            elif q_words and q_words <= words(name):
                found.append(2)
            elif min(len(skel), len(q_skel)) >= 2 and (q_skel in skel or skel in q_skel):
                found.append(3)
        return min(found, default=0)

    scored = [(level(i), i) for i in items]
    for wanted in (1, 2, 3):
        candidates = [i for lvl, i in scored if lvl == wanted]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            options = ", ".join(sorted(label(c) for c in candidates))
            raise ToolError(f"Ambiguous {kind} '{query}'. It could be: {options}.")
    known = ", ".join(sorted(label(i) for i in items)) or "none"
    raise ToolError(f"Unknown {kind} '{query}'. Known {kind}s: {known}.")
