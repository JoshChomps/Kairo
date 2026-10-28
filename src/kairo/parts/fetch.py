"""Datasheets fetched once from their makers and kept in Kairo's folder.

A chip's limits are in its datasheet and nowhere else Kairo can reach,
so Kairo fetches the PDF itself rather than asking a person to. The link
comes from the caller, the KiCad symbol's Datasheet field, or the parts
catalogue's listing. Each file is kept in the ``datasheets`` folder of
Kairo's cache with a record of where it came from, its SHA-256 and the
day it was fetched (``index.json``), so a figure read from it can name
exactly which document it came from, and a link already fetched is never
fetched again.

What comes back must be a PDF (it starts ``%PDF-``): a maker's site that
answers with a web page in place of the file is refused, not kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse

from kairo import __version__

#: The folder of Kairo's cache that holds them.
DATASHEETS = "datasheets"
#: The record of each kept file.
INDEX = "index.json"
#: Seconds to wait for a maker's site.
TIMEOUT_S = 60.0
#: The largest file kept, in bytes. The datasheets tried were 0.8 MB to
#: 3.7 MB; a file past this is more likely a catalogue than one part's.
MAX_BYTES = 50_000_000
#: How Kairo names itself to a maker's site.
USER_AGENT = f"Kairo/{__version__} (datasheet fetch)"


class FetchError(ValueError):
    """A datasheet could not be fetched; the message starts with its code."""


class Response(Protocol):
    """What `urllib.request.urlopen` returns, as far as this module reads it."""

    def read(self, amt: int, /) -> bytes:
        """Read up to ``amt`` bytes."""
        ...

    def geturl(self) -> str:
        """The address the content came from, after any redirect."""
        ...

    def __enter__(self) -> Response:
        """Open."""
        ...

    def __exit__(self, *args: object) -> None:
        """Close."""
        ...


Opener = Callable[[urllib.request.Request, float], Response]


def _open(request: urllib.request.Request, timeout: float) -> Response:
    """Open a request over the network."""
    response: Response = urllib.request.urlopen(request, timeout=timeout)
    return response


@dataclass(frozen=True, slots=True)
class Kept:
    """A datasheet in Kairo's folder.

    Attributes:
        file: Its name in the datasheets folder, which `read_datasheet`
            takes.
        url: The link it was asked for by.
        source: Where it came from, after any redirect.
        sha256: Its hash.
        size: Its size in bytes.
        fetched: The day it was fetched, as ``YYYY-MM-DD``.
        already: True if it was kept before this call, so nothing was
            fetched.
    """

    file: str
    url: str
    source: str
    sha256: str
    size: int
    fetched: str
    already: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The record as plain data."""
        return asdict(self)


def fetch_datasheet(
    folder: Path,
    url: str,
    *,
    opener: Opener = _open,
    today: str | None = None,
) -> Kept:
    """A datasheet, from Kairo's folder if kept there, else from its maker.

    Args:
        folder: Kairo's datasheets folder; made if missing.
        url: The link, ``http`` or ``https``.
        opener: Opens a request; the network's by default.
        today: The date to record; today's in UTC by default.

    Returns:
        The kept file's record.

    Raises:
        FetchError: ``E_DATASHEET_LINK`` for a link that is not http or
            https; ``E_DATASHEET_FETCH`` if the site cannot be reached or
            refuses; ``E_DATASHEET_NOT_PDF`` if what came back is not a
            PDF; ``E_DATASHEET_TOO_LARGE`` past `MAX_BYTES`.
    """
    if urlparse(url).scheme not in ("http", "https"):
        raise FetchError(f"E_DATASHEET_LINK: {url!r} is not an http or https link")
    records = _records(folder)
    known = records.get(url)
    if known is not None:
        path = folder / str(known["file"])
        if path.is_file() and _sha256(path.read_bytes()) == known["sha256"]:
            return Kept(
                str(known["file"]),
                url,
                str(known["source"]),
                str(known["sha256"]),
                int(known["size"]),
                str(known["fetched"]),
                already=True,
            )
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"}
    )
    try:
        with opener(request, TIMEOUT_S) as response:
            data = response.read(MAX_BYTES + 1)
            source = response.geturl()
    except OSError as exc:
        raise FetchError(f"E_DATASHEET_FETCH: {url}: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise FetchError(
            f"E_DATASHEET_TOO_LARGE: {url} is over {MAX_BYTES // 1_000_000} MB"
        )
    if not data.startswith(b"%PDF-"):
        raise FetchError(
            f"E_DATASHEET_NOT_PDF: {url} gave something that is not a PDF "
            f"(it starts {data[:15]!r}); the site may want a person to open it"
        )
    digest = _sha256(data)
    name = _name(source, digest, records, url)
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f"{name}.partial"
    partial.write_bytes(data)
    partial.replace(folder / name)
    kept = Kept(
        name,
        url,
        source,
        digest,
        len(data),
        today or datetime.now(UTC).date().isoformat(),
    )
    record = kept.as_dict()
    del record["already"], record["url"]
    records[url] = record
    _write_records(folder, records)
    return kept


def _name(source: str, digest: str, records: dict[str, Any], url: str) -> str:
    """A file name from the link's last part, kept apart from other links'."""
    last = unquote(urlparse(source).path.rsplit("/", 1)[-1])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", last).strip("._") or "datasheet"
    if not stem.lower().endswith(".pdf"):
        stem += ".pdf"
    taken = {str(r["file"]) for u, r in records.items() if u != url}
    if stem in taken:
        stem = f"{digest[:8]}-{stem}"
    return stem


def _records(folder: Path) -> dict[str, Any]:
    """The index of kept files, by link; empty if there is none or it is unreadable."""
    try:
        data = json.loads((folder / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return cast("dict[str, Any]", data)


def _write_records(folder: Path, records: dict[str, Any]) -> None:
    """Write the index of kept files."""
    partial = folder / f"{INDEX}.partial"
    partial.write_text(
        json.dumps(records, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    partial.replace(folder / INDEX)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
