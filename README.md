# tdt_player

A small desktop app to **browse several IPTV playlists, star channels and play
them in `mpv`**.

It reads any number of M3U playlists at once — the Spanish DTT list from
[TDTChannels](https://www.tdtchannels.com/lists/tv.m3u8), the
[IPTV-org Spain list](https://iptv-org.github.io/iptv/countries/es.m3u), a file on
your disk — stores them in one library, shows them in a searchable table, and
gives you favourites and a channel switcher on top.

The application is **standard-library only** (`tkinter` draws the window,
`urllib` downloads the playlists, `socket` drives mpv), so there is nothing to
`pip install` and no dependency to keep up to date. `mpv` is the only external
program it needs.

## Features

* **Several playlists at once**, each one enabled, disabled, refreshed and
  removed on its own. The table has a *Playlist* column and a playlist filter, so
  a mixed library stays readable.
* **One-click presets** for the recommended feeds (TDTChannels M3U8 and its M3U
  backup, IPTV-org Spain) plus any custom URL or local `.m3u`/`.m3u8` file.
* **Populate by country or language** from the IPTV-org catalogue: a picker
  (and a first-run wizard when the library is empty) lists the countries and
  languages that publish a playlist and adds them for you.
* **Search as you type**, accent-insensitively (`andalucia` finds *Canal Sur
  Andalucía*), filtered by group and by playlist.
* **Stars and a switcher**: star channels with `Ctrl`+`D`, open the favourites
  with `Ctrl`+`P`, and zap through them with `Ctrl`+`PgDn` / `Ctrl`+`PgUp`.
* **Plays in `mpv` and checks that it really plays**: the player is driven over
  mpv's IPC socket, so the window never blocks, switching channels reuses the
  same player (no window flash) and mpv's own `file-loaded` / `end-file` events
  confirm a stream started — or report the failure with mpv's message.
* **Mirror fallback**: a station listed several times is one row with several
  sources; a dead one moves on to the next automatically (three tries).
* **Honest error reporting**: a broken playlist, an HTTP error, a missing `mpv`
  or a corrupt config never produce a traceback on screen.
* **Your own channels** can be added, edited and removed by hand, and are never
  touched by a refresh.

## Requirements

| | |
|---|---|
| Python | 3.10 or newer, with `tkinter` (Manjaro/Arch: `sudo pacman -S tk`) |
| mpv | 0.35 or newer (`sudo pacman -S mpv`) — the player |
| yt-dlp | optional; `mpv` uses it for the YouTube-hosted channels in the lists |

## Run it

```bash
git clone <this repository> tdt_player
cd tdt_player
python -m tdt_player            # the window
```

Or install it, which puts `tdtplayer` and `tdtplayer-gui` on your `PATH`:

```bash
pip install --user -e .
tdtplayer
```

On the first run the window is empty: a small **welcome wizard** asks which
countries or languages you want, adds those IPTV-org playlists and downloads
them (TDTChannels is configured already and is fetched too). You can skip it and
do the same later under **Playlists… → Browse catalogue…**, or from the CLI.

## The window

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Search: [ canal sur      ]  Group: [ Locales        ▾ ]  Playlist: [ … ▾ ]  ☐ ★ only │
│ [Play] [Next source] [Stop] │ [★ Favourite] [Switcher…] │ [Add…] [Edit…] [Remove] │
│                                        [Refresh all] [Playlists…]        │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ ★ │ Channel                │ Group        │ Playlist                  │ │
│ │ ★ │ La 1                   │ Generalistas │ TDTChannels               │ │
│ │   │ 3CatInfo (1080p)       │ News, Public │ IPTV-org Spain            │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│ <last message or error>                       332 channels · 332 shown   │
└──────────────────────────────────────────────────────────────────────────┘
```

* **Search**, **Group** and **Playlist** combine (`canal` + `Locales` + one
  playlist), and `★ only` keeps the favourites. Accents and case are ignored;
  IPTV-org's multi-groups (`News;Public`) match either group.
* The **★ column** shows the stars, the **Playlist** column says where each row
  came from (`manual` for channels you added yourself). The selected channel and
  its number of mirrors are mirrored in the window title.
* **Play** (or double-click, or `Enter`) hands the selected channel to mpv.
  **Next source** starts at the second mirror, for a dead first one.
* **Refresh all** downloads every enabled playlist, one after the other, and
  reports what each one did. **Playlists…** manages them.
* **Add… / Edit… / Remove** maintain your own channels. Editing keeps a channel's
  other mirrors; only the primary URL is replaced.
* Right-click a row for *Play*, *Play next source*, *Favourite*, *Copy URL*,
  *Edit…*, *Remove*, *Playlists…* and *Show mpv log…*.

### Keyboard shortcuts

| Keys | Action |
|---|---|
| `Ctrl`+`F` | focus the search box and select its text |
| `Esc` | clear the search |
| `↑` / `↓`, `PgUp` / `PgDn`, `Home` / `End` | move through the table |
| `Enter`, double-click | play the selected channel |
| `Delete` | remove the selected channel (asks first) |
| `Ctrl`+`D` | star / unstar the selected channel |
| `Ctrl`+`P` | open the favourites switcher |
| `Ctrl`+`PgDn` / `Ctrl`+`PgUp` | zap to the next / previous favourite |
| `Ctrl`+`N` | add a channel |
| `F5` | refresh every enabled playlist |

## Playlists

**Playlists…** opens the manager: every source with its on/off state, channel
count, last refresh and last error, plus *Add…*, *Add recommended*,
*Edit…*, *Remove*, *Enable/Disable* and *Refresh selected*.

| Preset | URL |
|---|---|
| TDTChannels | `https://www.tdtchannels.com/lists/tv.m3u8` |
| TDTChannels (M3U backup) | `https://www.tdtchannels.com/lists/tv.m3u` |
| IPTV-org Spain | `https://iptv-org.github.io/iptv/countries/es.m3u` |

Both TDTChannels entries describe the same stations, so adding both does not
duplicate rows — the second one just contributes extra mirrors. IPTV-org uses its
own channel ids (`3CatInfo.es@SD`), so a station it shares with TDTChannels
appears once per playlist; the *Playlist* column and filter make that explicit.

Removing a playlist asks whether its channels should go too; channels that
another playlist (or you) also provide are always kept.

## Populate by country or language

**Playlists… → Browse catalogue…** opens a picker with two tabs:

* **Countries** — the 250 countries IPTV-org knows, with their flag and code.
* **Languages** — the 149 languages spoken in those countries (tick *Show every
  language* for all 7 893 ISO entries). Only 206 languages have stations at all.

Pick one or more (multi-select), press **Add selected**, and one playlist per
entry is created — `IPTV-org (Spain)`, `IPTV-org (Spanish)`, … — with the option
to download them immediately. Entries already configured show `✓ added`.

Not every country or language has a feed (Antarctica does not, for example), so
the app asks the server with a `HEAD` request for the entries you picked; an
entry without a playlist is reported and skipped instead of being added, and the
answer is remembered (`! no playlist`) so it is not asked again. If the network
is down the playlist is still added and marked *unverified*.

The indexes are small (19 KB for countries, 269 KB for languages) and cached for
a week under `$XDG_CACHE_HOME/tdt_player/catalogue/`; *Reload catalogue* (or
`--refresh-catalogue`) bypasses the cache. Nothing depends on IPTV-org at runtime:
once the playlists are added they are ordinary sources that refresh like any
other.

Country and language lists overlap (261 of Spain's 325 stations are also in the
Spanish-language list); they merge by `tvg-id`, so a station appears once with
both streams, not twice.

From the CLI:

```bash
tdtplayer catalogue countries --search spain
tdtplayer catalogue languages --search catalan
tdtplayer catalogue languages --all --search assamese
tdtplayer sources add --country ES --language spa --refresh
tdtplayer sources add --country AQ          # prints "no playlist: Antarctica"
```

## Favourites and the channel switcher

* `Ctrl`+`D` (or the **★ Favourite** button, or the context menu) stars a
  channel. Stars survive refreshes: the channel object is kept, only its streams
  are updated.
* `★ only` restricts the table to the favourites.
* `Ctrl`+`P` opens the **switcher**: type to filter the favourites, `Enter`
  plays, `Esc` closes. With nothing starred yet it lists every channel, so it
  doubles as a quick picker.
* `Ctrl`+`PgDn` / `Ctrl`+`PgUp` zap to the next/previous favourite of the current
  view (or the view's channels when fewer than two favourites are visible).
  Because mpv is driven over IPC, that is a `loadfile … replace` in the running
  player: the window does not close and reopen.

## Command line

The same `config.json` is used, so the GUI and the CLI are interchangeable.

```bash
tdtplayer                       # the window (same as `tdtplayer gui`)
tdtplayer sources list          # the configured playlists and their state
tdtplayer sources add --preset iptv-org-es
tdtplayer sources add --country ES --language spa   # from the IPTV-org catalogue
tdtplayer catalogue countries --search spain        # what is available
tdtplayer catalogue languages --all                 # every ISO language
tdtplayer sources add ~/tv.m3u8 --name "Local" --refresh
tdtplayer sources disable ID    # stop downloading it (channels stay)
tdtplayer sources enable ID
tdtplayer sources remove ID [--keep-channels]
tdtplayer refresh                # every enabled playlist
tdtplayer refresh --source iptv  # just one (id, name or URL fragment)
tdtplayer refresh --replace      # wipe the library first
tdtplayer refresh --url URL      # fetch one URL without registering it
tdtplayer import ~/tv.m3u8       # register the playlist and fetch it
tdtplayer import ~/tv.m3u8 --once  # merge it, register nothing
tdtplayer list                   # every stored channel
tdtplayer list --search sur --group Locales --playlist iptv
tdtplayer list --favourites --urls
tdtplayer list --groups          # just the group names
tdtplayer favourite "la 1"       # star it (--off to unstar)
tdtplayer play "la 1"            # play in mpv, foreground, mpv's output visible
tdtplayer play canal --source 2
tdtplayer play "la 1" --print-command   # show the mpv command, run nothing
tdtplayer --config /tmp/other.json list # use another config file
```

`play` accepts a fragment of the name; when several channels match, the
candidates are listed instead of guessing. Starred channels are marked with `★`
in `list`. `refresh` visits the playlists in order, keeps going when one fails,
and exits non-zero if any did.

## Where the data lives

One JSON file, written atomically (temporary file + `os.replace`, so a crash
cannot leave half a library behind):

```
$XDG_CONFIG_HOME/tdt_player/config.json      # ~/.config/tdt_player/config.json
```

`--config PATH` and `$TDT_PLAYER_CONFIG` override it (which is how the tests and
the examples above stay out of your real config).

```json
{
  "version": 2,
  "settings": {
    "mpv_path": "mpv",
    "mpv_args": [],
    "user_agent": "Mozilla/5.0 …",
    "last_filter": "",
    "last_playlist_filter": "",
    "favourites_only": false,
    "geometry": "1020x660"
  },
  "playlists": [
    {"id": "tdtchannels", "name": "TDTChannels",
     "url": "https://www.tdtchannels.com/lists/tv.m3u8",
     "enabled": true, "last_refresh": "…", "channel_count": 462, "last_error": ""}
  ],
  "channels": [
    {"id": "fce04ab2eeaf", "name": "La 1", "group": "Generalistas",
     "tvg_id": "La1.TV", "logo": "https://…", "favourite": true,
     "sources": [
       {"url": "https://…/la1_main_dvr.m3u8", "referrer": "", "user_agent": "",
        "variant_name": "La 1", "playlist": "tdtchannels"}
     ]}
  ]
}
```

* Every stream remembers the **playlist it came from** (`""` = added by hand).
  That is what lets a refresh update or prune exactly one playlist.
* A channel with several groups (IPTV-org's `News;Public`) stores them in `group`
  separated by `;`; the table shows them as `News, Public`.
* **Upgrading from 0.1**: a `version: 1` file is migrated in place — its
  `settings.list_url` becomes a playlist and no data is lost or moved aside. The
  channels imported back then have no playlist origin yet, so they are never
  pruned; a `tdtplayer refresh --replace` (or a fresh refresh after removing the
  old ones) gives them one.
* An unreadable, non-JSON or foreign-version file is moved to `config.json.bak`
  and the app starts empty instead of refusing to start.
* The country/language indexes and the playlist probes live next to it in
  `$XDG_CACHE_HOME/tdt_player/catalogue/` (`countries.json`, `languages.json`,
  `probes.json`) and are valid for a week; deleting that directory is safe.

## Playback: mpv, IPC and "is it really playing?"

The GUI keeps **one** mpv process alive:

```
mpv --idle=yes --force-window=yes --input-ipc-server=/run/user/1000/tdt_player-….sock
```

and talks to it over that socket: `loadfile <url> replace` to play or switch,
`set_property title/user-agent/referrer` for the per-channel bits, `quit` to
stop. mpv reports back:

| mpv event | what the app does |
|---|---|
| `file-loaded` | status becomes **“Playing …”** — the stream really started |
| `end-file` `reason=error` | counts that source as dead and tries the next mirror |
| `end-file` `reason=eof/quit` | “finished” |
| no `file-loaded` within 10 s | that source counts as dead, next mirror |

mpv's output is appended to `$XDG_CACHE_HOME/tdt_player/mpv.log`; *Show mpv log…*
in the context menu shows its tail. If the socket cannot be created (very old
mpv, odd permissions) the player falls back to spawning one mpv per channel —
the 0.1 behaviour — and says so in the status bar. The CLI keeps that simple
foreground mode on purpose, so mpv's messages appear in your terminal.

### Why `rtvelivestream.akamaized.net` fails

That hostname **does not exist**; it is not something the app does wrong:

```
$ getent hosts rtvelivestream.akamaized.net      # nothing
$ curl https://rtvelivestream.akamaized.net/…    # curl: (6) Could not resolve host
$ getent hosts rtvelivestream.rtve.es            # resolves (Fastly)
$ mpv --no-config --vo=null --ao=null --length=3 \
      https://ztnr.rtve.es/ztnr/1694255.m3u8     # plays, exit 0
```

The TDTChannels list uses `rtvelivestream.rtve.es`, which works. Two related
notes: mpv's `--playlist=` option wants a **file** (`[file]` in `mpv
--list-options`), so for a single stream you just pass the URL; and plenty of
streams in these lists are geo-blocked or simply down, which is why the app
retries mirrors and reports mpv's own error instead of pretending it plays.

## How a refresh merges

* Every `#EXTINF` is parsed: `tvg-id`, `tvg-name`, `tvg-logo`, `group-title`,
  the display name (which may itself contain commas) and any following
  `#EXTVLCOPT`/`#EXTGRP` lines. Headers written *inside* `#EXTINF`
  (`http-referrer=`, `http-user-agent=`, as IPTV-org does) are read too.
  `#KODIPROP` and HLS tags are ignored; entries without a URL are skipped and
  reported.
* Entries sharing a `tvg-id` (or, without one, a name and its groups) become one
  channel with several sources, in playlist order, repeated URLs removed.
* A refresh **tags** every stream with the playlist it came from, **prunes** the
  streams that playlist contributed last time, merges the fresh list, then drops
  channels that are left with nothing. So a station that left the feed goes away,
  while a channel that another playlist (or you) still provides stays — and the
  channel object itself is reused, keeping its id, its star and a name you
  edited.
* Channels you added by hand have no playlist origin and are never pruned.
* One broken playlist does not stop the others: its error lands in
  `last_error`, the rest are still refreshed, and the exit status is non-zero.

## Development

```bash
pip install --user -e '.[dev]'   # pytest only; the app itself needs nothing
python -m pytest -q              # 193 tests, no network, no mpv
```

The suite covers the parser against a deliberately nasty fixture (two `#EXTM3U`
headers, CRLF, BOM, commas in names, headers containing `=`, `#KODIPROP`,
`#EXTGRP`, an entry without a URL, multi-groups), the v1→v2 migration, the
refresh engine (tagging, pruning, shared stations, failures), the IPTV-org
catalogue (cached indexes, offline fallback, probes, the add report), the
accent-insensitive search with its playlist/favourite filters, the exact mpv
command line, the mirror fallback and the IPC player (with a fake socket and
scripted mpv events), the CLI, and the window itself — a real but withdrawn Tk
instance with a fake player, an injected playlist loader and an injected
catalogue. GUI tests skip themselves when there is no display.

```
tdt_player/
├── __init__.py     version
├── __main__.py     python -m tdt_player
├── models.py       Channel / Source / Playlist (one station, many mirrors)
├── m3u.py          playlist parsing, HTTP/file loading (stdlib only)
├── store.py        versioned JSON config (v2), atomic save, merge helpers
├── catalogue.py    IPTV-org countries/languages, cache and playlist probes
├── refresh.py      fetch/tag/prune/merge, one playlist at a time
├── search.py       accent-insensitive, multi-token filtering
├── player.py       mpv command line, IPC player, mirror fallback
├── cli.py          argparse CLI (gui/list/play/favourite/import/refresh/sources/catalogue)
└── gui.py          tkinter window (+ dialogs, switcher and the first-run wizard)
```

## Limitations

* Playback depends on the stream being alive and not geo-blocked; dead mirrors
  are retried, but some channels simply cannot play from every location.
* The country/language catalogue is IPTV-org's: if its JSON shape changes the
  app says so and keeps using its cached copy, and only the entries you pick are
  checked for existence (some countries and most of the 7 893 languages have no
  stations).
* The same station can appear once per playlist when the playlists use different
  channel ids (TDTChannels vs IPTV-org); filter by playlist or hide one of them.
* No EPG yet (the lists advertise one via `url-tvg`), no channel logos in the
  table, no recording, no DRM, no name-based de-duplication across playlists.
