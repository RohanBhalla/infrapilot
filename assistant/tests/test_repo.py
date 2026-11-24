"""Reading a linked GitHub repo for context (decision 006): detection rules, the reader (fake
network) and how the dialog uses the facts."""

from __future__ import annotations

import io
import json
import urllib.error

from infrapilot_assistant.classifier import Classification
from infrapilot_assistant.dialog import Assistant, step
from infrapilot_assistant.repo import GitHubReader, RepoRef, detect, find_repo
from infrapilot_assistant.state import DialogState

VERSIONS = {"nodejs": {"lts": "24.21.0"}}


def C(intent, conf=0.95, params=None):
    return Classification(intent=intent, confidence=conf, standalone_question="q", params=params or {})


def facts(files: dict[str, str], extra: set[str] = frozenset()):
    return detect(set(files) | set(extra), files.get)


def pkg(**deps):
    return json.dumps({"dependencies": deps})


def test_finds_repo_links_and_ignores_other_hosts():
    assert find_repo("host https://github.com/acme/web-site.git please") == RepoRef("acme", "web-site")
    assert find_repo("see github.com/acme/app/tree/main, thanks") == RepoRef("acme", "app", "main")
    assert find_repo("(https://github.com/a/b)") == RepoRef("a", "b")
    assert find_repo("https://gitlab.com/acme/app") is None
    assert find_repo("https://github.com/acme") is None
    assert find_repo("https://github.com/acme/../etc") is None


def test_detects_the_framework_and_its_build_folder():
    assert facts({"package.json": pkg(react="19", vite="7")})[0] == {
        "language": "node", "lambda_runtime": "node", "package_manager": "npm", "framework": "react",
        "site_dir": "dist"}
    assert facts({"package.json": pkg(react="19", **{"react-scripts": "5"})})[0]["site_dir"] == "build"
    f, notes = facts({"package.json": pkg(next="16", react="19")})
    assert f["site_dir"] == "out" and any("output: 'export'" in n for n in notes)
    angular = {"package.json": pkg(**{"@angular/core": "21"}), "angular.json": '{"projects": {"shop": {}}}'}
    assert facts(angular)[0]["site_dir"] == "dist/shop/browser"
    assert facts({"package.json": pkg(vue="3", vite="7")})[0]["framework"] == "vue"


def test_plain_html_sites():
    assert facts({}, {"public/index.html", "README.md"})[0] == {"framework": "plain", "site_dir": "public"}
    f, notes = facts({}, {"index.html"})
    assert f == {"framework": "plain"} and "which folder to upload" in notes[0]


def test_language_package_manager_and_version():
    node = facts({"package.json": json.dumps({"engines": {"node": ">=22"}})}, {"pnpm-lock.yaml"})[0]
    assert node["package_manager"] == "pnpm" and node["runtime_version"] == "22"
    assert facts({"package.json": "{}", ".nvmrc": "v24.1.0\n"}, {"yarn.lock"})[0]["runtime_version"] == "24"
    py = facts({"pyproject.toml": '[project]\nrequires-python = ">=3.13"\n[tool.poetry]\n'})[0]
    assert py == {"language": "python", "lambda_runtime": "python", "package_manager": "poetry",
                  "runtime_version": "3.13"}
    assert facts({"go.mod": "module x\n\ngo 1.25\n"})[0] == {"language": "go", "package_manager": "go",
                                                             "runtime_version": "1.25"}
    assert facts({}, {"pom.xml"})[0] == {"language": "java", "package_manager": "maven"}
    f, notes = facts({"go.mod": "go 1.25"}, {"requirements.txt"})
    assert "language" not in f and "go and python" in notes[0]


def test_port_from_the_dockerfile_and_bad_files_are_skipped():
    f, _ = facts({"requirements.txt": "", "Dockerfile": "FROM python\nEXPOSE 8080/tcp\n"})
    assert f["app_port"] == 8080
    f, notes = facts({"package.json": "{not json"})
    assert f["language"] == "node" and "isn't valid JSON" in notes[0]


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_github(files: dict[str, str], api_status: int = 200, private: bool = False):
    calls = []

    def urlopen(req, timeout):
        url, calls[len(calls):] = req.full_url, [req.full_url]
        if "api.github.com" in url:
            if private:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            if api_status != 200:
                raise urllib.error.HTTPError(url, api_status, "limit", {}, None)
            names = {p.split("/")[0] for p in files}
            return FakeResp(json.dumps({"tree": [{"path": n} for n in names]}).encode())
        path = url.split("/HEAD/", 1)[1]
        if path not in files or private:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResp(files[path].encode())
    return urlopen, calls


