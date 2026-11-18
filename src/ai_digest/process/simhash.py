"""64-bit SimHash for near-duplicate detection.

Why hand-rolled instead of a library: it is about 40 lines, and being able to
explain the bit-voting intuition out loud is worth more than a dependency.

Two details that matter:

* Tokens are hashed with ``blake2b``, *not* Python's ``hash()``, which is
  salted per process and would make near-dup decisions irreproducible.
* We hash 3-word shingles rather than single words, so that "openai releases
  a faster model" and "faster model released by openai" land close together.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9]+")
HASH_BITS = 64


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def shingles(tokens: list[str], size: int = 3) -> list[str]:
    if not tokens:
        return []
    if len(tokens) <= size:
        return [" ".join(tokens)]
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def _hash64(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(text: str, shingle_size: int = 3) -> int:
    """Bit-vote every shingle hash; a positive weight wins the bit."""
    features = shingles(tokenize(text), shingle_size)
    if not features:
        return 0

    vector = [0] * HASH_BITS
    for token, weight in Counter(features).items():
        hashed = _hash64(token)
        for bit in range(HASH_BITS):
            vector[bit] += weight if (hashed >> bit) & 1 else -weight

    value = 0
    for bit in range(HASH_BITS):
        if vector[bit] > 0:
            value |= 1 << bit
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_hex(value: int) -> str:
    # SQLite integers are signed 64-bit, so the value is stored as hex text.
    return f"{value:016x}"


def from_hex(value: str) -> int:
    return int(value, 16)
