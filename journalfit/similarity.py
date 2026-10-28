"""Lightweight TF-IDF cosine similarity (no heavy ML dependencies)."""

from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

STOP = set("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had
has have having he her here hers herself him himself his how i if in into is it its itself just
me more most my myself no nor not now of off on once only or other our ours ourselves out over own
same she should so some such than that the their theirs them themselves then there these they this
those through to too under until up very was we were what when where which while who whom why will
with would you your yours yourself yourselves paper study results show shows shown using used use
based propose proposed approach method methods new novel however therefore thus well also may
via among within without whereas respectively et al fig figure table data analysis
""".split())
TOKEN = re.compile(r"[a-z][a-z0-9\-]{2,}")


def tokens(text: str) -> list[str]:
    words = [w.strip("-") for w in TOKEN.findall(text.lower())]
    words = [w for w in words if w and w not in STOP]
    # Unigrams plus bigrams: bigrams capture phrases like "graph neural".
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]


def tfidf_similarities(query: str, docs: list[str]) -> np.ndarray:
    """Cosine similarity of `query` against each document."""
    if not docs:
        return np.zeros(0)
    toks = [tokens(d) for d in docs]
    q = tokens(query)
    df = Counter()
    for t in toks:
        df.update(set(t))
    df.update(set(q))
    n = len(docs) + 1
    vocab = {w: i for i, w in enumerate(w for w in df if df[w] > 0)}
    idf = np.zeros(len(vocab))
    for w, i in vocab.items():
        idf[i] = math.log((1 + n) / (1 + df[w])) + 1

    def vec(ts: list[str]) -> np.ndarray:
        v = np.zeros(len(vocab))
        for w, c in Counter(ts).items():
            v[vocab[w]] = 1 + math.log(c)
        v *= idf
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    qv = vec(q)
    return np.array([float(vec(t) @ qv) for t in toks])


def top_terms(text: str, k: int = 8) -> list[str]:
    counts = Counter(w for w in tokens(text) if "_" not in w)
    return [w for w, _ in counts.most_common(k)]
