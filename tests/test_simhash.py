from __future__ import annotations

from ai_digest.process.simhash import (
    from_hex,
    hamming,
    simhash,
    to_hex,
    tokenize,
)

PARAGRAPH = (
    "OpenAI releases a faster inference stack. A new kernel cuts latency by 40 "
    "percent on H100 GPUs, the company said on Tuesday, and the library is open "
    "source under an Apache license."
)

REWORDED = (
    "OpenAI ships a faster inference stack. A new kernel cuts latency by 40 "
    "percent on H100 GPUs, the company said on Tuesday, and the library is open "
    "source under the Apache license."
)

UNRELATED = (
    "Scaling laws for multimodal pretraining suggest that compute allocation "
    "should shift as the number of modalities grows, according to a new paper."
)


def test_identical_text_has_zero_distance() -> None:
    assert hamming(simhash(PARAGRAPH), simhash(PARAGRAPH)) == 0


def test_reworded_text_is_closer_than_unrelated_text() -> None:
    base = simhash(PARAGRAPH)
    reworded = simhash(REWORDED)
    unrelated = simhash(UNRELATED)

    assert hamming(base, reworded) < hamming(base, unrelated)
    assert hamming(base, unrelated) > 10


def test_empty_text_is_zero() -> None:
    assert simhash("") == 0
    assert simhash("   ") == 0


def test_hash_is_stable_across_calls() -> None:
    # blake2b, not hash(): must not depend on PYTHONHASHSEED.
    assert simhash(PARAGRAPH) == simhash(PARAGRAPH)


def test_hex_roundtrip() -> None:
    value = simhash(PARAGRAPH)
    assert from_hex(to_hex(value)) == value
    assert len(to_hex(value)) == 16


def test_tokenize_lowercases_and_splits() -> None:
    assert tokenize("Hello, World! 2026") == ["hello", "world", "2026"]
