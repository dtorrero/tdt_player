"""Discover downloadable playlists by country or language, from IPTV-org.

IPTV-org publishes one M3U per country (``/iptv/countries/es.m3u``) and one per
language (``/iptv/languages/spa.m3u``), generated from two small JSON indexes:
``api/countries.json`` (250 entries with code, name, flag and the languages
spoken there) and ``api/languages.json`` (7 893 ISO 639-3 entries).  This module
turns those indexes into :class:`CatalogueEntry` objects that know their
playlist URL, so the GUI and the CLI can populate the library without anyone
having to find a feed URL by hand.

Only the two small indexes are downloaded (about 290 KB); they are cached under
``$XDG_CACHE_HOME/tdt_player/catalogue/`` for a week.  Whether a given country or
language actually has stations cannot be known from the indexes (only 189 of the
250 countries and 206 of the 7 893 languages do), and downloading IPTV-org's 3 MB
index playlists just to grey rows out is wasteful, so :func:`probe_url` asks the
server for the one playlist the user picked (``HEAD``, milliseconds) and the
answer is remembered in ``probes.json`` so a known-empty entry is not requested
twice.

Nothing here needs a display or the network to be tested: the fetcher and the
prober are injectable.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from .models import Playlist
from .search import fold

#: Where the two indexes and the per-kind playlists live.
API_BASE = "https://iptv-org.github.io/api"
PLAYLIST_BASE = "https://iptv-org.github.io/iptv"
COUNTRIES_URL = f"{API_BASE}/countries.json"
LANGUAGES_URL = f"{API_BASE}/languages.json"

#: Plain and honest agent for the index requests.
USER_AGENT = "tdt_player/0.3 (+https://codeberg.org/sprinter/tdt_player)"

#: Seconds before an index download is abandoned.
REQUEST_TIMEOUT = 30.0

#: Seconds before a playlist probe is abandoned (it only needs the headers).
PROBE_TIMEOUT = 8.0

#: The indexes change rarely, so a week-old copy is still useful.
CACHE_TTL = 7 * 24 * 3600

#: Version of the cache files written under ``$XDG_CACHE_HOME``.
CACHE_VERSION = 1

KINDS = ("country", "language")

#: ``url -> decoded JSON``; injectable so the tests never touch the network.
Fetcher = Callable[[str], Any]
#: ``url -> True/False/None`` (``None`` = could not tell).
Prober = Callable[[str], Optional[bool]]


def cache_directory() -> Path:
    """``$XDG_CACHE_HOME/tdt_player/catalogue`` (created on demand)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tdt_player" / "catalogue"


def cache_path(kind: str) -> Path:
    """File holding the cached index of ``kind``."""
    return cache_directory() / f"{kind}.json"


def probes_path() -> Path:
    """File remembering which playlist URLs answered (or did not)."""
    return cache_directory() / "probes.json"


