"""Strategy name -> fetch function. Each takes (SourceSpec, FetchContext) -> FetchResult."""

from .base import FetchContext
from .feeds import fetch_feed
from .github import fetch_github_files, fetch_github_repo
from .local import fetch_upload
from .versions import fetch_versions
from .web import fetch_github_docs, fetch_html_crawl, fetch_md_links, fetch_pages, fetch_sitemap

STRATEGIES = {
    "page": fetch_pages,
    "sitemap": fetch_sitemap,
    "md_links": fetch_md_links,
    "html_crawl": fetch_html_crawl,
    "github_docs": fetch_github_docs,
    "github_repo": fetch_github_repo,
    "github_files": fetch_github_files,
    "feed": fetch_feed,
    "versions": fetch_versions,
    "upload": fetch_upload,
}

__all__ = ["STRATEGIES", "FetchContext"]
