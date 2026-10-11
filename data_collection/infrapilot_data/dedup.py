"""Exact and near-duplicate detection.

Exact: identical content hash. Near: 64-bit SimHash over 5-word shingles, Hamming distance
at or below a threshold. When duplicates collide, the doc from the earlier-listed source
wins, so primary references beat mirrors and aggregators.
"""

from __future__ import annotations

import hashlib
import re

from .config import SETTINGS
from .models import NormalizedDoc

WORD = re.compile(r"\w+")


def simhash(text: str, shingle: int = 5) -> int:
    words = WORD.findall(text.lower())
    if len(words) < shingle:
        words = words + [""] * (shingle - len(words))
    weights = [0] * 64
    for i in range(len(words) - shingle + 1):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i:i + shingle]).encode(),
                                           digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if weights[bit] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def dedupe(docs: list[NormalizedDoc], source_order: dict[str, int],
           max_distance: int | None = None) -> tuple[list[NormalizedDoc], list[tuple[str, str, str]]]:
    """Return (kept docs, [(dropped_doc_id, kept_doc_id, reason)])."""
    max_distance = SETTINGS.quality.near_dup_hamming if max_distance is None else max_distance
    ordered = sorted(docs, key=lambda d: (source_order.get(d.source_id, 1_000), d.url))
    kept: list[NormalizedDoc] = []
    kept_hashes: dict[str, str] = {}
    kept_urls: dict[str, str] = {}
    # Bucket fingerprints by 16-bit bands; near-duplicates within distance 3 share a band.
    bands: dict[tuple[int, int], list[tuple[int, str]]] = {}
    dropped: list[tuple[str, str, str]] = []

    for d in ordered:
        if d.url in kept_urls:
            dropped.append((d.doc_id, kept_urls[d.url], "same_url"))
            continue
        if d.content_hash in kept_hashes:
            dropped.append((d.doc_id, kept_hashes[d.content_hash], "exact"))
            continue
        fp = simhash(d.body_markdown)
        match = None
        for band in range(4):
            for other_fp, other_id in bands.get((band, fp >> (16 * band) & 0xFFFF), []):
                if hamming(fp, other_fp) <= max_distance:
                    match = other_id
                    break
            if match:
                break
        if match:
            dropped.append((d.doc_id, match, "near"))
            continue
        kept.append(d)
        kept_hashes[d.content_hash] = d.doc_id
        kept_urls[d.url] = d.doc_id
        for band in range(4):
            bands.setdefault((band, fp >> (16 * band) & 0xFFFF), []).append((fp, d.doc_id))
    return kept, dropped
