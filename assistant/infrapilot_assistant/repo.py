"""Read a public GitHub repo for context (decision 006, option 1).

When a message links a public repo, we look at a few files at its top level (package.json, lock
files, pyproject.toml, go.mod, pom.xml, Dockerfile, ...) and work out answers the dialog would
otherwise ask for: the language, package manager and version, the front-end framework and the
folder its build goes to, and the port in the Dockerfile.

Nothing from the repo runs, and nothing from it is sent to the model: only the facts below,
picked out by plain rules, are used. Private repos aren't read (that needs a GitHub sign-in).

`detect` is pure (paths + a file reader in, facts out) so it's tested without the network;
`GitHubReader` does the fetching, with short timeouts and size caps.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# github.com/<owner>/<repo>, optionally /tree/<branch>. Only these characters, so a URL can't
# point the reader anywhere but GitHub.
GITHUB_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]{1,39})/([\w.-]{1,100}?)(?:\.git)?"
    r"(?:/tree/([\w.-]{1,100}))?(?=[\s/#?)>\],.;:!]|$)")
MAX_BYTES = 200_000
TIMEOUT_S = 4

# Which repo facts answer which slot, per intent.
REPO_SLOTS: dict[str, dict[str, str]] = {
    "cicd_github_actions": {"language": "language", "package_manager": "package_manager",
                            "runtime_version": "runtime_version"},
    "containerize": {"language": "language", "app_port": "app_port"},
    "deploy_static_site": {"framework": "framework", "source_dir": "site_dir"},
    "deploy_lambda": {"runtime": "lambda_runtime"},
}

# Files we may read; anything else at the top level is only checked by name.
WANTED = ("package.json", "angular.json", "pyproject.toml", "go.mod", "Dockerfile", ".nvmrc",
          ".node-version", ".python-version")
# Checked by name when GitHub's API can't list the repo (rate limit): we then try each one.
PROBE = WANTED + ("requirements.txt", "setup.py", "Pipfile", "poetry.lock", "pom.xml", "build.gradle",
                  "build.gradle.kts", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "index.html")
SITE_FOLDERS = ("public", "site", "www", "docs")


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    ref: str = "HEAD"

    @property
    def label(self) -> str:
        return f"github.com/{self.owner}/{self.name}"


def find_repo(message: str) -> RepoRef | None:
    m = GITHUB_URL.search(message)
    if not m or m.group(2) in (".", ".."):
        return None
    return RepoRef(m.group(1), m.group(2), m.group(3) or "HEAD")


def _major(version: str) -> str | None:
    m = re.search(r"\d+(?:\.\d+)?", version)
    return m.group() if m else None


def _node_framework(deps: dict, read) -> tuple[str | None, str | None, list[str]]:
    """(framework, build folder, notes) from package.json dependencies."""
    has = deps.__contains__
    ui = "react" if has("react") else "vue" if has("vue") else "svelte" if has("svelte") else None
    if has("@angular/core"):
        project = None
        try:
            project = next(iter(json.loads(read("angular.json") or "{}").get("projects", {})), None)
        except (ValueError, AttributeError):
            pass
        if project and re.fullmatch(r"[\w.-]+", project):
            return "angular", f"dist/{project}/browser", []
        return "angular", None, []
    if has("next"):
        return "react", "out", ["Next.js only builds a static site with `output: 'export'` in next.config."]
    if has("gatsby"):
        return "react", "public", []
    if has("@sveltejs/kit"):
        return "svelte", "build", ["SvelteKit needs `@sveltejs/adapter-static` to build a static site."]
    if has("nuxt"):
        return "vue", ".output/public", ["Nuxt builds a static site with `nuxi generate`."]
    if has("astro"):
        return "other", "dist", []
    if has("react-scripts"):
        return "react", "build", []
    if has("@vue/cli-service"):
        return "vue", "dist", []
    if has("vite"):
        return ui or "plain", "dist", []
    return ui, None, []


def detect(paths: set[str], read: Callable[[str], str | None]) -> tuple[dict, list[str]]:
    """Facts about a repo from its top-level file names and a reader for the files in WANTED."""
    facts: dict[str, object] = {}
    notes: list[str] = []
    languages = []
    if "go.mod" in paths:
        languages.append("go")
    if paths & {"pom.xml", "build.gradle", "build.gradle.kts"}:
        languages.append("java")
    if paths & {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}:
        languages.append("python")
    if "package.json" in paths:
        languages.append("node")

    pkg: dict = {}
    if "package.json" in paths:
        try:
            pkg = json.loads(read("package.json") or "{}")
        except ValueError:
            notes.append("package.json isn't valid JSON, so I skipped it.")
        pkg = pkg if isinstance(pkg, dict) else {}
    deps = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    deps = deps if all(isinstance(k, str) for k in deps) else {}

    # A front end decides the language even if the repo also has, say, a Python back end.
    framework, site_dir, site_notes = _node_framework(deps, read) if deps else (None, None, [])
    if len(languages) == 1 or (languages and framework):
        language = "node" if framework else languages[0]
        facts["language"] = language
        if language in ("node", "python"):
            facts["lambda_runtime"] = language
    elif len(languages) > 1:
        notes.append(f"The repo has {' and '.join(languages)} files, so I didn't pick a language.")

    language = facts.get("language")
    if language == "node":
        facts["package_manager"] = ("pnpm" if "pnpm-lock.yaml" in paths else
                                    "yarn" if "yarn.lock" in paths else "npm")
        version = next((v for f in (".nvmrc", ".node-version") if f in paths and (v := read(f))), None)
        version = version or str((pkg.get("engines") or {}).get("node") or "")
        if m := re.search(r"\d+", version):  # Node: the major version
            facts["runtime_version"] = m.group()
    elif language == "python":
        project: dict = {}
        if "pyproject.toml" in paths:
            try:
                project = tomllib.loads(read("pyproject.toml") or "")
            except tomllib.TOMLDecodeError:
                notes.append("pyproject.toml isn't valid TOML, so I skipped it.")
        poetry = "poetry.lock" in paths or "poetry" in (project.get("tool") or {})
        facts["package_manager"] = "poetry" if poetry else "pip"
        version = (read(".python-version") if ".python-version" in paths else None) or \
            str((project.get("project") or {}).get("requires-python") or "")
        if (v := _major(version)) and "." in v:
            facts["runtime_version"] = v
    elif language == "java":
        facts["package_manager"] = "maven" if "pom.xml" in paths else "gradle"
    elif language == "go":
        facts["package_manager"] = "go"
        if m := re.search(r"^go\s+(\d+\.\d+)", read("go.mod") or "", re.M):
            facts["runtime_version"] = m.group(1)

    if framework:
        facts["framework"] = framework
        if site_dir:
            facts["site_dir"] = site_dir
            notes.append(f"`{site_dir}` is where {framework if framework != 'other' else 'the build'} "
                         "puts the built site by default; tell me if yours is set up differently.")
        notes += site_notes
    elif not pkg and (folder := next((f for f in SITE_FOLDERS if f"{f}/index.html" in paths), None)):
        facts["framework"], facts["site_dir"] = "plain", folder
    elif not pkg and "index.html" in paths:
        facts["framework"] = "plain"
        notes.append("Your index.html is at the top of the repo. Uploading the whole repo would publish "
                     "its other files too, so tell me which folder to upload (or move the pages into one, "
                     "like `site/`).")

    if "Dockerfile" in paths and (m := re.search(r"^\s*EXPOSE\s+(\d{1,5})\b", read("Dockerfile") or "", re.M | re.I)):
        facts["app_port"] = int(m.group(1))
    return facts, notes


class RepoUnreadable(Exception):
    pass


class GitHubReader:
    """Reads public repos: one GitHub API call to list the top level, then raw files."""

    def __init__(self, token: str | None = None, timeout: float = TIMEOUT_S, urlopen=urllib.request.urlopen):
        self.token, self.timeout, self._urlopen = token, timeout, urlopen

    def _get(self, url: str, api: bool = False, method: str = "GET") -> bytes | None:
        headers = {"User-Agent": "infrapilot"}
        if api:
            headers["Accept"] = "application/vnd.github+json"
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        try:
            with self._urlopen(urllib.request.Request(url, headers=headers, method=method),
                               timeout=self.timeout) as resp:
                return resp.read(MAX_BYTES + 1) if method == "GET" else b""
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _paths(self, ref: RepoRef) -> set[str]:
        base = f"https://api.github.com/repos/{ref.owner}/{ref.name}"
        try:
            body = self._get(f"{base}/git/trees/{ref.ref}", api=True)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429):
                raise
            log.warning("GitHub API limit reached; checking files one by one")
            return self._probe(ref)
        if body is None:
            raise RepoUnreadable("it's private or doesn't exist")
        names = {e["path"] for e in json.loads(body).get("tree", []) if isinstance(e, dict) and "path" in e}
        for folder in [f for f in SITE_FOLDERS if f in names]:
            if self._raw(ref, f"{folder}/index.html", method="HEAD") is not None:
                names.add(f"{folder}/index.html")
        return names

    def _probe(self, ref: RepoRef) -> set[str]:
        names = {p for p in PROBE + tuple(f"{f}/index.html" for f in SITE_FOLDERS)
                 if self._raw(ref, p, method="HEAD") is not None}
        if not names:
            raise RepoUnreadable("it's private, doesn't exist, or GitHub is limiting requests right now")
        return names

    def _raw(self, ref: RepoRef, path: str, method: str = "GET") -> bytes | None:
        return self._get(f"https://raw.githubusercontent.com/{ref.owner}/{ref.name}/{ref.ref}/{path}",
                         method=method)

    def read(self, ref: RepoRef) -> dict:
        """{"repo", "facts", "notes", "error"}: what the dialog stores and shows."""
        out = {"repo": ref.label, "facts": {}, "notes": [], "error": None}
        try:
            paths = self._paths(ref)

            def read_file(path: str) -> str | None:
                if path not in WANTED:
                    return None
                body = self._raw(ref, path)
                if body is None or len(body) > MAX_BYTES:
                    return None
                return body.decode("utf-8", "replace")

            out["facts"], out["notes"] = detect(paths, read_file)
        except RepoUnreadable as exc:
            out["error"] = str(exc)
        except Exception as exc:  # network trouble must never break the chat
            log.warning("reading %s failed: %r", ref.label, exc)
            out["error"] = "GitHub didn't answer in time" if "timed out" in str(exc) else "something went wrong"
        return out
