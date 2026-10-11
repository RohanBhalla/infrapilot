"""Source registry: the team sheet (sources.csv) + proposed additions + collection rules."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import httpx
import yaml

from .config import SETTINGS, SHEET_CSV_URL, USER_AGENT
from .models import SourceSpec

EXPECTED_COLUMNS = ["#", "Category", "Document", "URL", "Type", "Filter / Notes"]


def sync_sheet(dest: Path | None = None, url: str = SHEET_CSV_URL) -> Path:
    """Download the Google Sheet as CSV and validate its columns."""
    dest = dest or SETTINGS.paths.sources_dir / "sources.csv"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    rows = list(csv.reader(io.StringIO(resp.text)))
    if not rows or rows[0] != EXPECTED_COLUMNS:
        raise ValueError(f"Unexpected sheet columns: {rows[0] if rows else 'empty'}")
    dest.write_text(resp.text)
    return dest


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("#", "").strip()]


def load_sources(sources_dir: Path | None = None, include_additional: bool = True) -> list[SourceSpec]:
    sources_dir = sources_dir or SETTINGS.paths.sources_dir
    config = yaml.safe_load((sources_dir / "source_config.yaml").read_text())
    defaults, rules = config.get("defaults", {}), config.get("sources", {})

    rows = [(r, "sheet") for r in _read_rows(sources_dir / "sources.csv")]
    if include_additional:
        rows += [(r, "additional") for r in _read_rows(sources_dir / "additional_sources.csv")]

    specs: list[SourceSpec] = []
    for row, origin in rows:
        sid = row["#"].strip()
        rule = rules.get(sid)
        if rule is None:
            raise KeyError(f"Source #{sid} ({row['Document']}) has no entry in source_config.yaml")
        params = {"max_pages": defaults.get("max_pages", 150), **(rule.get("params") or {})}
        specs.append(
            SourceSpec(
                id=sid,
                category=row["Category"].strip(),
                name=row["Document"].strip(),
                url=row["URL"].strip(),
                sheet_type=row["Type"].strip(),
                notes=(row.get("Filter / Notes") or "").strip(),
                origin=origin,
                strategy=rule["strategy"],
                doc_type=rule.get("doc_type", defaults.get("doc_type", "reference")),
                params=params,
                skip_reason=rule.get("skip_reason"),
            )
        )
    return specs


def select(specs: list[SourceSpec], ids: list[str] | None = None, category: str | None = None,
           strategy: str | None = None, include_skipped: bool = False) -> list[SourceSpec]:
    out = []
    for s in specs:
        if ids and s.id not in ids:
            continue
        if category and category.lower() not in s.category.lower():
            continue
        if strategy and s.strategy != strategy:
            continue
        if not include_skipped and not s.enabled:
            continue
        out.append(s)
    return out
