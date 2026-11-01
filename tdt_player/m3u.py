"""Read M3U/M3U8 playlists into :class:`~tdt_player.models.Channel` objects.

Only the standard library is used: :mod:`urllib.request` fetches a remote list
and :mod:`gzip` unwraps it when the server compresses it.  A local path, a
``file://`` URL and a plain ``http(s)://`` URL all go through :func:`load_m3u`.

What the parser has to survive, because real lists (TDTChannels in particular)
contain all of it:

* two ``#EXTM3U`` header lines at the top;
* ``#EXTINF:-1 key="value" ... ,Display name`` where the *display name itself*
  may contain commas (``...,Canal 24, S.A.``) - so the attribute area is walked
  pair by pair and everything after the terminating comma is the name;
* ``#EXTVLCOPT:http-referrer=`` / ``#EXTVLCOPT:http-user-agent=`` between the
  ``#EXTINF`` line and the URL, whose values may themselves contain ``=``;
* ``#KODIPROP:``/``#EXTGRP:`` lines that mpv cannot use but that must not derail
  the parse (``#EXTGRP`` doubles as a fallback group);
* the same station repeated once per mirror - which is folded into a single
  :class:`~tdt_player.models.Channel` carrying several sources.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from .models import Channel, Source, normalise_groups

#: Default playlist: every Spanish DTT channel, free and geo-available.
DEFAULT_LIST_URL = "https://www.tdtchannels.com/lists/tv.m3u8"

#: Plain and honest user agent for the playlist request itself.
REQUEST_USER_AGENT = "tdt_player/0.1 (+https://codeberg.org/sprinter/tdt_player)"

#: Seconds before a stalled playlist download is abandoned.
DEFAULT_TIMEOUT = 30.0

#: URL schemes mpv can be handed; stub entries and garbage are dropped.
PLAYABLE_SCHEMES = frozenset(
    {
        "http",
        "https",
        "rtmp",
        "rtmps",
        "rtsp",
        "rtsps",
        "udp",
        "rtp",
        "mms",
        "mmsh",
        "file",
    }
)

_EXTINF = "#EXTINF:"
_EXTGRP = "#EXTGRP:"
_EXTVLCOPT = "#EXTVLCOPT:"
_DURATION_RE = re.compile(r"\s*(-?\d+(?:\.\d+)?)")
# ``key="quoted value"`` or ``key=bare``; the leading \s* lets pairs be separated
# by any amount of whitespace.  Bare values stop at a comma, which is what ends
# the attribute area in an #EXTINF line.
_ATTRIBUTE_RE = re.compile(r'\s*([0-9A-Za-z_.:+-]+)\s*=\s*(?:"([^"]*)"|([^\s,]+))')


class PlaylistError(RuntimeError):
    """Raised when a playlist cannot be read (missing file, HTTP error, ...)."""


@dataclass
class ParseResult:
    """What :func:`parse_m3u` produced, plus why the rest was dropped."""

    channels: List[Channel] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: Number of ``#EXTINF`` lines seen, mirrors included.
    entries: int = 0

    @property
    def sources(self) -> int:
        """Total number of stream URLs kept."""
        return sum(len(channel.sources) for channel in self.channels)


@dataclass
class _Pending:
    """The ``#EXTINF`` block being read, awaiting its URL line."""

    name: str
    attrs: Dict[str, str]
    extgrp: str = ""
    referrer: str = ""
    user_agent: str = ""


def is_playable_url(url: str) -> bool:
    """Return ``True`` when ``url`` uses a scheme mpv understands."""
    return urlparse(url).scheme.casefold() in PLAYABLE_SCHEMES


def decode_text(raw: bytes) -> str:
    """Decode playlist bytes, unwrapping gzip and tolerating Latin-1 leftovers."""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        return raw.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes any byte
        return raw.decode("utf-8", errors="replace")


def _iter_lines(text: str) -> Iterator[str]:
    """Yield lines with the BOM removed and CRLF/CR normalised."""
    if text.startswith("\ufeff"):
        text = text[1:]
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        yield line


def _parse_extinf(line: str) -> Tuple[Dict[str, str], str]:
    """Split ``#EXTINF:`` into its attributes and its display name.

    The attribute area is consumed token by token, so the comma that separates it
    from the name is the *only* comma that matters - names may contain more.
    """
    body = line[len(_EXTINF) :]
    attrs: Dict[str, str] = {}

    duration = _DURATION_RE.match(body)
    index = duration.end() if duration else 0

    while True:
        match = _ATTRIBUTE_RE.match(body, index)
        if not match:
            break
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[match.group(1).lower()] = value
        index = match.end()

    rest = body[index:]
    comma = rest.find(",")
    name = rest[comma + 1 :].strip() if comma != -1 else rest.strip()
    return attrs, name


