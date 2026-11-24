"""A user's own static site, uploaded as a zip (decision 006): the zip rules, the API flow, the
S3 upload link, and the ops Lambda copying the site into the sandbox bucket."""

from __future__ import annotations

import io
import stat
import zipfile

import boto3
import pytest
from moto import mock_aws

from infrapilot_api import site_zip
from infrapilot_api.artifacts import InMemoryPlanStore, InMemorySiteUploads, S3SiteUploads, site_key
from infrapilot_api.aws_deployer import bundle_key
from infrapilot_api.deploy_requests import InMemoryRequestStore
from infrapilot_api.deploys import ConflictError, DeployService, DryRunDeployer, NotDeployableError
from infrapilot_api.service import NotFound
from infrapilot_assistant.classifier import Classification
from infrapilot_assistant.sandbox import Bundle

from test_api import VERSIONS, make, new_session
from test_deploys import lambda_plan, table  # noqa: F401  (fixture)
from test_sandbox_ops import Ctx, FakeCfn, deploying, ops  # noqa: F401  (fixture)

SITE = Classification("deploy_static_site", 0.95, "host my site", {"source_dir": "dist", "ci_deploy": "no"})


def make_zip(files: dict[str, bytes | str], links: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


DIST = make_zip({"dist/index.html": "<h1>mine</h1>", "dist/assets/app.js": "console.log(1)",
                 "dist/assets/app.css": "h1{}", "dist/.DS_Store": "x", "__MACOSX/dist/._index.html": "x"})


# The zip rules ----------------------------------------------------------------------------------

def test_a_zipped_dist_folder_becomes_the_site_top_and_extras_are_skipped():
    site = site_zip.check(DIST)
    assert sorted(site.files) == ["assets/app.css", "assets/app.js", "index.html"]
    assert site.skipped == 2 and site.files["index.html"] == b"<h1>mine</h1>"
    assert site_zip.check(make_zip({"index.html": "x", "about/index.html": "y"})).files.keys() == {
        "index.html", "about/index.html"}


@pytest.mark.parametrize("data, reason", [
    (b"not a zip", "isn't a zip"),
    (make_zip({"about.html": "x"}), "index.html"),
    (make_zip({"a/index.html": "x", "b/x.css": "y"}), "index.html"),  # two folders: no single top
    (make_zip({"index.html": "x", "server.py": "import os", "run.sh": "rm -rf /"}), "`server.py`, `run.sh`"),
    (make_zip({"index.html": "x", "../evil.html": "x"}), "outside its folder"),
    (make_zip({"index.html": "x", "/abs.html": "x"}), "outside its folder"),
    (make_zip({"index.html": "x"}, links=("link.html",)), "links"),
    (make_zip({".env": "SECRET=1"}), "no site files"),
])
def test_bad_zips_are_refused_with_a_reason(data, reason):
    with pytest.raises(site_zip.SiteRefused, match=reason):
        site_zip.check(data)


def test_size_and_file_count_limits(monkeypatch):
    monkeypatch.setattr(site_zip, "MAX_FILES", 2)
    with pytest.raises(site_zip.SiteRefused, match="limit is 2"):
        site_zip.check(make_zip({"index.html": "x", "a.css": "x", "b.js": "x"}))
    monkeypatch.setattr(site_zip, "MAX_SITE_BYTES", 1000)
    with pytest.raises(site_zip.SiteRefused, match="unzipped"):  # compresses well: a small zip, a big site
        site_zip.check(make_zip({"index.html": "x" * 5000}))
    assert site_zip.content_type("a/b.woff2") == "font/woff2" and site_zip.content_type("x.HTML") == "text/html"


# The API, locally (the browser PUTs the zip to the local API) -------------------------------------

def site_request():
    client, _, _ = make(None, SITE)
    sid = new_session(client)
    mid = client.post(f"/sessions/{sid}/messages", json={"content": "host my site"}).json()["message"]["id"]
    return client, client.post("/requests", json={"session_id": sid, "message_id": mid}).json()


def test_upload_check_and_deploy_with_your_own_site():
    client, req = site_request()
    rid = req["id"]
    link = client.post(f"/requests/{rid}/upload").json()
    assert link == {"method": "PUT", "url": f"/requests/{rid}/site", "fields": {}, "max_bytes": 20 * 2**20}
    assert client.post(f"/requests/{rid}/upload/check").status_code == 404  # nothing uploaded yet

    assert client.put(link["url"], content=make_zip({"index.html": "x", "run.sh": "x"})).status_code == 204
    r = client.post(f"/requests/{rid}/upload/check")
    assert r.status_code == 422 and "`run.sh`" in r.json()["detail"]

    client.put(link["url"], content=DIST)
    checked = client.post(f"/requests/{rid}/upload/check").json()
    assert checked["site_files"] == 3 and checked["site_bytes"] == len("<h1>mine</h1>console.log(1)h1{}")
    assert client.post(f"/requests/{rid}/approve").json()["status"] == "deployed"
    assert client.post(f"/requests/{rid}/upload").status_code == 409  # too late now


def test_a_refused_zip_forgets_the_earlier_good_one():
    client, req = site_request()
    rid = req["id"]
    client.put(f"/requests/{rid}/site", content=DIST)
    assert client.post(f"/requests/{rid}/upload/check").json()["site_files"] == 3
    client.put(f"/requests/{rid}/site", content=b"oops")
    assert client.post(f"/requests/{rid}/upload/check").status_code == 422
    assert client.get(f"/requests/{rid}").json()["site_files"] is None  # the sample page again


class Capture(DryRunDeployer):
    def start(self, req, bundle):
        self.bundle = bundle
        return super().start(req, bundle)


def test_only_static_sites_take_uploads_and_the_bundle_points_at_the_zip():
    from infrapilot_assistant.generators import generate

    plans, uploads, deployer = InMemoryPlanStore(), InMemorySiteUploads(), Capture()
    plans.save("alice", "s1", "site", generate("deploy_static_site", {"source_dir": "dist", "ci_deploy": False}))
    plans.save("alice", "s1", "fn", lambda_plan())
    svc = DeployService(InMemoryRequestStore(), plans, deployer, uploads=uploads)

    fn = svc.create("alice", "s1", "fn")
    with pytest.raises(NotDeployableError, match="Only a static site"):
        svc.upload_link("alice", fn.id)
    site = svc.create("alice", "s1", "site")
    with pytest.raises(NotFound):
        svc.upload_link("bob", site.id)  # someone else's request

    uploads.put(site.id, DIST)
    svc.check_upload("alice", site.id)
    uploads._files.clear()  # deleted after a day
    with pytest.raises(NotFound, match="expired"):
        svc.approve("alice", site.id)
    uploads.put(site.id, DIST)
    svc.approve("alice", site.id)
    assert deployer.bundle.site_zip == site_key(site.id) and deployer.bundle.uploads == {}
    with pytest.raises(ConflictError):
        svc.check_upload("alice", site.id)


def test_without_an_upload_the_sample_page_is_deployed():
    from infrapilot_assistant.generators import generate

    plans, deployer = InMemoryPlanStore(), Capture()
    plans.save("alice", "s1", "site", generate("deploy_static_site", {"source_dir": "dist", "ci_deploy": False}))
    svc = DeployService(InMemoryRequestStore(), plans, deployer, uploads=InMemorySiteUploads())
    svc.approve("alice", svc.create("alice", "s1", "site").id)
    assert deployer.bundle.site_zip is None and "index.html" in deployer.bundle.uploads


# AWS: the presigned upload link, and the ops Lambda's copy ----------------------------------------

@mock_aws
def test_s3_upload_link_limits_the_size_and_key(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="artifacts")
    uploads = S3SiteUploads("artifacts", s3)
    link = uploads.link("abc1234567", 20 * 2**20)
    assert link["method"] == "POST" and link["fields"]["key"] == "uploads/abc1234567/site.zip"
    assert "policy" in link["fields"] and not uploads.exists("abc1234567")
    assert uploads.read("abc1234567", 10) is None
    s3.put_object(Bucket="artifacts", Key=site_key("abc1234567"), Body=b"x" * 50)
    assert uploads.exists("abc1234567") and len(uploads.read("abc1234567", 10)) == 11


def test_deployed_copies_the_users_site_instead_of_the_sample(ops):  # noqa: F811
    h, store, s3 = ops
    req = deploying(store)
    site = req.stack_name + "-sitebucket-abc"
    s3.create_bucket(Bucket=site)
    s3.put_object(Bucket="artifacts", Key=site_key(req.id), Body=DIST)
    bundle = Bundle(request_id=req.id, stack_name=req.stack_name, template={}, upload_to="BucketName",
                    site_zip=site_key(req.id))
    s3.put_object(Bucket="artifacts", Key=bundle_key(req.id), Body=bundle.model_dump_json())
    h._clients["cloudformation"] = FakeCfn({"sid": {"StackStatus": "CREATE_COMPLETE", "Outputs": [
        {"OutputKey": "BucketName", "OutputValue": site}]}})

    h.handler({"action": "deployed", "request_id": req.id, "stack": "sid"}, Ctx())
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=site)["Contents"])
    assert keys == ["assets/app.css", "assets/app.js", "index.html"]
    assert s3.get_object(Bucket=site, Key="assets/app.js")["ContentType"] == "text/javascript"
    assert store.get(req.id).status == "deployed"


def test_a_zip_swapped_after_the_check_is_refused_by_the_ops_lambda(ops):  # noqa: F811
    h, store, s3 = ops
    req = deploying(store)
    site = req.stack_name + "-sitebucket-abc"
    s3.create_bucket(Bucket=site)
    s3.put_object(Bucket="artifacts", Key=site_key(req.id), Body=make_zip({"index.html": "x", "x.php": "x"}))
    bundle = Bundle(request_id=req.id, stack_name=req.stack_name, template={}, upload_to="BucketName",
                    site_zip=site_key(req.id))
    s3.put_object(Bucket="artifacts", Key=bundle_key(req.id), Body=bundle.model_dump_json())
    h._clients["cloudformation"] = FakeCfn({"sid": {"StackStatus": "CREATE_COMPLETE", "Outputs": [
        {"OutputKey": "BucketName", "OutputValue": site}]}})
    with pytest.raises(site_zip.SiteRefused):  # the workflow catches it and records `failed`
        h.handler({"action": "deployed", "request_id": req.id, "stack": "sid"}, Ctx())
    assert "Contents" not in s3.list_objects_v2(Bucket=site)