def test_reader_lists_the_repo_then_reads_only_what_it_needs():
    urlopen, calls = fake_github({"package.json": pkg(react="19", vite="7"), "yarn.lock": "x" * 10,
                                  "src/main.tsx": ""})
    out = GitHubReader(urlopen=urlopen).read(RepoRef("acme", "web"))
    assert out["error"] is None and out["facts"]["site_dir"] == "dist" and out["facts"]["package_manager"] == "yarn"
    assert calls[0] == "https://api.github.com/repos/acme/web/git/trees/HEAD"
    assert not any("yarn.lock" in c or "src" in c for c in calls)  # lock files: name only


def test_reader_falls_back_to_checking_files_when_rate_limited():
    urlopen, _ = fake_github({"public/index.html": "<h1>hi</h1>"}, api_status=403)
    out = GitHubReader(urlopen=urlopen).read(RepoRef("acme", "web"))
    assert out["facts"] == {"framework": "plain", "site_dir": "public"}


def test_private_or_missing_repos_and_network_errors_are_reported_not_raised():
    urlopen, _ = fake_github({}, private=True)
    assert "private" in GitHubReader(urlopen=urlopen).read(RepoRef("acme", "secret"))["error"]

    def down(req, timeout):
        raise TimeoutError("timed out")
    assert GitHubReader(urlopen=down).read(RepoRef("a", "b"))["error"] == "GitHub didn't answer in time"


REACT = {"repo": "github.com/acme/web", "error": None, "notes": ["`dist` is where react puts it."],
         "facts": {"language": "node", "lambda_runtime": "node", "package_manager": "npm",
                   "framework": "react", "site_dir": "dist"}}


def test_repo_answers_the_build_folder_question():
    st = DialogState()
    t = step("host https://github.com/acme/web on s3", st,
             C("deploy_static_site", params={"source_dir": "https://github.com/acme/web"}), VERSIONS, REACT)
    assert t.type == "ready" and t.params["source_dir"] == "dist" and t.params["spa_routing"] is True
    assert t.message.startswith("From github.com/acme/web I found: framework: react; source dir: dist.")


def test_users_own_words_win_and_facts_carry_over_to_the_next_task():
    st = DialogState()
    t = step("host github.com/acme/web, it builds to site", st,
             C("deploy_static_site", params={"source_dir": "site"}), VERSIONS, REACT)
    assert t.params["source_dir"] == "site" and t.params["framework"] == "react"
    t = step("now a CI workflow for it", st, C("cicd_github_actions"), VERSIONS)
    assert t.type == "ready" and t.params["language"] == "node" and t.params["package_manager"] == "npm"
    assert "From github.com" not in t.message  # only said when the repo is first read


def test_unreadable_repo_falls_back_to_asking():
    repo = {"repo": "github.com/acme/secret", "error": "it's private or doesn't exist", "facts": {}, "notes": []}
    t = step("host github.com/acme/secret", DialogState(), C("deploy_static_site"), VERSIONS, repo)
    assert t.type == "clarification" and t.message.startswith("I couldn't read github.com/acme/secret")


class FakeClassifier:
    def __init__(self, c):
        self.c = c

    def classify(self, message, state, history):
        return self.c


class CountingReader:
    def __init__(self):
        self.reads = 0

    def read(self, ref):
        self.reads += 1
        return {**REACT, "repo": ref.label}


def test_assistant_reads_each_linked_repo_once():
    reader, st = CountingReader(), DialogState()
    a = Assistant(FakeClassifier(C("deploy_static_site")), VERSIONS, reader)
    assert a.handle("host https://github.com/acme/web", st).params["source_dir"] == "dist"
    a.handle("again https://github.com/acme/web", st)
    a.handle("no link here", st)
    assert reader.reads == 1
    a.handle("this one: github.com/acme/other", st)
    assert reader.reads == 2 and st.repo["repo"] == "github.com/acme/other"
