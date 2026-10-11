"""Shared helpers for fetchers: Markdown-first page fetching, URL canonicalization, sitemaps."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

from ..http import PoliteClient, RobotsDisallowed
from ..models import FetchResult, RawDoc, SourceSpec

log = logging.getLogger(__name__)

GITHUB_DOCS_ARTICLE_API = "https://docs.github.com/api/article/body?pathname="


@dataclass
class FetchContext:
    client: PoliteClient
    max_pages_override: int | None = None

    def max_pages(self, spec: SourceSpec) -> int:
        limit = int(spec.params.get("max_pages", 150))
        return min(limit, self.max_pages_override) if self.max_pages_override else limit


def canonical_url(url: str) -> str:
    """Drop fragments and normalise trailing slashes so the same page isn't fetched twice."""
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    path = parts.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def markdown_variant(url: str) -> str | None:
    """Return a URL that serves the same page as Markdown, when the site offers one."""
    parts = urlsplit(url)
    host, path = parts.netloc, parts.path
    if host == "docs.aws.amazon.com" and path.endswith(".html"):
        return urlunsplit((parts.scheme, host, path[:-5] + ".md", "", ""))
    if host == "docs.docker.com" and not path.endswith(".md"):
        return urlunsplit((parts.scheme, host, path.rstrip("/") + ".md", "", ""))
    if host == "docs.github.com" and path.startswith("/en/"):
        return GITHUB_DOCS_ARTICLE_API + quote(path, safe="/")
    if host == "github.com":
        m = re.match(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", path)
        if m:
            owner, repo, ref, file_path = m.groups()
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{file_path}"
    return None


def _looks_like_html(text: str, content_type: str) -> bool:
    if "html" in content_type:
        return True
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _md_title(text: str) -> str | None:
    m = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _html_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return re.split(r"\s+[|\-–—]\s+", title)[0] or title


def fetch_page(ctx: FetchContext, spec: SourceSpec, url: str) -> RawDoc | None:
    """Fetch one page, preferring the Markdown rendition. Returns None on a non-200."""
    md_url = markdown_variant(url)
    if md_url:
        try:
            r = ctx.client.get(md_url)
            if r.status == 200 and r.text.strip() and not _looks_like_html(r.text, r.content_type):
                return RawDoc(source_id=spec.id, url=url, title=_md_title(r.text),
                              format="markdown", body=r.text,
                              extra={"fetched_from": md_url, "cache_hit": r.from_cache})
        except RobotsDisallowed:
            log.info("robots.txt disallows %s, trying HTML", md_url)
    r = ctx.client.get(url)
    if r.status != 200:
        log.warning("[%s] HTTP %s for %s", spec.id, r.status, url)
        return None
    if _looks_like_html(r.text, r.content_type):
        return RawDoc(source_id=spec.id, url=url, title=_html_title(r.text), format="html",
                      body=r.text, extra={"cache_hit": r.from_cache})
    return RawDoc(source_id=spec.id, url=url, title=_md_title(r.text), format="markdown",
                  body=r.text, extra={"cache_hit": r.from_cache})


def fetch_many(ctx: FetchContext, spec: SourceSpec, urls: list[str], result: FetchResult) -> None:
    seen: set[str] = set()
    for url in urls:
        url = canonical_url(url)
        if url in seen:
            continue
        seen.add(url)
        try:
            doc = fetch_page(ctx, spec, url)
        except RobotsDisallowed:
            result.skipped_urls.append(f"{url} (robots.txt)")
            continue
        except Exception as exc:  # keep going; one bad page shouldn't sink a source
            result.errors.append(f"{url}: {exc}")
            continue
        if doc:
            result.docs.append(doc)
        else:
            result.errors.append(f"{url}: non-200")


def compile_patterns(patterns: list[str] | None) -> list[re.Pattern]:
    return [re.compile(p) for p in (patterns or [])]


def url_matches(url: str, include: list[re.Pattern], exclude: list[re.Pattern]) -> bool:
    if include and not any(p.search(url) for p in include):
        return False
    return not any(p.search(url) for p in exclude)


def sitemap_urls(ctx: FetchContext, sitemap_url: str, depth: int = 0) -> list[str]:
    r = ctx.client.get(sitemap_url)
    if r.status != 200:
        raise RuntimeError(f"sitemap {sitemap_url} returned {r.status}")
    # Some sitemaps (e.g. karpenter.sh) use relative paths; resolve them against the sitemap URL.
    locs = [urljoin(sitemap_url, loc) for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)]
    if "<sitemapindex" in r.text[:500] and depth < 2:
        out: list[str] = []
        for child in locs:
            out.extend(sitemap_urls(ctx, child, depth + 1))
        return out
    return locs


def resolve_link(base: str, href: str) -> str:
    return canonical_url(urljoin(base, href))
