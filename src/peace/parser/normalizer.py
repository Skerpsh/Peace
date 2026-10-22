"""Приведение текста к единому виду перед разбором."""
from __future__ import annotations

import re

_APOSTROPHES = re.compile(r"[’ʼ`´‘]")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Нижний регистр, единый апостроф, ё->е, схлопывание пробелов.

    Эмодзи НЕ удаляем — 🔴/🟡/🟢 используются как маркеры уровня.
    """
    t = text.lower()
    t = _APOSTROPHES.sub("'", t)
    t = t.replace("ё", "е")
    t = _SPACES.sub(" ", t)
    return t.strip()
