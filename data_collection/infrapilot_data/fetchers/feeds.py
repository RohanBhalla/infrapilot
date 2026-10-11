"""RSS/Atom feeds: release notes and changelogs, filtered by keyword and age."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import feedparser

from ..models import FetchResult, RawDoc, SourceSpec, utcnow
from .base import FetchContext


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    url = spec.params["url"]
    keywords = [k.lower() for k in spec.params.get("keywords", [])]
    cutoff = utcnow() - timedelta(days=int(spec.params.get("window_days", 180)))
    limit = ctx.max_pages(spec)

    r = ctx.client.get(url)
    if r.status != 200:
        result.errors.append(f"{url}: HTTP {r.status}")
        return result
    feed = feedparser.parse(r.text)
    for entry in feed.entries:
        published = _entry_time(entry)
        if published and published < cutoff:
            continue
        content = ""
        if entry.get("content"):
            content = entry.content[0].get("value", "")
        content = content or entry.get("summary", "")
        haystack = f"{entry.get('title', '')} {content}".lower()
        if keywords and not any(k in haystack for k in keywords):
            continue
        result.docs.append(RawDoc(
            source_id=spec.id,
            url=entry.get("link", url),
            title=entry.get("title"),
            format="html",
            body=f"<html><body><h1>{entry.get('title', '')}</h1>{content}</body></html>",
            published_at=published,
            extra={"feed": url},
        ))
        if len(result.docs) >= limit:
            break
    return result