def _collect_option(pending: _Pending, line: str) -> None:
    """Fold one directive line into the channel being read."""
    if line.startswith(_EXTGRP):
        if not pending.extgrp:
            pending.extgrp = line[len(_EXTGRP) :].strip()
        return

    if line.startswith(_EXTVLCOPT):
        option = line[len(_EXTVLCOPT) :]
        # ``partition`` splits on the *first* ``=``: referrer URLs contain more.
        key, _, value = option.partition("=")
        key = key.strip().casefold()
        value = value.strip()
        if not value:
            return
        if key in ("http-referrer", "http-referer", "referrer", "referer"):
            if not pending.referrer:
                pending.referrer = value
        elif key in ("http-user-agent", "user-agent"):
            if not pending.user_agent:
                pending.user_agent = value
        return

    # #KODIPROP (DRM), #EXTHTTP, #EXT-X-*: nothing mpv can take from here.


def _first(attrs: Dict[str, str], *keys: str) -> str:
    """Return the first non-empty attribute among ``keys``."""
    for key in keys:
        value = (attrs.get(key) or "").strip()
        if value:
            return value
    return ""


def _entry_from(pending: _Pending, url: str) -> Optional[Tuple[Channel, Source]]:
    """Turn a finished ``#EXTINF`` block into a channel/source pair."""
    if not is_playable_url(url):
        return None

    attrs = pending.attrs
    name = (attrs.get("tvg-name") or pending.name or "").strip()
    if not name:
        name = url

    # IPTV-org writes the headers into #EXTINF itself, TDTChannels uses
    # #EXTVLCOPT; accept both, with the #EXTVLCOPT value winning when present.
    group = normalise_groups(attrs.get("group-title") or pending.extgrp or "")
    referrer = pending.referrer or _first(attrs, "http-referrer", "http-referer", "referrer", "referer")
    user_agent = pending.user_agent or _first(attrs, "http-user-agent", "user-agent")

    channel = Channel(
        name=name,
        group=group,
        tvg_id=(attrs.get("tvg-id") or "").strip(),
        logo=(attrs.get("tvg-logo") or "").strip(),
    )
    source = Source(
        url=url,
        referrer=referrer,
        user_agent=user_agent,
        variant_name=pending.name.strip(),
    )
    return channel, source


def parse_m3u(text: str) -> ParseResult:
    """Parse playlist text into channels, grouping repeated stations together."""
    result = ParseResult()
    index: Dict[Tuple[str, str], Channel] = {}
    pending: Optional[_Pending] = None

    def drop_pending() -> None:
        nonlocal pending
        if pending is not None:
            result.warnings.append(f"'{pending.name or '?'}' has no stream URL")
            pending = None

    for raw_line in _iter_lines(text):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith(_EXTINF):
            drop_pending()
            attrs, name = _parse_extinf(line)
            pending = _Pending(name=name, attrs=attrs)
            result.entries += 1
            continue

        if line.startswith("#"):
            if pending is not None:
                _collect_option(pending, line)
            continue

        if pending is None:
            continue

        entry = _entry_from(pending, line)
        pending = None
        if entry is None:
            result.warnings.append(f"skipped entry with an unplayable URL: {line[:100]}")
            continue

        channel, source = entry
        grouped = index.get(channel.merge_key)
        if grouped is None:
            grouped = channel
            grouped.add_source(source)
            index[channel.merge_key] = grouped
            result.channels.append(grouped)
        else:
            grouped.add_source(source)
            if not grouped.logo and channel.logo:
                grouped.logo = channel.logo

    drop_pending()
    return result


def fetch_text(
    location: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = REQUEST_USER_AGENT,
) -> str:
    """Return the playlist at ``location`` (URL, ``file://`` URI or path)."""
    parsed = urlparse(location)

    if parsed.scheme in ("http", "https"):
        request = urllib.request.Request(
            location,
            headers={
                "User-Agent": user_agent,
                "Accept": (
                    "application/x-mpegURL, application/vnd.apple.mpegurl, "
                    "audio/mpegurl, text/plain, */*"
                ),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").casefold()
        except urllib.error.HTTPError as exc:
            raise PlaylistError(f"{location}: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise PlaylistError(f"{location}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise PlaylistError(f"{location}: {exc}") from exc
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        return decode_text(raw)

    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
    elif parsed.scheme and "://" in location:
        raise PlaylistError(f"{location}: unsupported URL scheme {parsed.scheme!r}")
    else:
        path = Path(location).expanduser()

    try:
        return decode_text(path.read_bytes())
    except OSError as exc:
        raise PlaylistError(f"{path}: {exc.strerror or exc}") from exc


def load_m3u(location: str, **kwargs: object) -> ParseResult:
    """Fetch ``location`` and parse it; ``kwargs`` go to :func:`fetch_text`."""
    return parse_m3u(fetch_text(location, **kwargs))
