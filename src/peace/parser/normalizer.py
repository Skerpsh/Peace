"""Text normalization before parsing."""
from __future__ import annotations

import re

_APOSTROPHES = re.compile(r"[’ʼ`´‘]")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercases text, standardizes apostrophes and 'ё', and collapses whitespace.

    Emojis are preserved because 🔴/🟡/🟢 are used as fallback markers.
    """
    t = text.lower()
    t = _APOSTROPHES.sub("'", t)
    t = t.replace("ё", "е")
    t = _SPACES.sub(" ", t)
    return t.strip()
