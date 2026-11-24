"""A user's built static site, uploaded as a zip (decision 006, option 2).

`check` decides whether a zip can go into a sandbox's site bucket, and returns its files. It runs
twice: when the user uploads (so they hear why straight away) and again in the sandbox ops
Lambda before copying, since the upload link could be used again in between.

Rules: at most 20 MB zipped, 50 MB unzipped and 1,000 files; web file types only; no links, no
absolute or `..` paths; an `index.html` at the top. If everything sits in one folder (a zipped
`dist/`), that folder is the top. macOS extras (`__MACOSX/`, `.DS_Store`) and hidden files are
skipped. Nothing in the zip runs on our side: the files are only copied to S3.
"""

from __future__ import annotations

import io
import mimetypes
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_SITE_BYTES = 50 * 1024 * 1024
MAX_FILES = 1000

TYPES = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css", ".js": "text/javascript",
    ".mjs": "text/javascript", ".map": "application/json", ".json": "application/json",
    ".webmanifest": "application/manifest+json", ".txt": "text/plain", ".xml": "application/xml",
    ".ico": "image/x-icon", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp", ".avif": "image/avif",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject", ".mp4": "video/mp4", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".pdf": "application/pdf", ".wasm": "application/wasm",
}


class SiteRefused(ValueError):
    """Why the zip can't be used, in words for the user."""


@dataclass
class Site:
    files: dict[str, bytes] = field(default_factory=dict)  # path in the bucket -> content
    skipped: int = 0  # hidden files and macOS extras

    @property
    def size(self) -> int:
        return sum(len(b) for b in self.files.values())


def content_type(path: str) -> str:
    return TYPES.get(PurePosixPath(path).suffix.lower()) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def _names(names: list[str], what: str) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:3])
    return f"{what}: {shown}" + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def check(data: bytes) -> Site:
    if len(data) > MAX_ZIP_BYTES:
        raise SiteRefused(f"The zip is larger than {MAX_ZIP_BYTES // 2**20} MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SiteRefused("That file isn't a zip.") from exc

    entries, skipped, bad_paths, bad_types = [], 0, [], []
    for info in archive.infolist():
        name = info.filename
        if info.is_dir():
            continue
        parts = PurePosixPath(name).parts
        if "\\" in name or name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
            bad_paths.append(name)
            continue
        if stat.S_ISLNK(info.external_attr >> 16):
            bad_paths.append(name)
            continue
        if parts[0] == "__MACOSX" or any(p.startswith(".") and p != ".well-known" for p in parts):
            skipped += 1
            continue
        if PurePosixPath(name).suffix.lower() not in TYPES:
            bad_types.append(name)
            continue
        entries.append(info)
    if bad_paths:
        raise SiteRefused(_names(bad_paths, "The zip has links or paths outside its folder") + ".")
    if bad_types:
        raise SiteRefused(_names(bad_types, "Only web files (HTML, CSS, JavaScript, images, fonts, media) "
                                            "can go on the site. Not allowed") + ".")
    if not entries:
        raise SiteRefused("The zip has no site files in it.")
    if len(entries) > MAX_FILES:
        raise SiteRefused(f"The zip has {len(entries)} files; the limit is {MAX_FILES}.")
    if sum(i.file_size for i in entries) > MAX_SITE_BYTES:
        raise SiteRefused(f"The site is larger than {MAX_SITE_BYTES // 2**20} MB unzipped.")

    # A zipped `dist/` folder: everything under one folder, so that folder is the site's top.
    names = [i.filename for i in entries]
    tops = {PurePosixPath(n).parts[0] for n in names}
    prefix = ""
    if "index.html" not in names and len(tops) == 1 and all(len(PurePosixPath(n).parts) > 1 for n in names):
        prefix = next(iter(tops)) + "/"
    if f"{prefix}index.html" not in names:
        raise SiteRefused("The zip needs an `index.html` at the top (or inside its one folder, like `dist/`).")

    site = Site(skipped=skipped)
    for info in entries:
        try:
            # Reads at most the size the zip declares, so the size check above holds.
            site.files[info.filename.removeprefix(prefix)] = archive.read(info)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:  # bad CRC, encrypted, odd method
            raise SiteRefused(f"`{info.filename}` couldn't be read from the zip ({exc}).") from exc
    return site