def _fetch_json(url: str, *, timeout: float = REQUEST_TIMEOUT) -> Any:
    """Download and decode one JSON document (gzip-aware)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").casefold()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(f"{url}: {exc}") from exc

    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"{url}: not valid JSON ({exc})") from exc


@dataclass(frozen=True)
class CatalogueEntry:
    """One country or language that maps to a downloadable playlist."""

    kind: str
    code: str
    name: str
    flag: str = ""

    @property
    def url(self) -> str:
        """The playlist this entry points at."""
        folder = "countries" if self.kind == "country" else "languages"
        return f"{PLAYLIST_BASE}/{folder}/{self.code.casefold()}.m3u"

    @property
    def title(self) -> str:
        """Playlist name shown in the library: ``IPTV-org (Spain)``."""
        return f"IPTV-org ({self.name})"

    @property
    def playlist_id(self) -> str:
        """Stable id, e.g. ``iptv-org-country-es``."""
        return f"iptv-org-{self.kind}-{self.code.casefold()}"

    @property
    def label(self) -> str:
        """``🇪🇸 Spain (ES)`` for lists and logs."""
        prefix = f"{self.flag} " if self.flag else ""
        return f"{prefix}{self.name} ({self.code})"

    def to_playlist(self) -> Playlist:
        """Build the :class:`~tdt_player.models.Playlist` this entry adds."""
        return Playlist(url=self.url, name=self.title, id=self.playlist_id)

    def matches(self, query: str) -> bool:
        """Case/accent-insensitive match on the name or the code."""
        wanted = fold(query)
        return not wanted or wanted in fold(f"{self.name} {self.code}")


@dataclass
class Catalogue:
    """The entries of one kind, plus where they came from."""

    kind: str
    entries: List[CatalogueEntry] = field(default_factory=list)
    #: Entries before the "spoken in some country" filter was applied.
    total: int = 0
    from_cache: bool = False
    fetched_at: float = 0.0
    #: Set when the network failed and an older cache was used instead.
    error: str = ""

    @property
    def fetched_label(self) -> str:
        """Human readable timestamp of the data in use."""
        if not self.fetched_at:
            return "unknown date"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.fetched_at))

    def search(self, query: str = "") -> List[CatalogueEntry]:
        """Entries matching ``query`` (all of them for an empty query)."""
        return [entry for entry in self.entries if entry.matches(query)]

    def codes(self) -> Set[str]:
        """The codes currently exposed."""
        return {entry.code for entry in self.entries}


def _read_cache(kind: str, *, now: float, ttl: float) -> Optional[Dict[str, Any]]:
    """A cache payload young enough to use, or ``None``."""
    payload = _read_any_cache(kind)
    if payload is None:
        return None
    fetched = payload.get("fetched")
    if not isinstance(fetched, (int, float)) or now - fetched > ttl:
        return None
    return payload


def _read_any_cache(kind: str) -> Optional[Dict[str, Any]]:
    """A cache payload regardless of age (used when the network is down)."""
    try:
        payload = json.loads(cache_path(kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    if not isinstance(payload.get("entries"), list):
        return None
    return payload


def _write_cache(kind: str, payload: Dict[str, Any]) -> None:
    """Write the cache atomically; a failure here is not worth reporting."""
    try:
        parent = cache_directory()
        parent.mkdir(parents=True, exist_ok=True)
        target = cache_path(kind)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        pass


def _country_entries(data: Sequence[Any]) -> List[CatalogueEntry]:
    entries: List[CatalogueEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            continue
        entries.append(
            CatalogueEntry(
                kind="country",
                code=code,
                name=name,
                flag=str(item.get("flag") or "").strip(),
            )
        )
    return sorted(entries, key=lambda entry: fold(entry.name))


def _language_entries(data: Sequence[Any]) -> List[CatalogueEntry]:
    entries: List[CatalogueEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if code and name:
            entries.append(CatalogueEntry(kind="language", code=code, name=name))
    return sorted(entries, key=lambda entry: fold(entry.name))


def _payload_from_entries(
    entries: Sequence[CatalogueEntry],
    *,
    now: float,
    subset: Iterable[str] = (),
) -> Dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "fetched": now,
        "entries": [
            {"code": entry.code, "name": entry.name, "flag": entry.flag} for entry in entries
        ],
        "subset": sorted({code for code in subset}),
        "total": len(entries),
    }


def _catalogue_from_payload(
    kind: str,
    payload: Dict[str, Any],
    *,
    all_languages: bool,
) -> Catalogue:
    entries: List[CatalogueEntry] = []
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            continue
        entries.append(
            CatalogueEntry(kind=kind, code=code, name=name, flag=str(item.get("flag") or ""))
        )

    total = payload.get("total")
    total = int(total) if isinstance(total, (int, float)) else len(entries)
    subset = {str(code).strip() for code in payload.get("subset") or []}
    if kind == "language" and not all_languages and subset:
        entries = [entry for entry in entries if entry.code in subset]

    fetched = payload.get("fetched")
    return Catalogue(
        kind=kind,
        entries=entries,
        total=total,
        from_cache=True,
        fetched_at=float(fetched) if isinstance(fetched, (int, float)) else 0.0,
    )


def _fetch_payload(kind: str, *, fetch: Fetcher, now: float) -> Dict[str, Any]:
    """Download the index(es) for ``kind`` and build a cache payload."""
    if kind == "country":
        data = fetch(COUNTRIES_URL)
        if not isinstance(data, list):
            raise RuntimeError("the country index is not a JSON list")
        entries = _country_entries(data)
        if not entries:
            raise RuntimeError("the country index contains no entry")
        return _payload_from_entries(entries, now=now)

    languages = fetch(LANGUAGES_URL)
    if not isinstance(languages, list):
        raise RuntimeError("the language index is not a JSON list")
    entries = _language_entries(languages)
    if not entries:
        raise RuntimeError("the language index contains no entry")

    # The countries index tells which languages are spoken somewhere, which is
    # the meaningful default view of a 7 893-entry list.  It is optional: if it
    # cannot be read, every language is shown instead.
    subset: Set[str] = set()
    try:
        countries = fetch(COUNTRIES_URL)
    except Exception:  # noqa: BLE001 - the language list is still usable
        countries = []
    if isinstance(countries, list):
        known = {entry.code for entry in entries}
        for item in countries:
            if not isinstance(item, dict):
                continue
            for code in item.get("languages") or []:
                code = str(code).strip()
                if code in known:
                    subset.add(code)

    payload = _payload_from_entries(entries, now=now, subset=subset)
    payload["subset"] = sorted(subset)
    return payload


def load_catalogue(
    kind: str,
    *,
    all_languages: bool = False,
    refresh: bool = False,
    fetch: Optional[Fetcher] = None,
    now: Optional[float] = None,
    ttl: float = CACHE_TTL,
) -> Catalogue:
    """Return the country or language entries, from the cache when possible.

    ``all_languages`` only matters for ``kind="language"``: by default the list
    is restricted to the languages spoken in at least one of the 250 countries,
    which is the useful subset of the 7 893 ISO entries.  ``refresh`` bypasses a
    fresh cache; when a download fails an older cache is used and ``error`` is
    set so the caller can say so.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown catalogue kind {kind!r}")

    fetch = fetch or _fetch_json
    moment = time.time() if now is None else now

    if not refresh:
        cached = _read_cache(kind, now=moment, ttl=ttl)
        if cached is not None:
            return _catalogue_from_payload(kind, cached, all_languages=all_languages)

    try:
        payload = _fetch_payload(kind, fetch=fetch, now=moment)
    except Exception as exc:  # noqa: BLE001 - offline must still be usable
        stale = _read_any_cache(kind)
        if stale is None:
            return Catalogue(kind=kind, error=str(exc))
        catalogue = _catalogue_from_payload(kind, stale, all_languages=all_languages)
        catalogue.error = str(exc)
        return catalogue

    _write_cache(kind, payload)
    catalogue = _catalogue_from_payload(kind, payload, all_languages=all_languages)
    catalogue.from_cache = False
    return catalogue


