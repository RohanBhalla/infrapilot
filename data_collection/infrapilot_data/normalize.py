"""RawDoc -> clean Markdown NormalizedDoc, plus corpus-level quality filters.

Cleaning keeps what helps retrieval (headings, prose, tables, code blocks) and drops what
adds noise to embeddings (link URLs, images, HTML anchors, template tags, front matter,
repeated boilerplate lines).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from urllib.parse import urlsplit

from .config import SETTINGS
from .models import NormalizedDoc, RawDoc, SourceSpec
from .text import count_tokens, map_prose, sha256, slugify, split_fences

FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
EMPTY_ANCHOR = re.compile(r"<a\s+[^>]*>\s*</a>", re.IGNORECASE)
IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
IMAGE_HTML = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
LINK_MD = re.compile(r"\[([^\]]+)\]\((?:[^()\s]|\([^)]*\))+(?:\s+\"[^\"]*\")?\)")
REF_LINK_DEF = re.compile(r"^\s*\[[^\]]+\]:\s+\S+.*$", re.MULTILINE)
LIQUID = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)
HUGO = re.compile(r"\{\{[<%].*?[>%]\}\}", re.DOTALL)
BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
WRAPPER_TAGS = re.compile(
    r"</?(?:p|div|span|details|summary|sup|sub|picture|source|kbd|center|figure|figcaption)\b[^>]*>",
    re.IGNORECASE,
)
BLANK_RUNS = re.compile(r"\n{3,}")
H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")


def html_to_markdown(html: str, url: str) -> str:
    import trafilatura

    md = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        include_images=False,
        include_comments=False,
        include_formatting=True,
        favor_recall=True,
    )
    return md or ""


def _clean_prose(text: str) -> str:
    text = HTML_COMMENT.sub("", text)
    text = EMPTY_ANCHOR.sub("", text)
    text = IMAGE_MD.sub("", text)
    text = IMAGE_HTML.sub("", text)
    text = LINK_MD.sub(r"\1", text)
    text = REF_LINK_DEF.sub("", text)
    text = LIQUID.sub("", text)
    text = HUGO.sub("", text)
    text = BR.sub(" ", text)
    text = WRAPPER_TAGS.sub("", text)
    return text


def clean_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\t", "    ")
    text = FRONT_MATTER.sub("", text)
    text = map_prose(text, _clean_prose)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return BLANK_RUNS.sub("\n\n", text).strip() + "\n"


def to_markdown(raw: RawDoc) -> str:
    if raw.format == "html":
        return clean_markdown(html_to_markdown(raw.body, raw.url))
    if raw.format == "code":
        title = raw.title or urlsplit(raw.url).path.rsplit("/", 1)[-1]
        return f"# {title}\n\nSource file: {raw.url}\n\n```{raw.language or ''}\n{raw.body.rstrip()}\n```\n"
    return clean_markdown(raw.body)


def doc_id_for(source_id: str, url: str) -> str:
    parts = urlsplit(url)
    slug = slugify(parts.path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or parts.netloc, 50)
    return f"s{source_id}-{slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"


def normalize(raw: RawDoc, spec: SourceSpec) -> NormalizedDoc:
    body = to_markdown(raw)
    m = H1.search(body)
    title = (raw.title or (m.group(1) if m else None) or spec.name).strip()
    if not m:
        body = f"# {title}\n\n{body}"
    return NormalizedDoc(
        doc_id=doc_id_for(spec.id, raw.url),
        source_id=spec.id,
        category=spec.category,
        doc_type=spec.doc_type,
        title=title[:200],
        url=raw.url,
        fetched_at=raw.fetched_at,
        published_at=raw.published_at,
        content_hash=sha256(body),
        token_count=count_tokens(body),
        body_markdown=body,
    )


# ---- corpus-level filters ------------------------------------------------------

def strip_boilerplate(docs: list[NormalizedDoc]) -> tuple[list[NormalizedDoc], list[str]]:
    """Remove prose lines that repeat across most docs of one source (banners, footers)."""
    q = SETTINGS.quality
    if len(docs) < q.boilerplate_min_docs:
        return docs, []
    counts: Counter[str] = Counter()
    for d in docs:
        lines = {
            ln.strip()
            for is_code, seg in split_fences(d.body_markdown) if not is_code
            for ln in seg.splitlines()
            if len(ln.strip()) >= 50 and not ln.lstrip().startswith(("#", "|"))
            and not LIST_ITEM.match(ln)
        }
        counts.update(lines)
    threshold = max(q.boilerplate_min_docs, int(len(docs) * q.boilerplate_doc_ratio))
    boiler = {ln for ln, n in counts.items() if n >= threshold}
    if not boiler:
        return docs, []

    def drop(seg: str) -> str:
        return "\n".join(ln for ln in seg.split("\n") if ln.strip() not in boiler)

    out = []
    for d in docs:
        body = BLANK_RUNS.sub("\n\n", map_prose(d.body_markdown, drop))
        out.append(d.model_copy(update={
            "body_markdown": body, "content_hash": sha256(body), "token_count": count_tokens(body),
        }))
    return out, sorted(boiler)


def quality_issue(doc: NormalizedDoc) -> str | None:
    """Return a reason to drop the doc, or None to keep it."""
    q = SETTINGS.quality
    if doc.token_count < q.min_doc_tokens:
        return "too_short"
    prose_lines = [
        ln for is_code, seg in split_fences(doc.body_markdown) if not is_code
        for ln in seg.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if prose_lines and doc.token_count < q.nav_max_tokens:
        short_items = sum(1 for ln in prose_lines if LIST_ITEM.match(ln) and len(ln.split()) <= 8)
        if short_items / len(prose_lines) >= q.nav_line_ratio:
            return "navigation_page"
    return None
