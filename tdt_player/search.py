"""Filtering helpers: accent-insensitive, multi-word search over a channel list.

Typing ``canal sur`` has to find *Canal Sur Andalucía*, and typing ``andalucia``
has to find it too even though the playlist spells it with an accent.  Both sides
of the comparison are therefore folded through :func:`fold`, which strips the
combining marks of an NFKD decomposition and case-folds the rest.

Every whitespace-separated token of the query must appear somewhere in the
channel's name, group, ``tvg-id`` or mirror names, so ``sur andalucia`` narrows
down and ``informativos`` finds everything in that group.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, Iterable, List, Optional

from .models import Channel


def fold(text: str) -> str:
    """Lower-case ``text`` and remove accents, so ``Andalucía`` == ``andalucia``."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.casefold().split())


def channel_haystack(channel: Channel) -> str:
    """Everything about ``channel`` that a search should look at, folded."""
    parts = [channel.name, channel.tvg_id]
    parts.extend(channel.groups)
    parts.extend(source.variant_name for source in channel.sources)
    return fold(" ".join(part for part in parts if part))


def filter_channels(
    channels: Iterable[Channel],
    query: str = "",
    group: Optional[str] = None,
    *,
    playlist: Optional[str] = None,
    favourites_only: bool = False,
) -> List[Channel]:
    """Return the channels matching every active filter.

    ``group`` matches any of a channel's groups (IPTV-org labels a channel with
    several, e.g. ``News; Public``), ``playlist`` is a playlist id that must have
    contributed one of the channel's streams, and ``favourites_only`` keeps the
    starred channels.
    """
    tokens = fold(query).split()
    wanted_group = fold(group or "")
    wanted_playlist = (playlist or "").strip()
    matches: List[Channel] = []

    for channel in channels:
        if favourites_only and not channel.favourite:
            continue
        if wanted_playlist and wanted_playlist not in channel.playlists:
            continue
        if wanted_group and wanted_group not in (fold(item) for item in channel.groups):
            continue
        if tokens:
            haystack = channel_haystack(channel)
            if not all(token in haystack for token in tokens):
                continue
        matches.append(channel)
    return matches


def groups(channels: Iterable[Channel]) -> List[str]:
    """Return the distinct non-empty groups, alphabetically (one spelling each)."""
    spellings: Dict[str, str] = {}
    for channel in channels:
        for group in channel.groups:
            spellings.setdefault(group.casefold(), group)
    return [spellings[key] for key in sorted(spellings)]


def favourites(channels: Iterable[Channel]) -> List[Channel]:
    """Return the starred channels, in their stored order."""
    return [channel for channel in channels if channel.favourite]
