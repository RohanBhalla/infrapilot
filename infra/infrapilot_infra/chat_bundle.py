"""Package the chat Lambda without Docker: pip downloads Linux ARM wheels on this machine.

The bundle holds our three packages (api, assistant, rag), the pinned third-party packages in
api/requirements-lambda.txt, and versions.json. LanceDB and pyarrow (local search only) are
left out, which keeps it small enough for fast cold starts.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as lambda_

REPO = Path(__file__).resolve().parents[2]
PACKAGES = ["rag", "assistant", "api"]
REQUIREMENTS = REPO / "api" / "requirements-lambda.txt"
VERSIONS = REPO / "data_collection" / "data" / "versions" / "versions.json"


def _sources() -> list[Path]:
    files = [REQUIREMENTS, VERSIONS]
    for pkg in PACKAGES:
        files += sorted(p for p in (REPO / pkg).glob("infrapilot_*/**/*.py"))
        files.append(REPO / pkg / "pyproject.toml")
    return files


def source_hash() -> str:
    """Changes when any bundled source changes, so unchanged code isn't rebuilt or redeployed."""
    h = hashlib.sha256()
    for f in _sources():
        h.update(str(f.relative_to(REPO)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@jsii.implements(ILocalBundling)
class _PipBundle:
    def try_bundle(self, output_dir: str, *, image=None, **_) -> bool:
        pip = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
               "--target", output_dir]
        subprocess.run(pip + ["--platform", "manylinux2014_aarch64", "--implementation", "cp",
                              "--python-version", "3.12", "--only-binary=:all:", "-r", str(REQUIREMENTS)],
                       check=True)
        shutil.rmtree(Path(output_dir) / "bin", ignore_errors=True)  # console scripts: not used on Lambda
        subprocess.run(pip + ["--no-deps"] + [str(REPO / p) for p in PACKAGES], check=True)
        shutil.rmtree(Path(output_dir) / "bin", ignore_errors=True)
        data = Path(output_dir) / "data" / "versions"
        data.mkdir(parents=True)
        shutil.copy(VERSIONS, data / "versions.json")
        return True


def chat_code() -> lambda_.Code:
    if not VERSIONS.exists():
        raise FileNotFoundError(f"{VERSIONS} is missing: run the data_collection version step first")
    return lambda_.Code.from_asset(
        str(REPO / "api"),
        asset_hash_type=AssetHashType.CUSTOM,
        asset_hash=source_hash(),
        bundling=BundlingOptions(image=DockerImage.from_registry("unused"), local=_PipBundle()),
    )
