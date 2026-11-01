"""Refresh playlist sources: load, tag, prune and merge, one playlist at a time.

A refresh has to do more than "merge whatever the playlist returned", because the
library is the union of several playlists plus whatever the user added by hand:

* every stream is **tagged** with the id of the playlist it came from, so a later
  refresh (or the removal of that playlist) can touch exactly those streams;
* the playlist's previous streams are **pruned** before the new ones are merged,
  which removes stations that disappeared from the feed and prevents duplicate
  rows when a playlist changes a channel's id or group;
* channels that still have a stream from another playlist, or were added by hand
  (no origin), are never removed;
* one broken playlist must not stop the others, so every failure is captured in
  :class:`RefreshResult` and recorded on the playlist itself.

Both the GUI and the CLI call :func:`refresh_playlist`/:func:`refresh_all`, so
the behaviour they show is the same, and neither needs Tk nor a network to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, List, Optional, Sequence

from . import store
from .m3u import PlaylistError, load_m3u
from .models import Channel, Playlist

#: ``(index, total, playlist)``, called right before a playlist is fetched.
ProgressCallback = Callable[[int, int, Playlist], None]


@dataclass
class RefreshResult:
    """What one playlist refresh did (or why it could not)."""

    playlist: Playlist
    entries: int = 0
    channels: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """``True`` when the playlist was read and merged."""
        return not self.error

    @property
    def summary(self) -> str:
        """One line the status bar or the CLI can print."""
        if self.error:
            return f"{self.playlist.label}: {self.error}"
        return (
            f"{self.playlist.label}: {self.channels} channels "
            f"({self.added} added, {self.updated} updated, {self.removed} removed)"
        )


def tag_sources(channels: Iterable[Channel], playlist_id: str) -> None:
    """Mark every stream of ``channels`` as coming from ``playlist_id``."""
    for channel in channels:
        for source in channel.sources:
            source.playlist = playlist_id


def prune_playlist(channels: Iterable[Channel], playlist_id: str) -> List[Channel]:
    """Drop the streams of ``playlist_id``, keeping the channels themselves.

    The channels are only *emptied*, not removed: :func:`apply_refresh` merges
    the freshly read playlist right afterwards and that merge matches on the
    channel key, which puts the streams back into the same object.  Keeping the
    object is what preserves its id, its star and the name the user edited;
    channels that are still empty after the merge are dropped then.
    """
    pruned: List[Channel] = []
    for channel in channels:
        channel.sources = [
            source for source in channel.sources if source.playlist != playlist_id
        ]
        pruned.append(channel)
    return pruned


def count_channels(library: store.Library, playlist_id: str) -> int:
    """Number of channels that still have a stream from ``playlist_id``."""
    return len(library.channels_of_playlist(playlist_id))


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class FetchedPlaylist:
    """The result of *reading* one playlist, before it touches the library.

    The GUI fetches on its worker thread (network + parsing) and applies the
    result on the Tk thread, so that no widget ever sees a half-merged library.
    """

    playlist: Playlist
    parsed: Any = None
    error: str = ""


def fetch_playlist(
    playlist: Playlist,
    *,
    loader: Callable[[str], object] = load_m3u,
) -> FetchedPlaylist:
    """Download and parse ``playlist``; failures are captured, not raised."""
    try:
        parsed = loader(playlist.url)
    except PlaylistError as exc:
        return FetchedPlaylist(playlist=playlist, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a broken feed must not crash the app
        return FetchedPlaylist(playlist=playlist, error=f"{playlist.url}: {exc}")

    if not list(getattr(parsed, "channels", []) or []):
        return FetchedPlaylist(
            playlist=playlist,
            parsed=parsed,
            error=f"{playlist.url} contains no playable channel",
        )
    return FetchedPlaylist(playlist=playlist, parsed=parsed)


def apply_refresh(
    library: store.Library,
    fetched: FetchedPlaylist,
    *,
    replace: bool = False,
) -> RefreshResult:
    """Tag, prune and merge a fetched playlist into ``library`` (main thread)."""
    playlist = fetched.playlist
    result = RefreshResult(playlist=playlist)

    if fetched.error:
        result.error = fetched.error
        playlist.last_error = fetched.error
        library.save()
        return result

    parsed = fetched.parsed
    channels = list(getattr(parsed, "channels", []) or [])
    result.entries = int(getattr(parsed, "entries", len(channels)) or 0)
    result.channels = len(channels)
    result.warnings = list(getattr(parsed, "warnings", []) or [])

    tag_sources(channels, playlist.id)

    if replace:
        library.channels = []
        for item in library.playlists:
            item.channel_count = 0
    else:
        library.channels = prune_playlist(library.channels, playlist.id)

    result.added, result.updated = library.merge(channels)

    # Channels the merge did not revive are stations that left the playlist.
    before = len(library.channels)
    library.channels = [channel for channel in library.channels if channel.sources]
    result.removed = before - len(library.channels)

    playlist.last_refresh = _timestamp()
    playlist.last_error = ""
    playlist.channel_count = count_channels(library, playlist.id)
    library.save()
    return result


def refresh_playlist(
    library: store.Library,
    playlist: Playlist,
    *,
    loader: Callable[[str], object] = load_m3u,
    replace: bool = False,
) -> RefreshResult:
    """Fetch ``playlist`` and merge it in one call (used by the CLI and tests)."""
    return apply_refresh(
        library,
        fetch_playlist(playlist, loader=loader),
        replace=replace,
    )


def refresh_targets(
    library: store.Library,
    only: Optional[Sequence[str]] = None,
    *,
    include_disabled: bool = False,
) -> List[Playlist]:
    """Return the playlists a refresh should visit, in stored order."""
    if only is not None:
        wanted = {item.strip() for item in only if item and item.strip()}
        return [playlist for playlist in library.playlists if playlist.id in wanted]
    return [
        playlist
        for playlist in library.playlists
        if include_disabled or playlist.enabled
    ]


def refresh_all(
    library: store.Library,
    *,
    loader: Callable[[str], object] = load_m3u,
    only: Optional[Sequence[str]] = None,
    replace: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> List[RefreshResult]:
    """Refresh every selected playlist, one after another, never raising."""
    targets = refresh_targets(library, only)
    if not targets:
        return []

    if replace:
        # Wiping per playlist would erase the previous playlist's work, so the
        # whole library is emptied once, before the first fetch.
        library.channels = []
        for playlist in library.playlists:
            playlist.channel_count = 0

    results: List[RefreshResult] = []
    total = len(targets)
    for index, playlist in enumerate(targets, start=1):
        if progress is not None:
            try:
                progress(index, total, playlist)
            except Exception:  # noqa: BLE001 - a progress reporter must not break a refresh
                pass
        results.append(
            refresh_playlist(library, playlist, loader=loader, replace=False)
        )
    return results


def summarise(results: Sequence[RefreshResult]) -> str:
    """One line covering every result, e.g. for the GUI status bar."""
    if not results:
        return "No playlists to refresh"

    channels = sum(result.channels for result in results if result.ok)
    added = sum(result.added for result in results)
    updated = sum(result.updated for result in results)
    removed = sum(result.removed for result in results)
    failures = [result for result in results if not result.ok]

    text = (
        f"{len(results)} playlist{'s' if len(results) != 1 else ''}: "
        f"{channels} channels, {added} added, {updated} updated, {removed} removed"
    )
    if failures:
        text += f" · {len(failures)} failed"
    return text
