"""Value objects shared by the parser, the store, the player and the GUI.

A single station shows up several times in an M3U playlist: TDTChannels ships one
entry per mirror, so *La 1* appears twice and *+tdp* nine times.  The GUI wants
one row per station while the player wants every URL it may fall back to, so
:class:`Channel` owns a list of :class:`Source` objects instead of a single URL.

A channel can come from several playlists at once (TDTChannels, IPTV-org, a local
file), which is why every :class:`Source` remembers the :class:`Playlist` it was
read from: that is what lets a refresh update or prune exactly one playlist
without touching the others, nor the channels added by hand.

All classes serialise through :meth:`to_dict`/:meth:`from_dict` and accept
incomplete data, so a hand-edited ``config.json`` (or a file written by a newer
version with extra keys) still loads instead of raising.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence, Tuple
from urllib.parse import urlparse


def new_channel_id() -> str:
    """Return a short unique id, used as the GUI's row identifier."""
    return uuid.uuid4().hex[:12]


def new_playlist_id() -> str:
    """Return a short unique id for a playlist source."""
    return uuid.uuid4().hex[:8]


def normalise_groups(value: Any) -> str:
    """Normalise groups into ``"News; Public"`` (deduplicated, ordered).

    IPTV-org packs several genres into one ``group-title`` (``News;Public``)
    while TDTChannels uses one region per entry.  The stored string keeps the
    separators, and :attr:`Channel.groups` splits them again for filtering.
    """
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = str(value or "").split(";")

    ordered: List[str] = []
    seen = set()
    for part in parts:
        cleaned = " ".join(part.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            ordered.append(cleaned)
    return "; ".join(ordered)


def suggest_playlist_name(location: str) -> str:
    """Derive a human readable name from a URL or path."""
    text = (location or "").strip()
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https"):
        host = parsed.netloc or parsed.path
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return f"{host}/{tail}" if tail else host
    if parsed.scheme == "file":
        text = parsed.path
    return os.path.basename(text.rstrip("/")) or text


@dataclass
class Playlist:
    """One playlist source: where channels come from and how the last refresh went."""

    url: str
    name: str = ""
    id: str = field(default_factory=new_playlist_id)
    enabled: bool = True
    last_refresh: str = ""
    channel_count: int = 0
    last_error: str = ""

    def __post_init__(self) -> None:
        self.url = (self.url or "").strip()
        self.name = (self.name or "").strip() or suggest_playlist_name(self.url)
        self.id = (self.id or "").strip() or new_playlist_id()
        self.enabled = bool(self.enabled)
        self.last_refresh = (self.last_refresh or "").strip()
        self.last_error = (self.last_error or "").strip()
        try:
            self.channel_count = int(self.channel_count or 0)
        except (TypeError, ValueError):
            self.channel_count = 0

    @property
    def label(self) -> str:
        """What the GUI and the CLI show for this playlist."""
        return self.name or self.url

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "last_refresh": self.last_refresh,
            "channel_count": self.channel_count,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Playlist":
        """Rebuild a playlist from JSON, tolerating missing or extra keys."""
        if not isinstance(data, Mapping):
            raise TypeError("a playlist must be a JSON object")
        return cls(
            url=str(data.get("url") or "").strip(),
            name=str(data.get("name") or "").strip(),
            id=str(data.get("id") or "").strip(),
            enabled=bool(data.get("enabled", True)),
            last_refresh=str(data.get("last_refresh") or "").strip(),
            channel_count=data.get("channel_count") or 0,
            last_error=str(data.get("last_error") or "").strip(),
        )


@dataclass
class Source:
    """One playable URL plus the HTTP headers that URL needs."""

    url: str
    referrer: str = ""
    user_agent: str = ""
    variant_name: str = ""
    #: Id of the :class:`Playlist` this stream was read from (``""`` = by hand).
    playlist: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "url": self.url,
            "referrer": self.referrer,
            "user_agent": self.user_agent,
            "variant_name": self.variant_name,
            "playlist": self.playlist,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Source":
        """Build a source from a JSON object, or from a bare URL string."""
        if isinstance(data, str):
            return cls(url=data.strip())
        if not isinstance(data, Mapping):
            return cls(url="")
        return cls(
            url=str(data.get("url") or "").strip(),
            referrer=str(data.get("referrer") or "").strip(),
            user_agent=str(data.get("user_agent") or "").strip(),
            variant_name=str(data.get("variant_name") or "").strip(),
            playlist=str(data.get("playlist") or "").strip(),
        )

    @property
    def key(self) -> Tuple[str, str, str, str]:
        """Identity used to drop repeated mirrors (same URL, headers and origin)."""
        return (self.url, self.referrer, self.user_agent, self.playlist)


@dataclass
class Channel:
    """A station: a name, its groups and one or more playable sources."""

    name: str
    sources: List[Source] = field(default_factory=list)
    group: str = ""
    tvg_id: str = ""
    logo: str = ""
    id: str = field(default_factory=new_channel_id)
    #: Starred by the user; kept across refreshes and used by the switcher.
    favourite: bool = False

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip()
        self.group = normalise_groups(self.group)
        self.tvg_id = (self.tvg_id or "").strip()
        self.logo = (self.logo or "").strip()
        self.id = (self.id or "").strip() or new_channel_id()
        self.favourite = bool(self.favourite)

        raw_sources = self.sources or []
        converted = [
            source if isinstance(source, Source) else Source.from_dict(source)
            for source in raw_sources
        ]

        # Empty URLs cannot be played and repeated mirrors only add noise.
        unique: List[Source] = []
        seen = set()
        for source in converted:
            if source.url and source.key not in seen:
                seen.add(source.key)
                unique.append(source)
        self.sources = unique

    @property
    def url(self) -> str:
        """The first playable URL (``""`` when the channel has none)."""
        return self.sources[0].url if self.sources else ""

    @property
    def groups(self) -> List[str]:
        """The individual groups, e.g. ``["News", "Public"]``."""
        return [part.strip() for part in (self.group or "").split(";") if part.strip()]

    @property
    def group_label(self) -> str:
        """Groups as one display string, e.g. ``"News, Public"``."""
        return ", ".join(self.groups)

    @property
    def playlists(self) -> List[str]:
        """Ids of the playlists that contributed a stream, sorted."""
        return sorted({source.playlist for source in self.sources if source.playlist})

    @property
    def merge_key(self) -> Tuple[str, str]:
        """Key a refresh uses to recognise the same station again.

        ``tvg-id`` alone identifies a station when the playlist publishes one -
        playlists move channels between groups, and keying on the group would
        then produce a duplicate row.  Without an id, the folded name and the
        sorted groups have to match.
        """
        identity = self.tvg_id.casefold()
        if identity:
            return (identity, "")
        name = " ".join(self.name.casefold().split())
        return (name, ";".join(sorted(group.casefold() for group in self.groups)))

    @property
    def label(self) -> str:
        """``Name [Group]`` when a group is known, otherwise just the name."""
        return f"{self.name} [{self.group_label}]" if self.groups else self.name

    def add_source(self, source: Source) -> bool:
        """Append ``source`` unless it is empty or already known.

        A stream that is already present *without* an origin is re-attributed to
        the playlist that just supplied it instead of being stored twice, which
        is how a library written by version 1 acquires playlist origins on its
        first refresh.
        """
        if not source.url:
            return False
        for existing in self.sources:
            if existing.key == source.key:
                return False
            if (existing.url, existing.referrer, existing.user_agent) == (
                source.url,
                source.referrer,
                source.user_agent,
            ):
                if source.playlist and not existing.playlist:
                    existing.playlist = source.playlist
                    return True
        self.sources.append(source)
        return True

    def playable_sources(self) -> List[Source]:
        """Sources with one entry per URL (two playlists may share a stream)."""
        unique: List[Source] = []
        seen = set()
        for source in self.sources:
            if source.url and source.url not in seen:
                seen.add(source.url)
                unique.append(source)
        return unique

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "tvg_id": self.tvg_id,
            "logo": self.logo,
            "favourite": self.favourite,
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Channel":
        """Rebuild a channel from JSON, tolerating missing or extra keys."""
        if not isinstance(data, Mapping):
            raise TypeError("a channel must be a JSON object")

        raw_sources = data.get("sources")
        sources: List[Source] = []
        if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes)):
            sources = [Source.from_dict(item) for item in raw_sources]

        if not sources and data.get("url"):
            # Hand-written files and older exports keep a single top-level URL.
            sources = [
                Source.from_dict(
                    {
                        "url": data.get("url"),
                        "referrer": data.get("referrer", ""),
                        "user_agent": data.get("user_agent", ""),
                        "playlist": data.get("playlist", ""),
                    }
                )
            ]

        return cls(
            name=str(data.get("name") or "").strip(),
            sources=sources,
            group=str(data.get("group") or "").strip(),
            tvg_id=str(data.get("tvg_id") or "").strip(),
            logo=str(data.get("logo") or "").strip(),
            id=str(data.get("id") or "").strip(),
            favourite=bool(data.get("favourite", False)),
        )
