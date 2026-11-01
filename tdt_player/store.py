"""Where the channel library lives and how it is written back.

``$XDG_CONFIG_HOME/tdt_player/config.json`` (``$TDT_PLAYER_CONFIG`` overrides it,
which is what the tests and the ``--config`` flag use) holds the user's settings
and the channel list, stamped with a version.  Saving writes a temporary file in
the same directory and then :func:`os.replace`\\ s it into place, so a crash at
the wrong moment cannot leave a truncated library behind; loading ignores a file
it cannot understand (keeping it as ``<name>.bak``) instead of refusing to start.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .m3u import DEFAULT_LIST_URL
from .models import Channel, Playlist, new_channel_id, new_playlist_id
from .player import DEFAULT_STREAM_USER_AGENT

#: Version of the file below; version 1 is migrated, anything else is ignored.
CONFIG_VERSION = 2

#: Version 1 kept a single ``settings.list_url`` instead of playlist objects.
LEGACY_VERSION = 1

APP_DIRECTORY = "tdt_player"
CONFIG_NAME = "config.json"

#: Playlists offered one click away in the GUI and by ``sources add --preset``.
PRESET_PLAYLISTS = (
    ("tdt", "TDTChannels", DEFAULT_LIST_URL),
    ("tdt-m3u", "TDTChannels (M3U backup)", "https://www.tdtchannels.com/lists/tv.m3u"),
    ("iptv-org-es", "IPTV-org Spain", "https://iptv-org.github.io/iptv/countries/es.m3u"),
)


def config_path() -> Path:
    """Return the config file to use, honouring ``$TDT_PLAYER_CONFIG``."""
    override = os.environ.get("TDT_PLAYER_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIRECTORY / CONFIG_NAME


def default_settings() -> Dict[str, Any]:
    """Fresh settings: mpv, the search state, and nothing playlist related."""
    return {
        "mpv_path": "mpv",
        "mpv_args": [],
        "user_agent": DEFAULT_STREAM_USER_AGENT,
        "last_filter": "",
        "last_playlist_filter": "",
        "favourites_only": False,
        #: welcome-wizard state so an empty library only asks once
        "welcome_done": False,
        "geometry": "",
    }


def preset_playlists() -> Dict[str, Tuple[str, str]]:
    """Return ``{key: (name, url)}`` for the recommended playlists."""
    return {key: (name, url) for key, name, url in PRESET_PLAYLISTS}


def default_playlists() -> List[Playlist]:
    """The playlist a fresh install starts with."""
    return [Playlist(id="tdtchannels", name="TDTChannels", url=DEFAULT_LIST_URL)]


@dataclass
class Library:
    """The stored channels, their playlists and the settings needed to play them."""

    channels: List[Channel] = field(default_factory=list)
    playlists: List[Playlist] = field(default_factory=default_playlists)
    settings: Dict[str, Any] = field(default_factory=default_settings)
    path: Path = field(default_factory=config_path)
    #: Non-fatal problems met while loading (the GUI shows them in its status bar).
    warnings: List[str] = field(default_factory=list)

    def setting(self, key: str, default: Any = None) -> Any:
        """Return one setting, falling back to ``default``."""
        value = self.settings.get(key, default)
        return default if value is None else value

    def find(self, channel_id: str) -> Optional[Channel]:
        """Return the channel with ``channel_id``, if it is still stored."""
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None

    def add(self, channel: Channel) -> Channel:
        """Append ``channel`` and make sure its id is unique."""
        taken = {existing.id for existing in self.channels}
        if channel.id in taken:
            channel.id = new_channel_id()
        self.channels.append(channel)
        return channel

    def remove(self, channel_id: str) -> bool:
        """Drop the channel with ``channel_id``; ``True`` when something went."""
        remaining = [channel for channel in self.channels if channel.id != channel_id]
        if len(remaining) == len(self.channels):
            return False
        self.channels = remaining
        return True

    def merge(self, incoming: Iterable[Channel]) -> Tuple[int, int]:
        """Merge ``incoming`` into the library; returns ``(added, updated)``."""
        self.channels, added, updated = merge_channels(self.channels, incoming)
        return added, updated

    # -- playlists ----------------------------------------------------------
    def playlist(self, playlist_id: str) -> Optional[Playlist]:
        """Return the playlist with ``playlist_id``, if it is stored."""
        for playlist in self.playlists:
            if playlist.id == playlist_id:
                return playlist
        return None

    def playlist_by_name(self, text: str) -> Optional[Playlist]:
        """Find a playlist by id, name or URL (case-insensitive)."""
        wanted = (text or "").strip().casefold()
        if not wanted:
            return None
        for playlist in self.playlists:
            if wanted in (playlist.id.casefold(), playlist.name.casefold(), playlist.url.casefold()):
                return playlist
        for playlist in self.playlists:
            if wanted in playlist.name.casefold() or wanted in playlist.url.casefold():
                return playlist
        return None

    def add_playlist(self, playlist: Playlist, *, replace: bool = False) -> Playlist:
        """Store ``playlist``, refusing duplicates of the same URL.

        With ``replace`` an existing entry for the same URL is updated in place
        (keeping its id, so the channels already tagged with it stay linked).
        """
        existing = next(
            (item for item in self.playlists if item.url and item.url == playlist.url),
            None,
        )
        if existing is not None and not replace:
            return existing
        if existing is not None:
            existing.name = playlist.name or existing.name
            existing.enabled = playlist.enabled
            return existing

        taken = {item.id for item in self.playlists}
        if playlist.id in taken:
            playlist.id = new_playlist_id()
        self.playlists.append(playlist)
        return playlist

    def remove_playlist(self, playlist_id: str, *, keep_channels: bool = False) -> int:
        """Drop a playlist; returns how many channels were removed with it.

        Channels are only deleted when they have no stream left that came from a
        different playlist (or from the user's hand).
        """
        playlist = self.playlist(playlist_id)
        if playlist is None:
            return 0
        self.playlists = [item for item in self.playlists if item.id != playlist_id]
        if keep_channels:
            return 0

        kept: List[Channel] = []
        removed = 0
        for channel in self.channels:
            channel.sources = [
                source
                for source in channel.sources
                if source.playlist != playlist_id
            ]
            if channel.sources:
                kept.append(channel)
            else:
                removed += 1
        self.channels = kept
        return removed

    def set_playlist_enabled(self, playlist_id: str, enabled: bool) -> bool:
        """Enable or disable a playlist; ``False`` when it does not exist."""
        playlist = self.playlist(playlist_id)
        if playlist is None:
            return False
        playlist.enabled = bool(enabled)
        return True

    def channels_of_playlist(self, playlist_id: str) -> List[Channel]:
        """Every channel that has at least one stream from ``playlist_id``."""
        return [channel for channel in self.channels if playlist_id in channel.playlists]

    def save(self, path: Optional[os.PathLike] = None) -> Path:
        """Write the library atomically and return the file it went to."""
        target = Path(path) if path is not None else self.path
        target.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "version": CONFIG_VERSION,
            "settings": self.settings,
            "playlists": [playlist.to_dict() for playlist in self.playlists],
            "channels": [channel.to_dict() for channel in self.channels],
        }

        descriptor, temporary = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

        self.path = target
        return target


def load(path: Optional[os.PathLike] = None) -> Library:
    """Load the library, returning an empty one when there is nothing usable."""
    target = Path(path) if path is not None else config_path()
    library = Library(path=target)

    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return library
    except OSError as exc:
        library.warnings.append(f"cannot read {target}: {exc.strerror or exc}")
        return library

    try:
        payload = json.loads(raw)
    except ValueError:
        library.warnings.append(f"{target} is not valid JSON; starting empty")
        _backup(target)
        return library

    if not isinstance(payload, Mapping):
        library.warnings.append(f"{target} does not contain a JSON object; starting empty")
        _backup(target)
        return library

    version = payload.get("version")
    if version not in (LEGACY_VERSION, CONFIG_VERSION):
        library.warnings.append(
            f"{target} was written by another version ({version!r}); starting empty"
        )
        _backup(target)
        return library

    settings = default_settings()
    stored_settings = payload.get("settings")
    legacy_url = ""
    if isinstance(stored_settings, Mapping):
        for key, value in stored_settings.items():
            if not isinstance(key, str):
                continue
            if key == "list_url":
                # Version 1 stored the playlist here; it becomes a playlist below.
                legacy_url = str(value or "").strip()
                continue
            settings[key] = value
    library.settings = settings

    playlists: List[Playlist] = []
    stored_playlists = payload.get("playlists")
    if isinstance(stored_playlists, Sequence) and not isinstance(stored_playlists, (str, bytes)):
        for entry in stored_playlists:
            if not isinstance(entry, Mapping) or not entry.get("url"):
                continue
            try:
                playlists.append(Playlist.from_dict(entry))
            except (TypeError, ValueError):
                continue

    if version == LEGACY_VERSION:
        url = legacy_url or DEFAULT_LIST_URL
        name = "TDTChannels" if url == DEFAULT_LIST_URL else ""
        library.playlists = [Playlist(id="tdtchannels", name=name, url=url)]
        library.warnings.append(
            f"{target} was upgraded from version 1: channels imported before keep "
            "no playlist origin, so run a refresh with 'Replace' once to track them"
        )
    else:
        library.playlists = playlists

    channels: List[Channel] = []
    stored_channels = payload.get("channels")
    if isinstance(stored_channels, Sequence) and not isinstance(stored_channels, (str, bytes)):
        for entry in stored_channels:
            if not isinstance(entry, Mapping) or not entry.get("name"):
                continue
            try:
                channel = Channel.from_dict(entry)
            except (TypeError, ValueError):
                continue
            if channel.sources:  # a channel nothing can play is not usable
                channels.append(channel)
    library.channels = channels
    return library


def merge_channels(
    existing: Iterable[Channel], incoming: Iterable[Channel]
) -> Tuple[List[Channel], int, int]:
    """Merge ``incoming`` into ``existing``; returns ``(channels, added, updated)``.

    Channels are matched on :attr:`~tdt_player.models.Channel.merge_key`, so a
    refresh updates the URLs of a station it already knows instead of appending a
    second copy.  Channels the user added by hand have no counterpart in the
    playlist and are therefore never removed; a renamed channel keeps its name,
    only its sources (and a missing logo) are refreshed.
    """
    merged: List[Channel] = list(existing)
    index: Dict[Tuple[str, str], Channel] = {}
    for channel in merged:
        index.setdefault(channel.merge_key, channel)

    added = 0
    updated = 0
    for channel in incoming:
        current = index.get(channel.merge_key)
        if current is None:
            merged.append(channel)
            index[channel.merge_key] = channel
            added += 1
            continue

        changed = False
        for source in channel.sources:
            changed = current.add_source(source) or changed
        if channel.logo and not current.logo:
            current.logo = channel.logo
            changed = True
        if changed:
            updated += 1

    return merged, added, updated


def _backup(path: Path) -> Optional[Path]:
    """Keep a copy of an unreadable file as ``<name>.bak`` (best effort)."""
    target = path.with_name(path.name + ".bak")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target
