"""Web strategies: page, sitemap, md_links, html_crawl, github_docs."""

from __future__ import annotations

import re
from collections import deque
from urllib.parse import quote

from ..models import FetchResult, RawDoc, SourceSpec
from .base import (
    GITHUB_DOCS_ARTICLE_API,
    FetchContext,
    canonical_url,
    compile_patterns,
    fetch_many,
    resolve_link,
    sitemap_urls,
    url_matches,
)

MD_LINK = re.compile(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)")
HREF = re.compile(r"""href=(?:"([^"#]*)"|'([^'#]*)'|([^\s>"'#]+))""", re.IGNORECASE)


def fetch_pages(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    urls = spec.params.get("urls") or [spec.url]
    fetch_many(ctx, spec, urls, result)
    return result


def fetch_sitemap(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    groups = spec.params.get("sitemaps") or [
        {k: spec.params.get(k) for k in ("sitemap", "include", "exclude")}
    ]
    selected: list[str] = []
    for group in groups:
        include = compile_patterns(group.get("include"))
        exclude = compile_patterns(group.get("exclude"))
        try:
            urls = sitemap_urls(ctx, group["sitemap"])
        except Exception as exc:
            result.errors.append(str(exc))
            continue
        selected += [u for u in urls if url_matches(u, include, exclude)]
    limit = ctx.max_pages(spec)
    if len(selected) > limit:
        result.skipped_urls += [f"{u} (over max_pages={limit})" for u in selected[limit:]]
    fetch_many(ctx, spec, selected[:limit], result)
    return result


def fetch_md_links(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    """Breadth-first walk over relative .md links, starting from seed .md pages."""
    result = FetchResult(source_id=spec.id)
    include = compile_patterns(spec.params.get("include"))
    exclude = compile_patterns(spec.params.get("exclude"))
    max_depth = int(spec.params.get("max_depth", 2))
    limit = ctx.max_pages(spec)
    queue = deque((canonical_url(s), 0) for s in spec.params["seeds"])
    seen = {u for u, _ in queue}
    while queue and len(result.docs) < limit:
        url, depth = queue.popleft()
        try:
            r = ctx.client.get(url)
        except Exception as exc:
            result.errors.append(f"{url}: {exc}")
            continue
        if r.status != 200:
            result.errors.append(f"{url}: HTTP {r.status}")
            continue
        page_url = url[:-3] + ".html" if url.endswith(".md") else url
        result.docs.append(RawDoc(source_id=spec.id, url=page_url, format="markdown", body=r.text,
                                  title=None, extra={"fetched_from": url}))
        if depth >= max_depth:
            continue
        for href in MD_LINK.findall(r.text):
            nxt = resolve_link(url, href)
            if nxt in seen or not url_matches(nxt, include, exclude):
                continue
            seen.add(nxt)
            queue.append((nxt, depth + 1))
    if queue:
        result.skipped_urls += [f"{u} (over max_pages={limit})" for u, _ in queue]
    return result


def fetch_html_crawl(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    """Bounded crawl of an HTML site within a path prefix."""
    result = FetchResult(source_id=spec.id)
    prefix = spec.params["prefix"]
    include = compile_patterns(spec.params.get("include"))
    exclude = compile_patterns(spec.params.get("exclude"))
    max_depth = int(spec.params.get("max_depth", 2))
    limit = ctx.max_pages(spec)
    queue = deque((canonical_url(s), 0) for s in spec.params["seeds"])
    seen = {u for u, _ in queue}
    while queue and len(result.docs) < limit:
        url, depth = queue.popleft()
        try:
            r = ctx.client.get(url)
        except Exception as exc:
            result.errors.append(f"{url}: {exc}")
            continue
        if r.status != 200 or "html" not in r.content_type:
            continue
        # Seeds are always kept; other pages must match the include filter.
        if depth == 0 or url_matches(url, include, exclude):
            result.docs.append(RawDoc(source_id=spec.id, url=url, format="html", body=r.text))
        if depth >= max_depth:
            continue
        for groups in HREF.findall(r.text):
            href = next((g for g in groups if g), "")
            if not href:
                continue
            nxt = resolve_link(url, href)
            if nxt in seen or not nxt.startswith(prefix):
                continue
            if any(p.search(nxt) for p in exclude) or re.search(r"\.(png|svg|jpg|gif|css|js|xml|zip)$", nxt):
                continue
            seen.add(nxt)
            queue.append((nxt, depth + 1))
    return result


def fetch_github_docs(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    """docs.github.com: list pages with the pagelist API, fetch rendered Markdown per page."""
    result = FetchResult(source_id=spec.id)
    pages = spec.params.get("pages")
    if not pages:
        r = ctx.client.get("https://docs.github.com/api/pagelist/en/free-pro-team@latest")
        prefixes = tuple(spec.params["prefixes"])
        pages = [p.strip() for p in r.text.splitlines() if p.strip().startswith(prefixes)]
    limit = ctx.max_pages(spec)
    if len(pages) > limit:
        result.skipped_urls += [f"{p} (over max_pages={limit})" for p in pages[limit:]]
    for path in pages[:limit]:
        api = GITHUB_DOCS_ARTICLE_API + quote(path, safe="/")
        try:
            r = ctx.client.get(api)
        except Exception as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        if r.status != 200 or not r.text.strip():
            result.errors.append(f"{path}: HTTP {r.status}")
            continue
        m = re.search(r"^#\s+(.+)$", r.text, re.MULTILINE)
        result.docs.append(RawDoc(source_id=spec.id, url=f"https://docs.github.com{path}",
                                  title=m.group(1).strip() if m else None, format="markdown",
                                  body=r.text, extra={"fetched_from": api}))
    return result
