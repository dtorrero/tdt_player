"""Command line interface for tdt_player.

    tdtplayer                          # the window (same as ``tdtplayer gui``)
    tdtplayer sources list             # the configured playlists
    tdtplayer sources add --preset iptv-org-es
    tdtplayer refresh                  # every enabled playlist
    tdtplayer list --search sur
    tdtplayer list --favourites
    tdtplayer favourite "La 1"
    tdtplayer play "La 1"

Every subcommand reads the same ``config.json`` as the GUI, so the list can be
refreshed from a script and the window opened afterwards.  The GUI is imported
lazily, which keeps ``list``/``play`` usable on a machine without a display.
"""

from __future__ import annotations

import argparse
import difflib
import shlex
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, catalogue, refresh, store
from .m3u import DEFAULT_LIST_URL, PlaylistError, load_m3u
from .models import Channel, Playlist
from .player import PlayerError, build_mpv_command, mpv_available, run_blocking
from .search import filter_channels, fold, groups

CONFIG_HELP = (
    "use PATH instead of $TDT_PLAYER_CONFIG / "
    "$XDG_CONFIG_HOME/tdt_player/config.json"
)


def _add_config_option(parser: argparse.ArgumentParser, *, suppressed: bool = False) -> None:
    """Add ``--config``; ``suppressed`` keeps a subparser from resetting it."""
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS if suppressed else None,
        help=CONFIG_HELP,
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="tdtplayer",
        description="Browse an M3U channel list and play the channels in mpv.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_config_option(parser)
    parser.add_argument(
        "--refresh-catalogue",
        action="store_true",
        help="ignore the cached country/language index and download it again",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    gui = subparsers.add_parser("gui", help="open the desktop application (default)")
    _add_config_option(gui, suppressed=True)
    gui.add_argument("--mpv", metavar="PATH", default=None, help="mpv executable to use")

    listing = subparsers.add_parser("list", help="print the stored channels")
    _add_config_option(listing, suppressed=True)
    listing.add_argument("--search", metavar="TEXT", default="", help="only channels matching TEXT")
    listing.add_argument("--group", metavar="NAME", default=None, help="only channels in group NAME")
    listing.add_argument(
        "--playlist", metavar="SELECTOR", default=None,
        help="only channels from this playlist (id, name or URL)",
    )
    listing.add_argument("--favourites", action="store_true", help="only starred channels")
    listing.add_argument("--urls", action="store_true", help="also print the stream URL")
    listing.add_argument("--groups", action="store_true", help="print the groups instead")

    play = subparsers.add_parser("play", help="play a channel in mpv")
    _add_config_option(play, suppressed=True)
    play.add_argument("name", help="channel name; a unique fragment is enough")
    play.add_argument(
        "--source", type=int, default=1, metavar="N",
        help="start with the Nth mirror of the channel (default 1)",
    )
    play.add_argument("--mpv", metavar="PATH", default=None, help="mpv executable to use")
    play.add_argument(
        "--print-command", action="store_true",
        help="print the mpv command instead of running it",
    )

    favourite = subparsers.add_parser("favourite", help="star or unstar a channel")
    _add_config_option(favourite, suppressed=True)
    favourite.add_argument("name", help="channel name; a unique fragment is enough")
    favourite.add_argument("--off", action="store_true", help="remove the star instead")

    importer = subparsers.add_parser(
        "import", help="add a playlist (URL or file) and fetch it"
    )
    _add_config_option(importer, suppressed=True)
    importer.add_argument(
        "location", nargs="?", default=DEFAULT_LIST_URL,
        help=f"playlist to read (default: {DEFAULT_LIST_URL})",
    )
    importer.add_argument("--replace", action="store_true", help="drop the stored channels first")
    importer.add_argument(
        "--once", action="store_true",
        help="merge the playlist without registering it as a source",
    )

    refresher = subparsers.add_parser("refresh", help="re-download every enabled playlist")
    _add_config_option(refresher, suppressed=True)
    refresher.add_argument(
        "--source", action="append", metavar="SELECTOR", default=None,
        help="only this playlist (id, name or URL); repeatable",
    )
    refresher.add_argument(
        "--url", metavar="URL", default=None,
        help="fetch one URL without touching the configured playlists",
    )
    refresher.add_argument("--replace", action="store_true", help="drop the stored channels first")

    sources = subparsers.add_parser("sources", help="manage the playlist sources")
    _add_config_option(sources, suppressed=True)
    source_actions = sources.add_subparsers(dest="sources_action", metavar="ACTION")

    source_list = source_actions.add_parser("list", help="print the configured playlists")
    _add_config_option(source_list, suppressed=True)

    source_add = source_actions.add_parser("add", help="add a playlist")
    _add_config_option(source_add, suppressed=True)
    source_add.add_argument("location", nargs="?", default=None, help="playlist URL or file path")
    source_add.add_argument("--name", metavar="NAME", default=None, help="label for the playlist")
    source_add.add_argument(
        "--preset", choices=sorted(store.preset_playlists()), default=None,
        help="add one of the recommended playlists",
    )
    source_add.add_argument("--disabled", action="store_true", help="add it without refreshing")
    source_add.add_argument("--refresh", action="store_true", help="fetch it immediately")
    source_add.add_argument(
        "--country", action="append", metavar="CODE|NAME", default=None,
        help="add this country's IPTV-org playlist (repeatable)",
    )
    source_add.add_argument(
        "--language", action="append", metavar="CODE|NAME", default=None,
        help="add this language's IPTV-org playlist (repeatable)",
    )
    source_add.add_argument(
        "--no-probe", action="store_true",
        help="do not check that the playlist exists before adding it",
    )

    source_remove = source_actions.add_parser("remove", help="remove a playlist")
    _add_config_option(source_remove, suppressed=True)
    source_remove.add_argument("playlist", help="playlist id, name or URL")
    source_remove.add_argument(
        "--keep-channels", action="store_true",
        help="keep the channels this playlist contributed",
    )

    for action, summary in (
        ("enable", "enable a playlist"),
        ("disable", "disable a playlist"),
    ):
        item = source_actions.add_parser(action, help=summary)
        _add_config_option(item, suppressed=True)
        item.add_argument("playlist", help="playlist id, name or URL")

    catalogue_parser = subparsers.add_parser(
        "catalogue", help="browse the IPTV-org feeds by country or language"
    )
    _add_config_option(catalogue_parser, suppressed=True)
    catalogue_actions = catalogue_parser.add_subparsers(
        dest="catalogue_action", metavar="KIND"
    )
    for action, summary in (
        ("countries", "list the countries that publish a playlist"),
        ("languages", "list the languages that publish a playlist"),
    ):
        item = catalogue_actions.add_parser(action, help=summary)
        _add_config_option(item, suppressed=True)
        item.add_argument("--search", metavar="TEXT", default="", help="only entries matching TEXT")
        if action == "languages":
            item.add_argument(
                "--all", action="store_true",
                help="include the languages spoken in no listed country",
            )

    return parser


def _library(args: argparse.Namespace) -> store.Library:
    """Load the library named by ``--config`` (or the default location)."""
    path = getattr(args, "config", None)
    return store.load(Path(path).expanduser() if path else None)


def _resolve(channels: Sequence[Channel], query: str) -> Optional[Channel]:
    """Find the channel the user means, printing suggestions when ambiguous."""
    wanted = fold(query)
    if not wanted:
        print("tdtplayer: empty channel name", file=sys.stderr)
        return None

    for channel in channels:
        if fold(channel.name) == wanted:
            return channel

    matches = filter_channels(channels, query)
    if len(matches) == 1:
        return matches[0]

    # Prefer channels whose *name* carries every token: asking for "canal local"
    # should not be ambiguous just because another channel sits in a group called
    # "Locales".
    tokens = wanted.split()
    name_matches = [
        channel for channel in matches if all(token in fold(channel.name) for token in tokens)
    ]
    if len(name_matches) == 1:
        return name_matches[0]

    candidates = name_matches or matches
    if candidates:
        print(f"tdtplayer: {len(candidates)} channels match {query!r}:", file=sys.stderr)
        for channel in candidates[:10]:
            print(f"  {channel.label}", file=sys.stderr)
        if len(candidates) > 10:
            print(f"  … and {len(candidates) - 10} more", file=sys.stderr)
        return None

    close = difflib.get_close_matches(query, [c.name for c in channels], n=3, cutoff=0.5)
    if close:
        print(f"tdtplayer: no channel matches {query!r}; did you mean:", file=sys.stderr)
        for name in close:
            print(f"  {name}", file=sys.stderr)
    else:
        print(f"tdtplayer: no channel matches {query!r}", file=sys.stderr)
    return None


def _command_gui(args: argparse.Namespace) -> int:
    """Hand over to :mod:`tdt_player.gui` with the options it understands."""
    from . import gui

    forwarded: List[str] = []
    if getattr(args, "config", None):
        forwarded += ["--config", str(args.config)]
    if getattr(args, "mpv", None):
        forwarded += ["--mpv", str(args.mpv)]
    return gui.main(forwarded)


def _command_list(args: argparse.Namespace) -> int:
    library = _library(args)

    if args.groups:
        for name in groups(library.channels):
            print(name)
        return 0

    playlist = None
    if args.playlist:
        playlist = library.playlist_by_name(args.playlist)
        if playlist is None:
            print(f"tdtplayer: no playlist matches {args.playlist!r}", file=sys.stderr)
            return 1

    rows = filter_channels(
        library.channels,
        args.search,
        args.group,
        playlist=playlist.id if playlist is not None else None,
        favourites_only=args.favourites,
    )
    if not rows:
        if not library.channels:
            print("No channels stored yet; run: tdtplayer refresh", file=sys.stderr)
        else:
            print("No channel matches those filters", file=sys.stderr)
        return 0

    width = max((len(channel.label) for channel in rows), default=0)
    for channel in rows:
        marker = "★ " if channel.favourite else "  "
        line = f"{marker}{channel.label:<{width}}"
        if args.urls:
            line += f"  {channel.url}"
        print(line)
    print(f"{len(rows)} of {len(library.channels)} channels", file=sys.stderr)
    return 0


def _command_favourite(args: argparse.Namespace) -> int:
    library = _library(args)
    if not library.channels:
        print("No channels stored yet; run: tdtplayer refresh", file=sys.stderr)
        return 1

    channel = _resolve(library.channels, args.name)
    if channel is None:
        return 1

    channel.favourite = not args.off
    library.save()
    print(f"{channel.name}: {'starred' if channel.favourite else 'unstarred'}")
    return 0


def _command_play(args: argparse.Namespace) -> int:
    library = _library(args)
    if not library.channels:
        print("No channels stored yet; run: tdtplayer refresh", file=sys.stderr)
        return 1

    channel = _resolve(library.channels, args.name)
    if channel is None:
        return 1

    streams = channel.playable_sources()
    index = max(0, args.source - 1)
    if index >= len(streams):
        print(
            f"tdtplayer: {channel.name} has {len(streams)} stream(s)",
            file=sys.stderr,
        )
        return 1

    mpv = args.mpv or library.setting("mpv_path", "mpv") or "mpv"
    if not mpv_available(mpv):
        print(f"tdtplayer: {mpv!r} was not found; install mpv first", file=sys.stderr)
        return 1

    extra_args = library.setting("mpv_args", []) or []
    user_agent = library.setting("user_agent", "")

    if args.print_command:
        command = build_mpv_command(
            channel,
            streams[index],
            extra_args=extra_args,
            user_agent=user_agent,
            mpv=mpv,
        )
        print(shlex.join(command))
        return 0

    try:
        return run_blocking(
            channel,
            start_index=index,
            extra_args=extra_args,
            user_agent=user_agent,
            mpv=mpv,
        )
    except PlayerError as exc:
        print(f"tdtplayer: {exc}", file=sys.stderr)
        return 1


def _import_once(library: store.Library, location: str, *, replace: bool = False) -> int:
    """Merge ``location`` without registering it as a playlist source."""
    try:
        result = load_m3u(location)
    except PlaylistError as exc:
        print(f"tdtplayer: {exc}", file=sys.stderr)
        return 1

    if not result.channels:
        print(f"tdtplayer: {location} contains no playable channel", file=sys.stderr)
        return 1

    if replace:
        library.channels = []

    added, updated = library.merge(result.channels)
    library.save()

    for warning in result.warnings[:5]:
        print(f"warning: {warning}", file=sys.stderr)
    if len(result.warnings) > 5:
        print(f"warning: … and {len(result.warnings) - 5} more", file=sys.stderr)

    print(
        f"{location}: {result.entries} entries, {len(result.channels)} channels; "
        f"{added} added, {updated} updated, {len(library.channels)} stored in {library.path}"
    )
    return 0


def _report(result: refresh.RefreshResult) -> None:
    """Print one playlist result, failures on stderr."""
    stream = sys.stdout if result.ok else sys.stderr
    print(result.summary, file=stream)
    for warning in result.warnings[:3]:
        print(f"warning: {warning}", file=sys.stderr)


def _command_import(args: argparse.Namespace) -> int:
    library = _library(args)
    if args.once:
        return _import_once(library, args.location, replace=args.replace)

    playlist = library.add_playlist(Playlist(url=args.location))
    library.save()
    result = refresh.refresh_playlist(library, playlist, replace=args.replace)
    _report(result)
    return 0 if result.ok else 1


def _progress(index: int, total: int, playlist: Playlist) -> None:
    print(f"[{index}/{total}] {playlist.label} …", file=sys.stderr)


def _command_refresh(args: argparse.Namespace) -> int:
    library = _library(args)

    if args.url:
        return _import_once(library, args.url, replace=args.replace)

    if not library.playlists:
        print(
            "tdtplayer: no playlists configured; add one with 'tdtplayer sources add URL'",
            file=sys.stderr,
        )
        return 1

    only = None
    if args.source:
        only = []
        for selector in args.source:
            playlist = library.playlist_by_name(selector)
            if playlist is None:
                print(f"tdtplayer: no playlist matches {selector!r}", file=sys.stderr)
                return 1
            only.append(playlist.id)

    results = refresh.refresh_all(
        library, only=only, replace=args.replace, progress=_progress
    )
    if not results:
        print("tdtplayer: nothing to refresh (are all playlists disabled?)", file=sys.stderr)
        return 1

    for result in results:
        _report(result)
    print(refresh.summarise(results), file=sys.stderr)
    return 0 if all(result.ok for result in results) else 1


def _command_sources(args: argparse.Namespace) -> int:
    library = _library(args)
    action = args.sources_action or "list"

    if action == "list":
        return _sources_list(library)
    if action == "add":
        return _sources_add(library, args)
    if action == "remove":
        return _sources_remove(library, args)
    if action in ("enable", "disable"):
        playlist = library.playlist_by_name(args.playlist)
        if playlist is None:
            print(f"tdtplayer: no playlist matches {args.playlist!r}", file=sys.stderr)
            return 1
        library.set_playlist_enabled(playlist.id, action == "enable")
        library.save()
        print(f"{playlist.label}: {'enabled' if playlist.enabled else 'disabled'}")
        return 0

    print("tdtplayer: unknown sources action", file=sys.stderr)
    return 2


def _sources_list(library: store.Library) -> int:
    if not library.playlists:
        print(
            "No playlists configured; add one with: tdtplayer sources add URL",
            file=sys.stderr,
        )
        return 0

    for playlist in library.playlists:
        state = "on " if playlist.enabled else "off"
        details = [f"{playlist.channel_count} channels"]
        if playlist.last_refresh:
            details.append(f"refreshed {playlist.last_refresh}")
        if playlist.last_error:
            details.append(f"error: {playlist.last_error}")
        print(f"[{state}] {playlist.id:10}  {playlist.label}")
        print(f"             {playlist.url}")
        print(f"             {', '.join(details)}")
    return 0


def _add_from_catalogue(
    library: store.Library, args: argparse.Namespace
) -> "tuple[catalogue.AddReport, int]":
    """Resolve ``--country``/``--language`` and add the playlists they name."""
    refresh_cache = bool(getattr(args, "refresh_catalogue", False))
    entries: List[catalogue.CatalogueEntry] = []
    failures = 0

    for kind, selectors in (
        ("country", args.country or []),
        ("language", args.language or []),
    ):
        if not selectors:
            continue
        # Resolving against the full language list means "--language assamese"
        # works even when the picker would hide it behind "show all".
        data = catalogue.load_catalogue(kind, all_languages=True, refresh=refresh_cache)
        if not data.entries:
            print(
                f"tdtplayer: {data.error or f'no {kind} index available'}",
                file=sys.stderr,
            )
            failures += len(selectors)
            continue
        if data.error:
            print(f"warning: {data.error}", file=sys.stderr)
        for selector in selectors:
            entry = catalogue.resolve(data.entries, selector)
            if entry is None:
                print(f"tdtplayer: no {kind} matches {selector!r}", file=sys.stderr)
                failures += 1
            else:
                entries.append(entry)

    prober = None
    if not args.no_probe and entries:
        cached = catalogue.load_probes()
        prober = lambda url: catalogue.probe_with_cache(url, cached=cached)  # noqa: E731

    report = catalogue.add_entries(library, entries, probe=prober)
    for playlist in report.added:
        print(f"added {playlist.label} ({playlist.id}): {playlist.url}")
    for entry in report.existing:
        print(f"already configured: {entry.label}")
    for entry in report.unavailable:
        print(f"no playlist: {entry.label}", file=sys.stderr)
        failures += 1
    for entry in report.unverified:
        print(f"could not verify it exists: {entry.label}", file=sys.stderr)
    if entries:
        print(report.summary, file=sys.stderr)

    return report, failures


def _sources_add(library: store.Library, args: argparse.Namespace) -> int:
    name = args.name or ""
    location = args.location
    status = 0

    if args.preset:
        preset = store.preset_playlists().get(args.preset)
        if preset is None:
            print(f"tdtplayer: unknown preset {args.preset!r}", file=sys.stderr)
            return 1
        preset_name, preset_url = preset
        location = location or preset_url
        name = name or preset_name

    added: List[Playlist] = []
    if location:
        playlist = library.add_playlist(
            Playlist(url=location, name=name, enabled=not args.disabled)
        )
        added.append(playlist)
        print(f"{playlist.label} ({playlist.id}): {playlist.url}")

    selectors = bool(args.country or args.language)
    if selectors:
        report, failures = _add_from_catalogue(library, args)
        added.extend(report.added)
        if args.disabled:
            for playlist in report.added:
                playlist.enabled = False
        if failures:
            status = 1

    if not added and not selectors:
        print(
            "tdtplayer: give a playlist URL/file, --preset, --country or --language",
            file=sys.stderr,
        )
        return 1

    library.save()

    if args.refresh and not args.disabled:
        for playlist in added:
            result = refresh.refresh_playlist(library, playlist)
            _report(result)
            if not result.ok:
                status = 1
    return status


def _sources_remove(library: store.Library, args: argparse.Namespace) -> int:
    playlist = library.playlist_by_name(args.playlist)
    if playlist is None:
        print(f"tdtplayer: no playlist matches {args.playlist!r}", file=sys.stderr)
        return 1

    removed = library.remove_playlist(playlist.id, keep_channels=args.keep_channels)
    library.save()
    if args.keep_channels:
        print(f"{playlist.label}: removed, channels kept")
    else:
        print(f"{playlist.label}: removed ({removed} channels removed)")
    return 0


def _command_catalogue(args: argparse.Namespace) -> int:
    action = getattr(args, "catalogue_action", None)
    if action not in ("countries", "languages"):
        print(
            "tdtplayer: use 'tdtplayer catalogue countries' or "
            "'tdtplayer catalogue languages'",
            file=sys.stderr,
        )
        return 2

    kind = "country" if action == "countries" else "language"
    data = catalogue.load_catalogue(
        kind,
        all_languages=bool(getattr(args, "all", False)),
        refresh=bool(getattr(args, "refresh_catalogue", False)),
    )
    if data.error:
        print(f"warning: {data.error}", file=sys.stderr)
    if not data.entries:
        print(f"tdtplayer: no {kind} index available", file=sys.stderr)
        return 1

    library = _library(args)
    configured = {playlist.url for playlist in library.playlists}
    rows = data.search(args.search)
    for entry in rows:
        mark = "✓" if entry.url in configured else " "
        print(f"{mark} {entry.code:<4} {entry.label}")

    plural = "countries" if kind == "country" else "languages"
    origin = (
        f"cached copy from {data.fetched_label}" if data.from_cache else "downloaded now"
    )
    if kind == "language" and not getattr(args, "all", False):
        print(
            f"{len(rows)} of {len(data.entries)} {plural} spoken in a listed country "
            f"({data.total} in total, --all for every one) · {origin}",
            file=sys.stderr,
        )
    else:
        print(f"{len(rows)} of {len(data.entries)} {plural} · {origin}", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns the process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "gui"

    try:
        if command == "gui":
            return _command_gui(args)
        if command == "list":
            return _command_list(args)
        if command == "favourite":
            return _command_favourite(args)
        if command == "play":
            return _command_play(args)
        if command == "import":
            return _command_import(args)
        if command == "refresh":
            return _command_refresh(args)
        if command == "sources":
            return _command_sources(args)
        if command == "catalogue":
            return _command_catalogue(args)
        parser.print_help()
        return 2
    except KeyboardInterrupt:
        print("\ntdtplayer: interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"tdtplayer: {exc}", file=sys.stderr)
        return 1
