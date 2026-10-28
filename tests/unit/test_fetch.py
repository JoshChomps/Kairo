"""Datasheets fetched once and kept, through a stand-in for the network."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kairo.parts.fetch import INDEX, MAX_BYTES, FetchError, Kept, fetch_datasheet

PDF = b"%PDF-1.7\nnot a real document, only its first bytes\n"


class Site:
    """Answers each link with its content, counting the requests."""

    def __init__(self, pages: dict[str, tuple[str, bytes]]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Answer:
        url = request.full_url
        self.asked.append(url)
        assert request.get_header("User-agent", "").startswith("Kairo/")
        if url not in self.pages:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        source, body = self.pages[url]
        return Answer(source, body)


class Answer:
    def __init__(self, source: str, body: bytes) -> None:
        self.source, self.body = source, body

    def read(self, amt: int, /) -> bytes:
        return self.body[:amt]

    def geturl(self) -> str:
        return self.source

    def __enter__(self) -> Answer:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_a_datasheet_is_fetched_once_and_kept_with_its_record(tmp_path: Path) -> None:
    link = "http://www.example.com/lit/ds/symlink/part1.pdf"
    site = Site({link: ("https://www.example.com/lit/ds/symlink/part1.pdf", PDF)})
    first = fetch_datasheet(tmp_path, link, opener=site, today="2026-10-13")
    assert first == Kept(
        "part1.pdf",
        link,
        "https://www.example.com/lit/ds/symlink/part1.pdf",
        first.sha256,
        len(PDF),
        "2026-10-13",
    )
    assert (tmp_path / "part1.pdf").read_bytes() == PDF
    record = json.loads((tmp_path / INDEX).read_text(encoding="utf-8"))
    assert record[link]["sha256"] == first.sha256
    # Asked again, it comes from the folder and the site is not asked.
    again = fetch_datasheet(tmp_path, link, opener=site, today="2026-10-14")
    assert again.already and again.fetched == "2026-10-13"
    assert site.asked == [link]
    # A file changed behind the record is fetched again.
    (tmp_path / "part1.pdf").write_bytes(PDF + b"edited")
    assert not fetch_datasheet(tmp_path, link, opener=site).already
    assert len(site.asked) == 2


def test_two_links_with_the_same_last_part_are_kept_apart(tmp_path: Path) -> None:
    one, two = "https://a.example/ds.pdf", "https://b.example/x/ds.pdf"
    site = Site({one: (one, PDF), two: (two, PDF + b"other")})
    assert fetch_datasheet(tmp_path, one, opener=site).file == "ds.pdf"
    second = fetch_datasheet(tmp_path, two, opener=site)
    assert second.file == f"{second.sha256[:8]}-ds.pdf"
    # A link with no file name of its own still gets one.
    bare = "https://c.example/download?id=7"
    site.pages[bare] = (bare, PDF)
    assert fetch_datasheet(tmp_path, bare, opener=site).file == "download.pdf"


def test_what_is_not_a_pdf_is_refused_and_nothing_is_kept(tmp_path: Path) -> None:
    page = "https://example.com/ask-a-person.pdf"
    huge = "https://example.com/catalogue.pdf"
    site = Site(
        {
            page: (page, b"<!DOCTYPE html><html>sign in</html>"),
            huge: (huge, PDF + b"x" * MAX_BYTES),
        }
    )
    with pytest.raises(FetchError, match="E_DATASHEET_NOT_PDF"):
        fetch_datasheet(tmp_path, page, opener=site)
    with pytest.raises(FetchError, match="E_DATASHEET_TOO_LARGE"):
        fetch_datasheet(tmp_path, huge, opener=site)
    with pytest.raises(FetchError, match=r"E_DATASHEET_FETCH: .*404"):
        fetch_datasheet(tmp_path, "https://example.com/gone.pdf", opener=site)
    with pytest.raises(FetchError, match="E_DATASHEET_LINK"):
        fetch_datasheet(tmp_path, "file:///C:/secret.pdf", opener=site)
    assert not (tmp_path / INDEX).exists()
    assert list(tmp_path.iterdir()) == []
