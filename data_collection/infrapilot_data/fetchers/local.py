"""Team-authored content (golden templates, guardrail policy) from data_collection/uploads/."""

from __future__ import annotations

from ..config import SETTINGS
from ..models import FetchResult, RawDoc, SourceSpec
from .github import CODE_LANG
from .base import FetchContext

TEXT_SUFFIXES = {".md", ".markdown", ".txt", *CODE_LANG}


def fetch_upload(spec: SourceSpec, ctx: FetchContext) -> FetchResult:
    result = FetchResult(source_id=spec.id)
    root = SETTINGS.paths.root / spec.params["dir"]
    if not root.exists():
        result.errors.append(f"{root} does not exist yet")
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES or path.name == "README.md":
            continue
        rel = path.relative_to(SETTINGS.paths.root).as_posix()
        is_md = path.suffix.lower() in (".md", ".markdown", ".txt")
        result.docs.append(RawDoc(
            source_id=spec.id,
            url=f"https://github.com/RohanBhalla/infrapilot/blob/main/data_collection/{rel}",
            title=None if is_md else path.name,
            format="markdown" if is_md else "code",
            language=None if is_md else CODE_LANG.get(path.suffix.lower()),
            body=path.read_text(),
            extra={"path": rel},
        ))
    return result
