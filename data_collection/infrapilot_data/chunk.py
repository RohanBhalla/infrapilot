"""Structure-aware Markdown chunking.

- Splits on headings first, so a chunk never spans two unrelated sections.
- Never cuts inside a fenced code block unless the block alone exceeds max_tokens
  (then it splits on line boundaries and re-opens the fence).
- Packs paragraphs up to target_tokens with a small overlap between neighbours.
- Merges tiny sections into the next one so headings with one line don't become chunks.
- Prefixes each chunk with a context header (doc title, section path, category) so the
  embedding knows where the text came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import SETTINGS, ChunkingConfig
from .models import Chunk, NormalizedDoc
from .text import count_tokens, sha256, split_fences

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass
class Section:
    path: list[str]
    blocks: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(b for b in self.blocks if b.strip())


def _sections(markdown: str) -> list[Section]:
    stack: list[tuple[int, str]] = []
    sections = [Section(path=[])]
    for is_code, seg in split_fences(markdown):
        if is_code:
            sections[-1].blocks.append(seg.strip("\n"))
            continue
        para: list[str] = []

        def flush():
            if para and "".join(para).strip():
                sections[-1].blocks.append("\n".join(para).strip("\n"))
            para.clear()

        for line in seg.split("\n"):
            m = HEADING.match(line)
            if m:
                flush()
                level, title = len(m.group(1)), m.group(2).strip()
                stack[:] = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
                sections.append(Section(path=[t for _, t in stack], blocks=[line.strip()]))
            elif not line.strip():
                flush()
            else:
                para.append(line)
        flush()
    return [s for s in sections if s.text.strip()]


TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}")


def _split_oversized(block: str, cfg: ChunkingConfig) -> list[str]:
    if count_tokens(block) <= cfg.max_tokens:
        return [block]
    lines = block.split("\n")
    fence = lines[0] if lines[0].lstrip().startswith(("```", "~~~")) else None
    body = lines[1:-1] if fence else lines
    # Tables repeat their header row in every piece so each chunk stays readable.
    header: list[str] = []
    if not fence and len(body) > 2 and body[0].lstrip().startswith("|") and TABLE_SEP.match(body[1]):
        header, body = body[:2], body[2:]
    pieces, cur = [], list(header)
    for line in body:
        cur.append(line)
        if count_tokens("\n".join(cur)) >= cfg.target_tokens:
            pieces.append(cur)
            cur = list(header)
    if len(cur) > len(header):
        pieces.append(cur)
    if fence:
        close = fence.strip()[:3]
        return [f"{fence}\n" + "\n".join(p) + f"\n{close}" for p in pieces]
    # Very long single lines (minified text): hard-split by characters.
    out = []
    for p in pieces:
        text = "\n".join(p)
        if count_tokens(text) <= cfg.max_tokens:
            out.append(text)
        else:
            step = cfg.target_tokens * 3
            out += [text[i:i + step] for i in range(0, len(text), step)]
    return out


def _merge_small(sections: list[Section], cfg: ChunkingConfig) -> list[Section]:
    """Fold sections under min_tokens (often a bare heading) into the following section."""
    merged: list[Section] = []
    carry: list[str] = []
    for s in sections:
        blocks = carry + s.blocks
        carry = []
        if count_tokens("\n\n".join(blocks)) < cfg.min_tokens:
            carry = blocks
            continue
        merged.append(Section(path=list(s.path), blocks=blocks))
    if carry:
        if merged:
            merged[-1].blocks += carry
        else:
            merged.append(Section(path=sections[-1].path if sections else [], blocks=carry))
    return merged


def _pack(blocks: list[str], cfg: ChunkingConfig) -> list[str]:
    units = [p for b in blocks for p in _split_oversized(b, cfg)]
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    for u in units:
        t = count_tokens(u)
        # Never close a chunk below min_tokens (e.g. headings followed by a large table).
        if cur and cur_tokens + t > cfg.target_tokens and cur_tokens >= cfg.min_tokens:
            chunks.append(cur)
            # Overlap: carry trailing prose blocks that fit in the overlap budget.
            carry, carry_tokens = [], 0
            for prev in reversed(cur):
                pt = count_tokens(prev)
                if prev.lstrip().startswith(("```", "~~~", "#")) or carry_tokens + pt > cfg.overlap_tokens:
                    break
                carry.insert(0, prev)
                carry_tokens += pt
            cur, cur_tokens = carry, carry_tokens
        cur.append(u)
        cur_tokens += t
    if cur:
        chunks.append(cur)
    texts = ["\n\n".join(c) for c in chunks]
    # Fold a small trailing remainder back into the previous chunk when it fits.
    if len(texts) > 1 and count_tokens(texts[-1]) < cfg.min_tokens and \
            count_tokens(texts[-2]) + count_tokens(texts[-1]) <= cfg.max_tokens:
        tail = texts.pop()
        texts[-1] = f"{texts[-1]}\n\n{tail}"
    return texts


def context_header(doc: NormalizedDoc, path: list[str], source_name: str | None = None) -> str:
    trail = [p for p in path if p != doc.title]
    lines = [f"Document: {doc.title}"]
    # The sheet's name for the source often carries the product the page title leaves out
    # ("CDK module READMEs", "Checkov – GitHub Actions rules").
    if source_name and source_name.lower() not in doc.title.lower():
        lines.append(f"Source: {source_name}")
    if trail:
        lines.append(f"Section: {' > '.join(trail)}")
    lines.append(f"Category: {doc.category}")
    return "\n".join(lines)


def chunk_document(doc: NormalizedDoc, cfg: ChunkingConfig | None = None,
                   source_name: str | None = None) -> list[Chunk]:
    cfg = cfg or SETTINGS.chunking
    chunks: list[Chunk] = []
    for section in _merge_small(_sections(doc.body_markdown), cfg):
        for body in _pack(section.blocks, cfg):
            if count_tokens(body) < 8:
                continue
            text = f"{context_header(doc, section.path, source_name)}\n\n{body}"
            idx = len(chunks)
            chunks.append(Chunk(
                chunk_id=f"{doc.doc_id}-c{idx:03d}",
                doc_id=doc.doc_id,
                source_id=doc.source_id,
                category=doc.category,
                doc_type=doc.doc_type,
                title=doc.title,
                url=doc.url,
                heading_path=section.path,
                chunk_index=idx,
                token_count=count_tokens(text),
                content_hash=sha256(text),
                fetched_at=doc.fetched_at,
                text=text,
            ))
    return chunks
