"""search_policy: BM25 keyword search over Fernway's public help documents in kb/*.md.

Each '## ' section of a document is one passage. Only public documents live in kb/: the internal procedures are
rules in rta/policy.py, never text the model can quote to a customer.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rta.config import KB_DIR

_STOP = frozenset(
    "a an and are as at be by can do does for from has have how i if in is it its my of on or our so that the "
    "their there this to was we what when where which who will with you your".split()
)


def _fold(t: str) -> str:
    """Light plural folding so 'plans', 'prices' and 'seats' match 'plan', 'price' and 'seat'."""
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def tokens(text: str) -> list[str]:
    return [_fold(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Passage:
    doc_id: str
    title: str
    meta: str  # version and status line, when the document has one
    section: str
    text: str

    def as_result(self) -> dict:
        return {"doc": self.doc_id, "title": self.title, "status": self.meta or None, "section": self.section,
                "text": self.text}


def load_passages(kb_dir: Path | str = KB_DIR) -> list[Passage]:
    passages: list[Passage] = []
    for path in sorted(Path(kb_dir).glob("*.md")):
        body = path.read_text().split("\n---\n")[0]  # the fiction note after the rule is not a passage
        title, meta = path.stem, ""
        sections: list[tuple[str, list[str]]] = []
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                sections.append((line[3:].strip(), []))
            elif not sections and not meta and line.startswith("*") and line.endswith("*"):
                meta = line.strip("*").strip()
            elif sections:
                sections[-1][1].append(line)
        for heading, lines in sections:
            text = "\n".join(lines).strip()
            if text:
                passages.append(Passage(path.stem, title, meta, heading, text))
    return passages


class KB:
    """Okapi BM25 over passages (k1 = 1.2, b = 0.75). Titles and section headings count as passage text."""

    def __init__(self, passages: list[Passage], k1: float = 1.2, b: float = 0.75) -> None:
        self.passages = passages
        self.k1, self.b = k1, b
        self._tf = [Counter(tokens(f"{p.title} {p.section} {p.text}")) for p in passages]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(passages)
        self._idf = {t: math.log((n - f + 0.5) / (f + 0.5) + 1) for t, f in df.items()}

    @classmethod
    def load(cls, kb_dir: Path | str = KB_DIR) -> "KB":
        return cls(load_passages(kb_dir))

    def search(self, query: str, k: int = 3) -> list[tuple[Passage, float]]:
        q = set(tokens(query))
        scored = []
        for i, tf in enumerate(self._tf):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    norm = f + self.k1 * (1 - self.b + self.b * self._len[i] / (self._avg or 1))
                    s += self._idf[t] * f * (self.k1 + 1) / norm
            if s > 0:
                scored.append((self.passages[i], s))
        scored.sort(key=lambda x: (-x[1], x[0].doc_id, x[0].section))
        return scored[:k]