def resolve(entries: Sequence[CatalogueEntry], text: str) -> Optional[CatalogueEntry]:
    """Find the entry named/coded ``text`` (``ES``, ``Spain``, ``spa`` …)."""
    wanted = (text or "").strip().casefold()
    if not wanted:
        return None
    for entry in entries:
        if entry.code.casefold() == wanted:
            return entry
    for entry in entries:
        if entry.name.casefold() == wanted:
            return entry
    matches = [
        entry
        for entry in entries
        if wanted in entry.name.casefold() or wanted in entry.code.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def load_probes(*, now: Optional[float] = None, ttl: float = CACHE_TTL) -> Dict[str, bool]:
    """Return ``{url: has_playlist}`` for the probes young enough to trust."""
    moment = time.time() if now is None else now
    try:
        payload = json.loads(probes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return {}
    if not isinstance(payload.get("probes"), dict):
        return {}

    probes: Dict[str, bool] = {}
    for url, item in payload["probes"].items():
        if not isinstance(item, dict):
            continue
        checked = item.get("at")
        if not isinstance(checked, (int, float)) or moment - checked > ttl:
            continue
        probes[str(url)] = bool(item.get("ok"))
    return probes


def remember_probe(url: str, ok: bool, *, now: Optional[float] = None) -> None:
    """Persist the outcome of one probe (best effort)."""
    moment = time.time() if now is None else now
    payload: Dict[str, Any] = {"version": CACHE_VERSION, "probes": {}}
    try:
        existing = json.loads(probes_path().read_text(encoding="utf-8"))
        if isinstance(existing, dict) and isinstance(existing.get("probes"), dict):
            payload["probes"] = dict(existing["probes"])
    except (OSError, ValueError):
        pass

    payload["probes"][url] = {"ok": bool(ok), "at": moment}
    try:
        probes_path().parent.mkdir(parents=True, exist_ok=True)
        probes_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def probe_url(url: str, *, timeout: float = PROBE_TIMEOUT) -> Optional[bool]:
    """Ask the server whether ``url`` exists (``HEAD``); ``None`` = cannot tell."""
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return 200 <= status < 400


def probe_with_cache(
    url: str,
    *,
    cached: Optional[Dict[str, bool]] = None,
    probe: Optional[Prober] = None,
) -> Optional[bool]:
    """Probe ``url`` unless a recent answer is known, remembering the answer."""
    if cached is not None and url in cached:
        return cached[url]
    verdict = (probe or probe_url)(url)
    if verdict is not None:
        remember_probe(url, verdict)
    return verdict


@dataclass
class AddReport:
    """What :func:`add_entries` did with the entries it was given."""

    added: List[Playlist] = field(default_factory=list)
    existing: List[CatalogueEntry] = field(default_factory=list)
    unavailable: List[CatalogueEntry] = field(default_factory=list)
    unverified: List[CatalogueEntry] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One line, e.g. ``2 added, 1 already configured, 1 without a playlist``."""
        parts: List[str] = []
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.existing:
            parts.append(f"{len(self.existing)} already configured")
        if self.unavailable:
            parts.append(f"{len(self.unavailable)} without a playlist")
        if self.unverified:
            parts.append(f"{len(self.unverified)} unverified")
        return ", ".join(parts) or "nothing to add"


def add_entries(
    library: Any,
    entries: Sequence[CatalogueEntry],
    *,
    probe: Optional[Prober] = None,
    allow_unknown: bool = True,
) -> AddReport:
    """Add one playlist per entry (skipping what is already configured).

    ``probe`` is called once per entry that is not configured yet; a ``False``
    answer means the playlist does not exist and the entry is reported instead of
    added.  ``None`` (network trouble) adds it anyway unless
    ``allow_unknown=False``, so an offline machine can still prepare the library.
    """
    report = AddReport()
    known = {playlist.url for playlist in library.playlists}
    seen: Set[str] = set()

    for entry in entries:
        if entry.url in seen:
            continue
        seen.add(entry.url)

        if entry.url in known:
            report.existing.append(entry)
            continue

        verdict = probe(entry.url) if probe is not None else None
        if verdict is False:
            report.unavailable.append(entry)
            continue
        if verdict is None and not allow_unknown:
            report.unverified.append(entry)
            continue

        playlist = library.add_playlist(entry.to_playlist())
        report.added.append(playlist)
        known.add(playlist.url)
        if verdict is None:
            report.unverified.append(entry)

    return report
