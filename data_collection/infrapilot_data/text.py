"""Text utilities shared by normalization and chunking: token counting and code-fence splitting."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

FENCE = re.compile(r"^(\s*)(```|~~~)")


@lru_cache(maxsize=1)
def _encoder():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Token count with cl100k_base, a close proxy for Titan/Claude tokenizers."""
    return len(_encoder().encode(text, disallowed_special=()))


def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def split_fences(text: str) -> list[tuple[bool, str]]:
    """Split Markdown into (is_code, segment) runs, keeping fenced code blocks intact."""
    segments: list[tuple[bool, str]] = []
    buf: list[str] = []
    in_code, marker = False, ""
    for line in text.splitlines(keepends=True):
        m = FENCE.match(line)
        if not in_code and m:
            if buf:
                segments.append((False, "".join(buf)))
            buf, in_code, marker = [line], True, m.group(2)
        elif in_code and line.strip().startswith(marker):
            buf.append(line)
            segments.append((True, "".join(buf)))
            buf, in_code = [], False
        else:
            buf.append(line)
    if buf:
        segments.append((in_code, "".join(buf)))
    return segments


def map_prose(text: str, fn) -> str:
    """Apply fn to the non-code parts of a Markdown document."""
    return "".join(seg if is_code else fn(seg) for is_code, seg in split_fences(text))


def slugify(value: str, max_len: int = 60) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:max_len].rstrip("-") or "doc"
