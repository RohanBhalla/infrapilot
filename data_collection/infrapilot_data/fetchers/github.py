"""GitHub strategies: README + latest release (github_repo), and files by glob (github_files)."""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from urllib.parse import quote

from ..models import FetchResult, RawDoc, SourceSpec, VersionFact
from .base import FetchContext

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
GH_JSON = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

CODE_LANG = {".yml": "yaml", ".yaml": "yaml", ".guard": "guard", ".json": "json",
             ".ts": "typescript", ".js": "javascript", ".py": "python"}


def fetch_github_repo(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    repo = spec.params["repo"]
    html_url = f"https://github.com/{repo}"

    if spec.params.get("readme", True):
        r = ctx.client.get(f"{API}/repos/{repo}/readme",
                           headers={"Accept": "application/vnd.github.raw+json"})
        if r.status == 200:
            result.docs.append(RawDoc(source_id=spec.id, url=f"{html_url}#readme",
                                      title=f"{repo} README", format="markdown", body=r.text,
                                      extra={"repo": repo}))
        else:
            result.errors.append(f"README for {repo}: HTTP {r.status}")

    tool = spec.params.get("version_tool", repo)
    if tool:
        fact = _latest_version(ctx, repo, tool)
        if fact:
            result.versions.append(fact)
        else:
            result.errors.append(f"no release or tag found for {repo}")
    return result


def _latest_version(ctx: FetchContext, repo: str, tool: str) -> VersionFact | None:
    r = ctx.client.get(f"{API}/repos/{repo}/releases/latest", headers=GH_JSON)
    if r.status == 200:
        rel = r.json()
        tag = rel["tag_name"]
        return VersionFact(
            tool=tool, latest=tag, released_at=rel.get("published_at"), source_url=rel["html_url"],
            extra={"major": tag.split(".")[0], "pin_hint": f"{repo}@{tag.split('.')[0]}"},
        )
    r = ctx.client.get(f"{API}/repos/{repo}/tags?per_page=1", headers=GH_JSON)
    if r.status == 200 and r.json():
        tag = r.json()[0]["name"]
        return VersionFact(tool=tool, latest=tag, source_url=f"https://github.com/{repo}/tags",
                           extra={"major": tag.split(".")[0], "from": "tags"})
    return None


def fetch_github_files(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    repo, ref = spec.params["repo"], spec.params.get("ref", "main")
    globs: list[str] = spec.params["globs"]
    limit = int(spec.params.get("max_files", 200))

    exact = [g for g in globs if not any(ch in g for ch in "*?[")]
    patterns = [g for g in globs if g not in exact]
    paths = list(exact)
    if patterns:
        r = ctx.client.get(f"{API}/repos/{repo}/git/trees/{ref}?recursive=1", headers=GH_JSON)
        if r.status != 200:
            result.errors.append(f"tree for {repo}@{ref}: HTTP {r.status}")
        else:
            tree = r.json()
            if tree.get("truncated"):
                result.errors.append(f"tree for {repo} truncated; some files may be missing")
            for item in tree["tree"]:
                if item["type"] == "blob" and any(fnmatch.fnmatch(item["path"], g) for g in patterns):
                    paths.append(item["path"])
    if len(paths) > limit:
        result.skipped_urls += [f"{p} (over max_files={limit})" for p in paths[limit:]]

    for path in paths[:limit]:
        raw_url = f"{RAW}/{repo}/{ref}/{quote(path)}"
        r = ctx.client.get(raw_url)
        if r.status != 200:
            result.errors.append(f"{path}: HTTP {r.status}")
            continue
        suffix = PurePosixPath(path).suffix.lower()
        is_md = suffix in (".md", ".markdown")
        result.docs.append(RawDoc(
            source_id=spec.id,
            url=f"https://github.com/{repo}/blob/{ref}/{path}",
            title=None if is_md else f"{repo}: {path}",
            format="markdown" if is_md else "code",
            language=None if is_md else CODE_LANG.get(suffix, suffix.lstrip(".")),
            body=r.text,
            extra={"repo": repo, "path": path},
        ))
    return result
