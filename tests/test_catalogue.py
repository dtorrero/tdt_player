"""Tests for :mod:`tdt_player.catalogue` (no network, no real cache)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tdt_player import catalogue

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


COUNTRIES = fixture("countries.json")
LANGUAGES = fixture("languages.json")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own ``$XDG_CACHE_HOME``."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


def fetcher(*, fail: str = ""):
    """A fake ``Fetcher`` that serves the fixtures (or raises)."""

    def fetch(url: str):
        if fail:
            raise RuntimeError(fail)
        if url == catalogue.COUNTRIES_URL:
            return COUNTRIES
        if url == catalogue.LANGUAGES_URL:
            return LANGUAGES
        raise AssertionError(f"unexpected url {url}")

    return fetch


def counting_fetcher(calls, *, fail: str = ""):
    inner = fetcher(fail=fail)

    def fetch(url: str):
        calls.append(url)
        return inner(url)

    return fetch


def test_country_entries_and_their_playlist_urls():
    data = catalogue.load_catalogue("country", fetch=fetcher())

    assert data.from_cache is False
    assert data.total == 4
    assert [entry.code for entry in data.entries] == ["AQ", "FR", "ES", "UK"]

    spain = next(entry for entry in data.entries if entry.code == "ES")
    assert spain.kind == "country"
    assert spain.url == "https://iptv-org.github.io/iptv/countries/es.m3u"
    assert spain.title == "IPTV-org (Spain)"
    assert spain.playlist_id == "iptv-org-country-es"
    assert spain.flag == "🇪🇸"
    assert spain.label == "🇪🇸 Spain (ES)"

    playlist = spain.to_playlist()
    assert playlist.url == spain.url
    assert playlist.name == "IPTV-org (Spain)"
    assert playlist.id == "iptv-org-country-es"


def test_languages_default_to_the_ones_spoken_in_a_listed_country():
    data = catalogue.load_catalogue("language", fetch=fetcher())

    assert data.total == 6
    assert {entry.code for entry in data.entries} == {"spa", "cat", "fra", "eng"}
    spanish = next(entry for entry in data.entries if entry.code == "spa")
    assert spanish.url == "https://iptv-org.github.io/iptv/languages/spa.m3u"
    assert spanish.title == "IPTV-org (Spanish)"
    assert spanish.playlist_id == "iptv-org-language-spa"


def test_languages_can_be_shown_in_full():
    data = catalogue.load_catalogue("language", all_languages=True, fetch=fetcher())
    assert {entry.code for entry in data.entries} == {
        "spa",
        "cat",
        "fra",
        "eng",
        "asm",
        "arg",
    }


def test_search_matches_codes_names_and_ignores_accents():
    data = catalogue.load_catalogue("language", all_languages=True, fetch=fetcher())

    assert [entry.code for entry in data.search("Spanish")] == ["spa"]
    assert [entry.code for entry in data.search("spa")] == ["spa"]
    assert [entry.code for entry in data.search("aragones")] == ["arg"]
    assert data.search("") == data.entries
    assert data.search("zzz") == []


def test_resolve_accepts_codes_names_and_unique_fragments():
    entries = catalogue.load_catalogue("country", fetch=fetcher()).entries

    assert catalogue.resolve(entries, "ES").code == "ES"
    assert catalogue.resolve(entries, "es").code == "ES"
    assert catalogue.resolve(entries, "Spain").code == "ES"
    assert catalogue.resolve(entries, "united").code == "UK"
    assert catalogue.resolve(entries, "Atlantis") is None
    assert catalogue.resolve(entries, "") is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        catalogue.load_catalogue("planet", fetch=fetcher())


def test_cache_is_written_and_reused_without_a_request():
    calls = []
    first = catalogue.load_catalogue("country", fetch=counting_fetcher(calls))
    assert first.from_cache is False
    assert calls == [catalogue.COUNTRIES_URL]

    calls.clear()
    second = catalogue.load_catalogue("country", fetch=counting_fetcher(calls))

    assert second.from_cache is True
    assert calls == []
    assert [entry.code for entry in second.entries] == [
        entry.code for entry in first.entries
    ]
    assert second.fetched_at > 0


def test_an_expired_cache_is_downloaded_again():
    now = 1_000_000.0
    catalogue.load_catalogue("country", fetch=fetcher(), now=now)

    calls = []
    later = now + catalogue.CACHE_TTL + 1
    data = catalogue.load_catalogue("country", fetch=counting_fetcher(calls), now=later)

    assert calls == [catalogue.COUNTRIES_URL]
    assert data.from_cache is False


def test_refresh_bypasses_a_fresh_cache():
    catalogue.load_catalogue("country", fetch=fetcher())

    calls = []
    catalogue.load_catalogue("country", fetch=counting_fetcher(calls), refresh=True)

    assert calls == [catalogue.COUNTRIES_URL]


def test_offline_falls_back_to_the_stale_cache():
    catalogue.load_catalogue("country", fetch=fetcher())

    data = catalogue.load_catalogue(
        "country", fetch=fetcher(fail="no route to host"), refresh=True
    )

    assert "no route to host" in data.error
    assert [entry.code for entry in data.entries] == ["AQ", "FR", "ES", "UK"]


def test_offline_without_a_cache_reports_the_error():
    data = catalogue.load_catalogue("country", fetch=fetcher(fail="boom"))

    assert data.entries == []
    assert "boom" in data.error


def test_a_corrupt_cache_is_ignored():
    catalogue.load_catalogue("country", fetch=fetcher())
    catalogue.cache_path("country").write_text("{not json", encoding="utf-8")

    data = catalogue.load_catalogue("country", fetch=fetcher())

    assert data.entries
    assert data.from_cache is False


def test_languages_still_load_when_the_country_index_fails():
    def fetch(url: str):
        if url == catalogue.COUNTRIES_URL:
            raise RuntimeError("countries are down")
        return LANGUAGES

    data = catalogue.load_catalogue("language", fetch=fetch)

    assert {entry.code for entry in data.entries} == {
        "spa",
        "cat",
        "fra",
        "eng",
        "asm",
        "arg",
    }


class FakeResponse:
    """Just enough of an ``http.client.HTTPResponse`` for ``probe_url``."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_probe_url_reads_the_status(monkeypatch):
    import urllib.error
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=0: FakeResponse(200)
    )
    assert catalogue.probe_url("https://example.test/a.m3u8") is True

    def not_found(request, timeout=0):
        raise urllib.error.HTTPError("https://example.test/a.m3u8", 404, "Not Found", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", not_found)
    assert catalogue.probe_url("https://example.test/a.m3u8") is False

    def offline(request, timeout=0):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    assert catalogue.probe_url("https://example.test/a.m3u8") is None


def test_probe_results_are_remembered_and_expire():
    catalogue.remember_probe("https://example.test/yes.m3u8", True, now=1000.0)
    catalogue.remember_probe("https://example.test/no.m3u8", False, now=1000.0)

    probes = catalogue.load_probes(now=1000.0)
    assert probes == {
        "https://example.test/yes.m3u8": True,
        "https://example.test/no.m3u8": False,
    }
    assert catalogue.load_probes(now=1000.0 + catalogue.CACHE_TTL + 1) == {}


def test_probe_with_cache_skips_known_urls():
    calls = []

    def probe(url: str):
        calls.append(url)
        return True

    cached = {"https://example.test/known.m3u8": False}

    assert (
        catalogue.probe_with_cache(
            "https://example.test/known.m3u8", cached=cached, probe=probe
        )
        is False
    )
    assert calls == []
    assert (
        catalogue.probe_with_cache(
            "https://example.test/new.m3u8", cached=cached, probe=probe
        )
        is True
    )
    assert calls == ["https://example.test/new.m3u8"]
    assert catalogue.load_probes()["https://example.test/new.m3u8"] is True


def test_add_entries_adds_once_and_reports_duplicates():
    from tdt_player import store

    library = store.Library(playlists=[])
    entries = catalogue.load_catalogue("country", fetch=fetcher()).entries
    spain = catalogue.resolve(entries, "ES")
    france = catalogue.resolve(entries, "FR")

    report = catalogue.add_entries(library, [spain, spain, france], probe=lambda url: True)

    assert [playlist.id for playlist in report.added] == [
        "iptv-org-country-es",
        "iptv-org-country-fr",
    ]
    assert report.existing == []
    assert report.summary == "2 added"
    assert [playlist.url for playlist in library.playlists] == [
        spain.url,
        france.url,
    ]

    again = catalogue.add_entries(library, [spain], probe=lambda url: True)
    assert again.added == []
    assert again.existing == [spain]
    assert again.summary == "1 already configured"


def test_add_entries_skips_a_playlist_that_does_not_exist():
    from tdt_player import store

    library = store.Library(playlists=[])
    entries = catalogue.load_catalogue("country", fetch=fetcher()).entries
    antarctica = catalogue.resolve(entries, "AQ")

    report = catalogue.add_entries(library, [antarctica], probe=lambda url: False)

    assert report.added == []
    assert report.unavailable == [antarctica]
    assert library.playlists == []
    assert "without a playlist" in report.summary


def test_add_entries_without_a_probe_marks_the_playlist_unverified():
    from tdt_player import store

    library = store.Library(playlists=[])
    spain = catalogue.resolve(
        catalogue.load_catalogue("country", fetch=fetcher()).entries, "ES"
    )

    report = catalogue.add_entries(library, [spain], probe=None)

    assert len(report.added) == 1
    assert report.unverified == [spain]
    assert "unverified" in report.summary


def test_add_entries_can_refuse_what_it_could_not_verify():
    from tdt_player import store

    library = store.Library(playlists=[])
    spain = catalogue.resolve(
        catalogue.load_catalogue("country", fetch=fetcher()).entries, "ES"
    )

    report = catalogue.add_entries(
        library, [spain], probe=lambda url: None, allow_unknown=False
    )

    assert report.added == []
    assert report.unverified == [spain]
    assert library.playlists == []


def test_add_entries_does_not_duplicate_an_existing_preset():
    from tdt_player import models, store

    preset = models.Playlist(
        url="https://iptv-org.github.io/iptv/countries/es.m3u", name="IPTV-org Spain"
    )
    library = store.Library(playlists=[preset])
    spain = catalogue.resolve(
        catalogue.load_catalogue("country", fetch=fetcher()).entries, "ES"
    )

    report = catalogue.add_entries(library, [spain], probe=lambda url: True)

    assert report.added == []
    assert report.existing == [spain]
    assert len(library.playlists) == 1
