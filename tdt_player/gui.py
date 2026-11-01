"""A tkinter front-end for tdt_player.

One window, one workflow: the filters sit on top, the channel table fills the
middle and a status bar reports what is happening at the bottom.  Everything is
stored in the same ``config.json`` the CLI uses, so the list can be refreshed
from a script and opened here afterwards.

The list can come from **several playlists at once** (``Playlists…``) and from
channels added by hand; the *Playlist* column says where each row came from and
the playlist filter narrows the table to one feed.  A refresh downloads on a
worker thread and merges on the Tk thread, one playlist at a time, so a broken
feed cannot take the window (or the other playlists) down with it.

Channels can be **starred**; the favourites are a view of their own, ``Ctrl+P``
opens them as a switcher and ``Ctrl+PgDn``/``Ctrl+PgUp`` zap through them.  With
mpv's IPC socket a zap is a ``loadfile … replace`` on the running player, so the
window does not close and reopen, and mpv's own ``file-loaded``/``end-file``
events tell us whether the stream really started.

``tkinter`` is part of the standard library, so the window costs no dependency;
``mpv`` is the only external program involved.
"""

from __future__ import annotations

import argparse
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from . import __version__, catalogue, refresh, store
from .m3u import is_playable_url, load_m3u
from .models import Channel, Playlist, Source
from .player import Player, mpv_available
from .search import filter_channels
from .search import groups as channel_groups

TITLE = "tdt_player"

#: How often the event loop drains the worker/player queues.
POLL_MS = 120

#: Typing pause before the table is filtered (long lists make filtering visible).
SEARCH_DEBOUNCE_MS = 150

#: Combobox entries that disable the corresponding filter.
ALL_GROUPS = "All groups"
ALL_PLAYLISTS = "All playlists"

#: ttk style used to paint the status bar red.
ERROR_STYLE = "Error.TLabel"

#: Shown in the playlist column for channels the user added by hand.
MANUAL_LABEL = "manual"


class ChannelDialog(tk.Toplevel):
    """Add/edit form; :attr:`result` holds the channel once *Save* is pressed."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        channel: Optional[Channel] = None,
    ) -> None:
        super().__init__(parent)
        self.result: Optional[Channel] = None
        self._channel = channel

        self.title(title)
        self.transient(parent)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        source = channel.sources[0] if channel and channel.sources else None
        self.name_var = tk.StringVar(value=channel.name if channel else "")
        self.group_var = tk.StringVar(value=channel.group if channel else "")
        self.url_var = tk.StringVar(value=channel.url if channel else _clipboard_url(self))
        self.referrer_var = tk.StringVar(value=source.referrer if source else "")
        self.user_agent_var = tk.StringVar(value=source.user_agent if source else "")

        self.name_entry = self._row(body, 0, "Name:", self.name_var)
        self._row(body, 1, "Group:", self.group_var)
        self.url_entry = self._row(body, 2, "Stream URL:", self.url_var)
        self._row(body, 3, "Referrer:", self.referrer_var)
        self._row(body, 4, "User agent:", self.user_agent_var)

        ttk.Label(
            body,
            text=(
                "Only the primary stream URL is edited; the channel's other "
                "mirrors are kept."
            ),
            foreground="#808080",
            wraplength=380,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover - the window is not viewable yet
            pass
        (self.url_entry if self.url_var.get() else self.name_entry).focus_set()

    def _row(self, parent: tk.Misc, row: int, label: str, variable: tk.StringVar) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        entry = ttk.Entry(parent, textvariable=variable, width=48)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        return entry

    def _save(self) -> None:
        name = self.name_var.get().strip()
        url = self.url_var.get().strip()
        if not name:
            messagebox.showerror(TITLE, "The channel needs a name.", parent=self)
            return
        if not is_playable_url(url):
            messagebox.showerror(
                TITLE,
                "The stream URL has to start with http://, https://, rtmp://, "
                "rtsp:// or udp://.",
                parent=self,
            )
            return

        group = self.group_var.get().strip()
        referrer = self.referrer_var.get().strip()
        user_agent = self.user_agent_var.get().strip()
        primary = Source(url=url, referrer=referrer, user_agent=user_agent, variant_name=name)

        channel = self._channel
        if channel is None:
            channel = Channel(name=name, group=group, sources=[primary])
        else:
            channel.name = name
            channel.group = group
            if channel.sources and channel.sources[0].url == url:
                first = channel.sources[0]
                first.referrer = referrer
                first.user_agent = user_agent
                first.variant_name = first.variant_name or name
            else:
                others = [source for source in channel.sources if source.url != url]
                channel.sources = [primary] + others

        self.result = channel
        self.destroy()


def _clipboard_url(parent: tk.Misc) -> str:
    """Return the clipboard's first line when it looks like a stream URL."""
    try:
        text = parent.clipboard_get()
    except tk.TclError:
        return ""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    first = stripped.splitlines()[0].strip()
    return first if is_playable_url(first) else ""


def ask_channel(
    parent: tk.Misc,
    *,
    title: str,
    channel: Optional[Channel] = None,
) -> Optional[Channel]:
    """Show the add/edit dialog modally and return the saved channel."""
    dialog = ChannelDialog(parent, title=title, channel=channel)
    parent.wait_window(dialog)
    return dialog.result


def _looks_like_location(text: str) -> bool:
    """``True`` for an http(s)/file URL or an existing file."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if cleaned.casefold().startswith(("http://", "https://", "file:")):
        return True
    return Path(cleaned).expanduser().is_file()


class PlaylistDialog(tk.Toplevel):
    """Add/edit form for one playlist source."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        playlist: Optional[Playlist] = None,
    ) -> None:
        super().__init__(parent)
        self.result: Optional[Playlist] = None
        self._playlist = playlist

        self.title(title)
        self.transient(parent)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        self.name_var = tk.StringVar(value=playlist.name if playlist else "")
        self.url_var = tk.StringVar(value=playlist.url if playlist else "")
        self.enabled_var = tk.BooleanVar(value=playlist.enabled if playlist else True)

        ttk.Label(body, text="Name:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(body, textvariable=self.name_var, width=48).grid(
            row=0, column=1, columnspan=2, sticky="ew", pady=3
        )

        ttk.Label(body, text="URL or file:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.url_entry = ttk.Entry(body, textvariable=self.url_var, width=48)
        self.url_entry.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Button(body, text="Browse…", command=self._browse).grid(
            row=1, column=2, padx=(6, 0), pady=3
        )

        ttk.Checkbutton(
            body,
            text="Enabled (download it when refreshing)",
            variable=self.enabled_var,
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(
            body,
            text="Any http(s):// URL, or a local .m3u/.m3u8 file.",
            foreground="#808080",
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass
        self.url_entry.focus_set()

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose a playlist",
            filetypes=[("M3U playlist", "*.m3u *.m3u8"), ("All files", "*")],
        )
        if path:
            self.url_var.set(path)

    def _save(self) -> None:
        location = self.url_var.get().strip()
        if not _looks_like_location(location):
            messagebox.showerror(
                TITLE,
                "Use an http(s):// URL or the path of an existing playlist file.",
                parent=self,
            )
            return

        name = self.name_var.get().strip()
        playlist = self._playlist
        if playlist is None:
            playlist = Playlist(url=location, name=name, enabled=self.enabled_var.get())
        else:
            playlist.url = location
            playlist.name = name or playlist.name
            playlist.enabled = self.enabled_var.get()

        self.result = playlist
        self.destroy()


def ask_playlist(
    parent: tk.Misc,
    *,
    title: str,
    playlist: Optional[Playlist] = None,
) -> Optional[Playlist]:
    """Show the playlist dialog modally and return the saved playlist."""
    dialog = PlaylistDialog(parent, title=title, playlist=playlist)
    parent.wait_window(dialog)
    return dialog.result


class PlaylistsDialog(tk.Toplevel):
    """Manage the playlist sources: add, edit, remove, enable and refresh."""

    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.parent_app = parent
        self.library = parent.library

        self.title("Playlists")
        self.transient(parent)
        self.geometry("800x380")

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        columns = ("on", "name", "channels", "refreshed", "error")
        self.tree = ttk.Treeview(
            body, columns=columns, show="headings", selectmode="browse"
        )
        for column, text, width, stretch, anchor in (
            ("on", "On", 40, False, "center"),
            ("name", "Playlist", 190, True, "w"),
            ("channels", "Channels", 80, False, "center"),
            ("refreshed", "Last refresh", 150, False, "w"),
            ("error", "Last error", 280, True, "w"),
        ):
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, stretch=stretch, anchor=anchor)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda _event: self._edit())
        self.tree.bind("<Delete>", lambda _event: self._remove())

        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        ttk.Button(buttons, text="Add…", command=self._add).pack(side="left")
        self.preset_button = ttk.Menubutton(buttons, text="Add recommended")
        menu = tk.Menu(self.preset_button, tearoff=False)
        for key, (name, _url) in store.preset_playlists().items():
            menu.add_command(label=name, command=lambda k=key: self._add_preset(k))
        self.preset_button["menu"] = menu
        self.preset_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Browse catalogue…", command=self._browse_catalogue).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="Edit…", command=self._edit).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Remove", command=self._remove).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Enable/Disable", command=self._toggle).pack(
            side="left", padx=(6, 0)
        )

        ttk.Button(buttons, text="Refresh selected", command=self._refresh_selected).pack(
            side="right"
        )
        ttk.Button(buttons, text="Close", command=self.destroy).pack(
            side="right", padx=(0, 6)
        )

        self._reload()
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass

    def _reload(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for playlist in self.library.playlists:
            self.tree.insert(
                "",
                "end",
                iid=playlist.id,
                values=(
                    "✓" if playlist.enabled else "",
                    playlist.label,
                    playlist.channel_count,
                    playlist.last_refresh or "never",
                    playlist.last_error,
                ),
            )

    def _selected(self) -> Optional[Playlist]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self.library.playlist(selection[0])

    def _change(self) -> None:
        self.parent_app.save_library()
        self._reload()

    def _add(self) -> None:
        playlist = ask_playlist(self, title="Add playlist")
        if playlist is None:
            return
        stored = self.library.add_playlist(playlist)
        self._change()
        self.tree.selection_set(stored.id)

    def _add_preset(self, key: str) -> None:
        preset = store.preset_playlists().get(key)
        if preset is None:
            return
        name, url = preset
        stored = self.library.add_playlist(Playlist(url=url, name=name))
        self._change()
        self.tree.selection_set(stored.id)
        self.parent_app.set_status(
            f"Added {stored.label}; press 'Refresh selected' to fetch it"
        )

    def _browse_catalogue(self) -> None:
        """Open the country/language picker on top of this manager."""
        CatalogueDialog(self.parent_app)

    def _edit(self) -> None:
        playlist = self._selected()
        if playlist is None:
            return
        if ask_playlist(self, title=f"Edit {playlist.label}", playlist=playlist) is None:
            return
        self._change()

    def _remove(self) -> None:
        playlist = self._selected()
        if playlist is None:
            return

        owned = len(self.library.channels_of_playlist(playlist.id))
        keep = False
        if owned:
            answer = messagebox.askyesnocancel(
                TITLE,
                f"Remove “{playlist.label}”?\n\n"
                f"{owned} channel(s) come (also) from it.\n\n"
                "Yes: remove them too\nNo: keep the channels\nCancel: do nothing",
                parent=self,
            )
            if answer is None:
                return
            keep = not answer
        elif not messagebox.askyesno(TITLE, f"Remove “{playlist.label}”?", parent=self):
            return

        removed = self.library.remove_playlist(playlist.id, keep_channels=keep)
        self.parent_app.save_library()
        self.parent_app.reload_channels()
        self._reload()

        message = f"Removed {playlist.label}"
        if removed:
            message += f" ({removed} channel(s) removed)"
        self.parent_app.set_status(message)

    def _toggle(self) -> None:
        playlist = self._selected()
        if playlist is None:
            return
        self.library.set_playlist_enabled(playlist.id, not playlist.enabled)
        self._change()

    def _refresh_selected(self) -> None:
        playlist = self._selected()
        if playlist is None:
            self.parent_app.set_status("Select a playlist first", error=True)
            return
        self.destroy()
        self.parent_app.refresh_playlists(only=[playlist.id])


class Switcher(tk.Toplevel):
    """A tiny channel switcher: filter the favourites, Enter plays one."""

    def __init__(self, parent: "App", channels: List[Channel]) -> None:
        super().__init__(parent)
        self._channels = channels
        self._on_choose = parent.play_channel

        self.title("Favourites")
        self.transient(parent)
        self.geometry("480x420")

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        self.query = tk.StringVar()
        self.query.trace_add("write", self._on_query)
        entry = ttk.Entry(body, textvariable=self.query)
        entry.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))

        self.tree = ttk.Treeview(
            body, columns=("name", "group"), show="headings", selectmode="browse"
        )
        self.tree.heading("name", text="Favourite")
        self.tree.heading("group", text="Group")
        self.tree.column("name", width=300, anchor="w", stretch=True)
        self.tree.column("group", width=160, anchor="w")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<Return>", lambda _event: self._choose())
        self.tree.bind("<Double-1>", lambda _event: self._choose())

        self.footer = tk.StringVar()
        ttk.Label(body, textvariable=self.footer, foreground="#808080").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

        self.bind("<Escape>", lambda _event: self.destroy())
        self._reload()
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass
        entry.focus_set()
        entry.select_range(0, "end")

    def _on_query(self, *_args: object) -> None:
        self._reload()

    def _reload(self) -> None:
        self.tree.delete(*self.tree.get_children())
        rows = filter_channels(self._channels, self.query.get(), favourites_only=True)
        for channel in rows:
            self.tree.insert(
                "", "end", iid=channel.id, values=(channel.name, channel.group_label)
            )
        if rows:
            first = rows[0].id
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.footer.set(
                "Enter plays · Esc closes · Ctrl+PgUp/PgDn zap between favourites"
            )
        else:
            self.footer.set("No favourite matches — star a channel with Ctrl+D")

    def _choose(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        channel = next(
            (item for item in self._channels if item.id == selection[0]), None
        )
        if channel is None:
            return
        self.destroy()
        self._on_choose(channel)


def show_text(parent: tk.Misc, title: str, text: str) -> None:
    """Show read-only text (the tail of ``mpv.log``) in its own window."""
    window = tk.Toplevel(parent)
    window.title(title)
    window.transient(parent)
    window.geometry("760x420")

    body = ttk.Frame(window, padding=8)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    widget = tk.Text(body, wrap="none", height=20)
    widget.insert("1.0", text or "(empty)")
    widget.configure(state="disabled")
    scrollbar = ttk.Scrollbar(body, orient="vertical", command=widget.yview)
    widget.configure(yscrollcommand=scrollbar.set)
    widget.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")

    ttk.Button(body, text="Close", command=window.destroy).grid(
        row=1, column=0, columnspan=2, sticky="e", pady=(8, 0)
    )


class CatalogueBrowser(ttk.Frame):
    """Country/language browser shared by the catalogue dialog and the wizard.

    Loading the index runs on a worker thread and its result is picked up by the
    widget's own ``after`` poll, so Tk is only ever touched from the event loop.
    ``submit`` lets the dialogs run a slow job (probing a playlist) the same way.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        loader: Optional[Callable[..., Any]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_activate: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.loader = loader or catalogue.load_catalogue
        self._on_status = on_status
        self._on_activate = on_activate
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._catalogues: Dict[str, Optional[catalogue.Catalogue]] = {
            "country": None,
            "language": None,
        }
        self._configured: Set[str] = set()
        self._probes: Dict[str, bool] = {}
        self._loading: Set[str] = set()
        self._job: Optional[str] = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.tabs: Dict[str, Dict[str, Any]] = {}
        for kind, label in (("country", "Countries"), ("language", "Languages")):
            self.tabs[kind] = self._make_tab(kind, label)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._job = self.after(POLL_MS, self._poll)
        self.bind("<Destroy>", self._on_destroy)

    def current_kind(self) -> str:
        """Which tab is showing: ``"country"`` or ``"language"``."""
        try:
            index = self.notebook.index(self.notebook.select())
        except tk.TclError:  # pragma: no cover - no tabs yet
            return "country"
        return "language" if index == 1 else "country"

    def _on_tab_changed(self, _event: Optional[tk.Event] = None) -> None:
        """Keep the status line about the tab the user is looking at."""
        kind = self.current_kind()
        data = self._catalogues.get(kind)
        if data is None:
            self._status(f"Loading the {kind} index …")
        else:
            self._status(self._describe(kind, data))

    def _describe(self, kind: str, data: catalogue.Catalogue) -> str:
        origin = (
            f"cached copy from {data.fetched_label}"
            if data.from_cache
            else "downloaded just now"
        )
        noun = "countries" if kind == "country" else "languages"
        text = f"{len(data.entries)} {noun} · {origin}"
        if kind == "language" and data.total > len(data.entries):
            text += f" (of {data.total}; tick “Show every language”)"
        if data.error:
            text += f" · offline: {data.error}"
        return text

    def _on_destroy(self, event: tk.Event) -> None:
        """Cancel the poll when the widget (or the root above it) goes away."""
        if event.widget is self:
            self._cancel_poll()

    def _cancel_poll(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:  # pragma: no cover - interpreter already gone
                pass
            self._job = None

    def _make_tab(self, kind: str, label: str) -> Dict[str, Any]:
        frame = ttk.Frame(self.notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Search:").grid(row=0, column=0, padx=(0, 6))
        query = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=query)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<KeyRelease>", lambda _event, k=kind: self._apply(k))

        tree = ttk.Treeview(
            frame,
            columns=("name", "code", "state"),
            show="headings",
            selectmode="extended",
        )
        for column, text, width, stretch, anchor in (
            ("name", "Name", 330, True, "w"),
            ("code", "Code", 70, False, "w"),
            ("state", "", 100, False, "center"),
        ):
            tree.heading(column, text=text)
            tree.column(column, width=width, stretch=stretch, anchor=anchor)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.bind("<Double-1>", lambda _event, k=kind: self._activate(k))

        self.notebook.add(frame, text=label)
        state: Dict[str, Any] = {
            "frame": frame,
            "query": query,
            "entry": entry,
            "tree": tree,
        }
        if kind == "language":
            show_all = tk.BooleanVar(value=False)
            state["show_all"] = show_all
            state["show_all_box"] = ttk.Checkbutton(
                frame,
                text="Show every language",
                variable=show_all,
                command=lambda: self.reload("language", all_languages=show_all.get()),
            )
            state["show_all_box"].grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        return state

    # -- loading ------------------------------------------------------------
    def start(self) -> None:
        """Load both indexes (one request each, cached for a week)."""
        self.reload("country")
        self.reload("language")

    def reload(self, kind: str, *, all_languages: bool = False, refresh: bool = False) -> None:
        if kind in self._loading:
            return
        self._loading.add(kind)
        self._status(f"Loading the {kind} index …")
        threading.Thread(
            target=self._load_worker,
            args=(kind, all_languages, refresh),
            name=f"tdt-catalogue-{kind}",
            daemon=True,
        ).start()

    def _load_worker(self, kind: str, all_languages: bool, refresh: bool) -> None:
        try:
            data = self.loader(kind, all_languages=all_languages, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - the dialog must survive it
            self._queue.put({"kind": kind, "error": str(exc)})
            return
        self._queue.put({"kind": kind, "data": data})

    def submit(
        self,
        work: Callable[[], Any],
        done: Callable[[Any, Optional[BaseException]], None],
    ) -> None:
        """Run ``work`` off the event loop, then call ``done(result, error)`` here."""

        def runner() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - reported to the dialog
                self._queue.put({"callback": done, "error": exc})
                return
            self._queue.put({"callback": done, "result": result})

        threading.Thread(target=runner, name="tdt-catalogue-task", daemon=True).start()

    def _poll(self) -> None:
        self._job = None
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle(message)
        try:
            self._job = self.after(POLL_MS, self._poll)
        except tk.TclError:  # pragma: no cover - widget already destroyed
            self._job = None

    def _handle(self, message: Dict[str, Any]) -> None:
        callback = message.get("callback")
        if callback is not None:
            callback(message.get("result"), message.get("error"))
            return

        kind = str(message.get("kind") or "")
        if kind not in self.tabs:
            return
        self._loading.discard(kind)

        error = message.get("error")
        if error:
            self._status(f"Could not load the {kind} index: {error}")
            return

        data = message.get("data")
        if not isinstance(data, catalogue.Catalogue):
            return
        self._catalogues[kind] = data
        self._apply(kind)
        if kind == self.current_kind():
            self._status(self._describe(kind, data))

    # -- view ---------------------------------------------------------------
    def _apply(self, kind: str) -> None:
        state = self.tabs[kind]
        data = self._catalogues.get(kind)
        tree: ttk.Treeview = state["tree"]
        tree.delete(*tree.get_children())
        if data is None:
            return
        for entry in data.search(state["query"].get()):
            tree.insert(
                "",
                "end",
                iid=self._iid(kind, entry),
                values=(
                    f"{entry.flag} {entry.name}".strip(),
                    entry.code,
                    self._state_of(entry),
                ),
            )

    @staticmethod
    def _iid(kind: str, entry: catalogue.CatalogueEntry) -> str:
        return f"{kind}:{entry.code}"

    def _state_of(self, entry: catalogue.CatalogueEntry) -> str:
        if entry.url in self._configured:
            return "✓ added"
        if self._probes.get(entry.url) is False:
            return "! no playlist"
        return ""

    def _activate(self, kind: str) -> None:
        if self._on_activate is not None:
            self._on_activate(kind)

    def set_library(self, library: store.Library) -> None:
        """Mark the entries that are already configured."""
        self._configured = {playlist.url for playlist in library.playlists}
        for kind in self.tabs:
            self._apply(kind)

    def set_probes(self, probes: Dict[str, bool]) -> None:
        """Show the entries known to have no playlist."""
        self._probes = dict(probes)
        for kind in self.tabs:
            self._apply(kind)

    def selected_entries(self) -> List[catalogue.CatalogueEntry]:
        """Every selected entry, from both tabs."""
        chosen: List[catalogue.CatalogueEntry] = []
        for kind, state in self.tabs.items():
            data = self._catalogues.get(kind)
            if data is None:
                continue
            by_iid = {self._iid(kind, entry): entry for entry in data.entries}
            for iid in state["tree"].selection():
                entry = by_iid.get(iid)
                if entry is not None:
                    chosen.append(entry)
        return chosen

    def loaded(self, kind: str) -> bool:
        """``True`` once the index of ``kind`` has been received."""
        return self._catalogues.get(kind) is not None

    def _status(self, text: str) -> None:
        if self._on_status is not None:
            self._on_status(text)

    def destroy(self) -> None:
        self._cancel_poll()
        super().destroy()


class CatalogueDialog(tk.Toplevel):
    """Add playlists by country or language (the IPTV-org catalogue)."""

    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.app = parent
        self._pending: List[catalogue.CatalogueEntry] = []
        self._busy = False

        self.title("Add playlists by country or language")
        self.transient(parent)
        self.geometry("780x540")

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Loading the catalogue …")
        self.browser = CatalogueBrowser(
            body,
            loader=parent.catalogue_loader,
            on_status=self.status_var.set,
            on_activate=lambda _kind: self._add_selected(),
        )
        self.browser.grid(row=0, column=0, sticky="nsew")

        self.refresh_after = tk.BooleanVar(value=True)
        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        ttk.Button(buttons, text="Add selected", command=self._add_selected).pack(
            side="right"
        )
        ttk.Checkbutton(
            buttons, text="Refresh after adding", variable=self.refresh_after
        ).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Reload catalogue", command=self._reload).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(body, textvariable=self.status_var, foreground="#666666").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )

        self.browser.set_library(parent.library)
        self.browser.set_probes(parent.catalogue_probes())
        self.browser.start()
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass

    def _reload(self) -> None:
        show_all = bool(self.browser.tabs["language"]["show_all"].get())
        self.browser.reload("country", refresh=True)
        self.browser.reload("language", all_languages=show_all, refresh=True)

    # -- adding -------------------------------------------------------------
    def _add_selected(self) -> None:
        if self._busy:
            return
        entries = self.browser.selected_entries()
        if not entries:
            self.status_var.set("Select a country or language first")
            return

        self._busy = True
        self._pending = entries
        self.status_var.set(f"Checking {len(entries)} playlist(s) …")
        self.browser.submit(self._probe, self._probed)

    def _probe(self) -> Dict[str, Optional[bool]]:
        """Runs off the event loop: one HEAD request per selected playlist."""
        return {entry.url: self.app.probe_playlist(entry.url) for entry in self._pending}

    def _probed(self, verdicts: Any, error: Optional[BaseException]) -> None:
        self._busy = False
        if error is not None:
            self.status_var.set(f"Could not check the playlists: {error}")
            return

        report = catalogue.add_entries(
            self.app.library,
            self._pending,
            probe=lambda url: (verdicts or {}).get(url),
        )
        self.app.save_library()
        self.app.reload_channels()
        self.browser.set_library(self.app.library)
        self.browser.set_probes(self.app.catalogue_probes())

        message = report.summary
        if report.unavailable:
            message += " · " + ", ".join(
                entry.label for entry in report.unavailable[:3]
            )
        self.status_var.set(message)
        self.app.set_status(f"Catalogue: {message}")

        added = [playlist.id for playlist in report.added]
        if added and self.refresh_after.get():
            self.destroy()
            self.app.refresh_playlists(only=added)


class WelcomeDialog(tk.Toplevel):
    """First-run wizard: choose the countries/languages to start with."""

    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.app = parent
        self._pending: List[catalogue.CatalogueEntry] = []
        self._busy = False

        self.title("Welcome to tdt_player")
        self.transient(parent)
        self.geometry("800x600")

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        ttk.Label(
            body, text="Welcome to tdt_player", font=("TkDefaultFont", 14, "bold")
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            body,
            text=(
                "Where are your channels from? Pick one or more countries or "
                "languages and their IPTV-org playlists will be added and "
                "downloaded. You can change this at any time under "
                "Playlists… → Browse catalogue…"
            ),
            wraplength=740,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))

        self.status_var = tk.StringVar(value="Loading the catalogue …")
        self.browser = CatalogueBrowser(
            body,
            loader=parent.catalogue_loader,
            on_status=self.status_var.set,
            on_activate=lambda _kind: self._accept(),
        )
        self.browser.grid(row=2, column=0, sticky="nsew")

        self.disable_tdt = tk.BooleanVar(value=False)
        self.tdt_playlist = next(
            (
                playlist
                for playlist in parent.library.playlists
                if "tdtchannels" in playlist.url.casefold()
            ),
            None,
        )
        if self.tdt_playlist is not None:
            ttk.Checkbutton(
                body,
                text=(
                    "Also disable TDTChannels (it and IPTV-org use different "
                    "channel ids, so a country would be listed twice)"
                ),
                variable=self.disable_tdt,
            ).grid(row=3, column=0, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Add and refresh", command=self._accept).pack(
            side="right"
        )
        ttk.Button(buttons, text="Skip for now", command=self._skip).pack(
            side="right", padx=(0, 8)
        )

        ttk.Label(body, textvariable=self.status_var, foreground="#666666").grid(
            row=5, column=0, sticky="w", pady=(6, 0)
        )

        self.protocol("WM_DELETE_WINDOW", self._skip)
        self.browser.set_library(parent.library)
        self.browser.set_probes(parent.catalogue_probes())
        self.browser.start()
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass

    # -- actions ------------------------------------------------------------
    def _skip(self) -> None:
        self._finish()

    def _accept(self) -> None:
        if self._busy:
            return
        self._pending = self.browser.selected_entries()
        if not self._pending:
            # Nothing chosen: just download what is already configured
            # (TDTChannels on a fresh install).
            self._finish(refresh=True)
            return

        self._busy = True
        self.status_var.set(f"Checking {len(self._pending)} playlist(s) …")
        self.browser.submit(self._probe, self._probed)

    def _probe(self) -> Dict[str, Optional[bool]]:
        return {entry.url: self.app.probe_playlist(entry.url) for entry in self._pending}

    def _probed(self, verdicts: Any, error: Optional[BaseException]) -> None:
        self._busy = False
        if error is not None:
            self.status_var.set(f"Could not check the playlists: {error}")
            return

        report = catalogue.add_entries(
            self.app.library,
            self._pending,
            probe=lambda url: (verdicts or {}).get(url),
        )
        if report.unavailable:
            self.app.set_status(
                "No playlist for: "
                + ", ".join(entry.label for entry in report.unavailable)
            )
        added = [playlist.id for playlist in report.added]
        self._finish(refresh=True, only=added or None)

    def _finish(self, *, refresh: bool = False, only: Optional[List[str]] = None) -> None:
        self.app.library.settings["welcome_done"] = True
        if self.disable_tdt.get() and self.tdt_playlist is not None:
            self.app.library.set_playlist_enabled(self.tdt_playlist.id, False)
        self.app.save_library()
        self._busy = False
        self.destroy()
        self.app.welcome_closed()
        if refresh:
            self.app.refresh_playlists(only=only)


def apply_window_icon(window: tk.Misc) -> None:
    """Use the packaged PNG as the window icon (Tk reads PNG, no Pillow needed).

    The file sits next to this module in an installed copy and in the checkout;
    the hicolor theme copy made by ``packaging/install.sh`` is the fallback, and a
    missing icon is never an error.
    """
    candidates = [Path(__file__).with_name("tdtplayer.png")]
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    theme = Path(data_home) / "icons" / "hicolor"
    for size in (256, 128, 64, 48, 32, 24, 16):
        candidates.append(theme / f"{size}x{size}" / "apps" / "tdtplayer.png")

    for candidate in candidates:
        try:
            image = tk.PhotoImage(file=str(candidate))
        except (tk.TclError, OSError):
            continue
        try:
            window.iconphoto(True, image)
        except tk.TclError:  # pragma: no cover - window without a WM
            return
        # Tk does not own the image: keep it alive for as long as the window.
        window._icon_image = image  # noqa: SLF001 - the window holds the only reference
        break

    try:
        window.iconname("tdtplayer")
    except tk.TclError:  # pragma: no cover
        pass


class App(tk.Tk):
    """The application window: filters, channel table and status bar.

    ``loader``, ``library`` and ``player`` are injectable so the tests can drive
    the whole window without the network, the user's config or a real mpv.
    """

    def __init__(
        self,
        *,
        library: Optional[store.Library] = None,
        player: Optional[Any] = None,
        loader: Callable[[str], Any] = load_m3u,
        mpv: str = "mpv",
        first_run: Optional[bool] = None,
        catalogue_loader: Optional[Callable[..., Any]] = None,
        catalogue_probes_loader: Optional[Callable[[], Dict[str, bool]]] = None,
    ) -> None:
        # className "tdt_player" is the WM_CLASS the desktop entry's
        # StartupWMClass matches, so the task bar groups and icons the window.
        super().__init__(className="tdt_player")

        self.library = library if library is not None else store.load()
        self.loader = loader
        self.catalogue_loader = catalogue_loader or catalogue.load_catalogue
        self._probes_loader = catalogue_probes_loader or catalogue.load_probes
        self._probes: Dict[str, bool] = {}
        self._welcome: Optional[Any] = None
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._index: Dict[str, Channel] = {}
        self._visible: List[Channel] = []
        self._playlist_index: Dict[str, Playlist] = {}
        self._filter_job: Optional[str] = None
        self._busy = False
        self._closing = False
        self._refresh_results: List[refresh.RefreshResult] = []
        self._playing_channel: Optional[Channel] = None
        self._tick_job: Optional[str] = None
        self._welcome_job: Optional[str] = None
        # An injected player means the tests have taken over: never look for mpv.
        self._mpv_ok = True if player is not None else bool(mpv_available(mpv))

        if player is None:
            player = Player(
                mpv=mpv,
                extra_args=self.library.setting("mpv_args", []) or [],
                user_agent=self.library.setting("user_agent", "") or "",
            )
        self.player = player

        self.title(TITLE)
        apply_window_icon(self)
        self.geometry(self.library.setting("geometry", "") or "1020x660")
        self.minsize(820, 460)
        self._build_widgets()
        self._refresh_choices()
        self._apply_filter()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._tick)

        if first_run is None:
            first_run = self._should_offer_welcome()
        if first_run:
            self._welcome_job = self.after(250, self._maybe_first_run)

        if not self._mpv_ok:
            self.set_status(
                "mpv was not found; install it before playing anything", error=True
            )
        elif self.library.warnings:
            self.set_status(self.library.warnings[0], error=True)

    # -- construction -------------------------------------------------------
    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        style = ttk.Style(self)
        style.configure(ERROR_STYLE, foreground="#b00020")

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Search:").grid(row=0, column=0, padx=(0, 6))
        self.search_var = tk.StringVar(value=self.library.setting("last_filter", "") or "")
        self.search_var.trace_add("write", self._on_search_changed)
        self.search_entry = ttk.Entry(top, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")

        ttk.Label(top, text="Group:").grid(row=0, column=2, padx=(10, 6))
        self.group_var = tk.StringVar(value=ALL_GROUPS)
        self.group_box = ttk.Combobox(
            top,
            textvariable=self.group_var,
            values=[ALL_GROUPS],
            state="readonly",
            width=18,
        )
        self.group_box.grid(row=0, column=3)
        self.group_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_filter())

        ttk.Label(top, text="Playlist:").grid(row=0, column=4, padx=(10, 6))
        self.playlist_var = tk.StringVar(
            value=self.library.setting("last_playlist_filter", "") or ALL_PLAYLISTS
        )
        self.playlist_box = ttk.Combobox(
            top,
            textvariable=self.playlist_var,
            values=[ALL_PLAYLISTS],
            state="readonly",
            width=22,
        )
        self.playlist_box.grid(row=0, column=5)
        self.playlist_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_filter())

        self.favourites_var = tk.BooleanVar(
            value=bool(self.library.setting("favourites_only", False))
        )
        self.favourites_box = ttk.Checkbutton(
            top,
            text="★ only",
            variable=self.favourites_var,
            command=self._apply_filter,
        )
        self.favourites_box.grid(row=0, column=6, padx=(10, 0))

        buttons = ttk.Frame(self, padding=(8, 6))
        buttons.grid(row=1, column=0, sticky="ew")

        self.play_button = ttk.Button(buttons, text="Play", command=self._play_selected)
        self.play_button.pack(side="left")
        self.next_button = ttk.Button(
            buttons,
            text="Next source",
            command=lambda: self._play_selected(next_source=True),
        )
        self.next_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(buttons, text="Stop", command=self._stop_player)
        self.stop_button.pack(side="left", padx=(6, 0))

        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=10)

        self.favourite_button = ttk.Button(
            buttons, text="★ Favourite", command=self._toggle_favourite
        )
        self.favourite_button.pack(side="left")
        self.switcher_button = ttk.Button(
            buttons, text="Switcher…", command=self._open_switcher
        )
        self.switcher_button.pack(side="left", padx=(6, 0))

        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=10)

        self.add_button = ttk.Button(buttons, text="Add…", command=self._add_channel)
        self.add_button.pack(side="left")
        self.edit_button = ttk.Button(buttons, text="Edit…", command=self._edit_selected)
        self.edit_button.pack(side="left", padx=(6, 0))
        self.remove_button = ttk.Button(buttons, text="Remove", command=self._remove_selected)
        self.remove_button.pack(side="left", padx=(6, 0))

        self.playlists_button = ttk.Button(
            buttons, text="Playlists…", command=self._open_playlists
        )
        self.playlists_button.pack(side="right")
        self.refresh_button = ttk.Button(
            buttons, text="Refresh all", command=self.refresh_playlists
        )
        self.refresh_button.pack(side="right", padx=(0, 6))

        self._action_buttons = [
            self.play_button,
            self.next_button,
            self.add_button,
            self.edit_button,
            self.remove_button,
            self.playlists_button,
            self.refresh_button,
        ]

        table = ttk.Frame(self, padding=(8, 0))
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        columns = ("star", "name", "group", "playlist")
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("star", text="★")
        self.tree.heading("name", text="Channel")
        self.tree.heading("group", text="Group")
        self.tree.heading("playlist", text="Playlist")
        self.tree.column("star", width=34, stretch=False, anchor="center")
        self.tree.column("name", width=400, stretch=True, anchor="w")
        self.tree.column("group", width=250, stretch=True, anchor="w")
        self.tree.column("playlist", width=200, stretch=False, anchor="w")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Double-1>", lambda _event: self._play_selected())
        self.tree.bind("<Return>", lambda _event: self._play_selected())
        self.tree.bind("<Delete>", lambda _event: self._remove_selected())
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_title())

        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Play", command=self._play_selected)
        self.menu.add_command(
            label="Play next source",
            command=lambda: self._play_selected(next_source=True),
        )
        self.menu.add_separator()
        self.menu.add_command(label="Favourite (Ctrl+D)", command=self._toggle_favourite)
        self.menu.add_separator()
        self.menu.add_command(label="Copy URL", command=self._copy_url)
        self.menu.add_command(label="Edit…", command=self._edit_selected)
        self.menu.add_command(label="Remove", command=self._remove_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Playlists…", command=self._open_playlists)
        self.menu.add_command(label="Show mpv log…", command=self._show_mpv_log)

        status = ttk.Frame(self, padding=(8, 4, 8, 6))
        status.grid(row=3, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(
            status,
            textvariable=self.status_var,
            anchor="w",
            relief="sunken",
            padding=(6, 2),
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.counts_var = tk.StringVar(value="")
        self.counts_label = ttk.Label(status, textvariable=self.counts_var, padding=(6, 2))
        self.counts_label.grid(row=0, column=1, padx=(8, 0))
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=110)
        self.progress.grid(row=0, column=2, padx=(8, 0))
        self.progress.grid_remove()

        for sequence, callback in (
            ("<Control-f>", lambda _event: self._focus_search()),
            ("<Control-F>", lambda _event: self._focus_search()),
            ("<Control-d>", lambda _event: self._toggle_favourite()),
            ("<Control-D>", lambda _event: self._toggle_favourite()),
            ("<Control-p>", lambda _event: self._open_switcher()),
            ("<Control-P>", lambda _event: self._open_switcher()),
            ("<Control-Next>", lambda _event: self._zap(1)),
            ("<Control-Prior>", lambda _event: self._zap(-1)),
            ("<Escape>", lambda _event: self.search_var.set("")),
            ("<F5>", lambda _event: self.refresh_playlists()),
            ("<Control-n>", lambda _event: self._add_channel()),
        ):
            self.bind(sequence, callback)

    # -- filtering ----------------------------------------------------------
    def _on_search_changed(self, *_args: object) -> None:
        """Debounce keystrokes so a 500-row table is not rebuilt per character."""
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except tk.TclError:
                pass
        self._filter_job = self.after(SEARCH_DEBOUNCE_MS, self._apply_filter)

    def _refresh_choices(self) -> None:
        """Keep the group and playlist comboboxes in step with the library."""
        group_names = [ALL_GROUPS] + channel_groups(self.library.channels)
        self.group_box.configure(values=group_names)
        if self.group_var.get() not in group_names:
            self.group_var.set(ALL_GROUPS)

        self._playlist_index = {
            playlist.label: playlist for playlist in self.library.playlists
        }
        playlist_names = [ALL_PLAYLISTS] + list(self._playlist_index)
        self.playlist_box.configure(values=playlist_names)
        if self.playlist_var.get() not in playlist_names:
            self.playlist_var.set(ALL_PLAYLISTS)

    def _playlist_names(self, channel: Channel) -> List[str]:
        names: List[str] = []
        for playlist_id in channel.playlists:
            playlist = self.library.playlist(playlist_id)
            names.append(playlist.label if playlist is not None else playlist_id)
        return names or [MANUAL_LABEL]

    def _apply_filter(self) -> None:
        """Rebuild the table from the search box and the three filters."""
        self._filter_job = None
        previous = self._selected_channel()
        self._refresh_choices()

        label = self.playlist_var.get()
        playlist = self._playlist_index.get(label)
        group = self.group_var.get()

        self._index = {channel.id: channel for channel in self.library.channels}
        self._visible = filter_channels(
            self.library.channels,
            self.search_var.get(),
            None if group in (ALL_GROUPS, "") else group,
            playlist=playlist.id if playlist is not None else None,
            favourites_only=self.favourites_var.get(),
        )

        self.tree.delete(*self.tree.get_children())
        for channel in self._visible:
            self.tree.insert(
                "",
                "end",
                iid=channel.id,
                values=(
                    "★" if channel.favourite else "",
                    channel.name,
                    channel.group_label,
                    ", ".join(self._playlist_names(channel)),
                ),
            )

        if previous is not None and self.tree.exists(previous.id):
            self.tree.selection_set(previous.id)
            self.tree.see(previous.id)
        elif self._visible:
            self.tree.selection_set(self._visible[0].id)
        else:
            self.title(TITLE)

        self.counts_var.set(
            f"{len(self.library.channels)} channels · {len(self._visible)} shown"
        )
        if not self.library.channels:
            self.set_status(
                "No channels yet — press “Refresh all”, or add feeds under "
                "“Playlists…” → “Browse catalogue…”"
            )

    def _focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    # -- selection ----------------------------------------------------------
    def _selected_channel(self) -> Optional[Channel]:
        selection = self.tree.selection()
        if not selection:
            return None
        return self._index.get(selection[0])

    def _select(self, channel_id: str) -> None:
        if self.tree.exists(channel_id):
            self.tree.selection_set(channel_id)
            self.tree.focus(channel_id)
            self.tree.see(channel_id)

    def _update_title(self) -> None:
        """Mirror the selected channel into the window title."""
        channel = self._selected_channel()
        if channel is None:
            self.title(TITLE)
            return
        mirrors = len(channel.playable_sources())
        suffix = f" · {mirrors} sources" if mirrors > 1 else ""
        self.title(f"{channel.label}{suffix} — {TITLE}")

    def _show_context_menu(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # -- status bar ---------------------------------------------------------
    def set_status(self, text: str, *, error: bool = False) -> None:
        """Show a message in the status bar (errors in red)."""
        self.status_var.set(text)
        self.status_label.configure(style=ERROR_STYLE if error else "TLabel")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._action_buttons:
            button.configure(state=state)
        if busy:
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    # -- playback -----------------------------------------------------------
    def play_channel(self, channel: Channel, *, start_index: int = 0) -> None:
        """Play ``channel`` (also used by the switcher)."""
        if not channel.sources:
            self.set_status(f"{channel.name} has no stream URL", error=True)
            return
        if not self._mpv_ok:
            messagebox.showerror(
                TITLE,
                "mpv was not found on this system.\n\nInstall mpv and start "
                "tdt_player again.",
                parent=self,
            )
            return
        if not self.player.play(channel, start_index=start_index):
            self.set_status(f"{channel.name} has no stream URL", error=True)
            return
        self._playing_channel = channel
        self.set_status(f"Starting {channel.name} …")

    def _play_selected(self, next_source: bool = False) -> None:
        channel = self._selected_channel()
        if channel is None:
            self.set_status("Select a channel first", error=True)
            return
        self.play_channel(channel, start_index=1 if next_source else 0)

    def _stop_player(self) -> None:
        self.player.stop()
        self._playing_channel = None
        self.set_status("Stopped")

    def _copy_url(self) -> None:
        channel = self._selected_channel()
        if channel is None or not channel.sources:
            self.set_status("Select a channel first", error=True)
            return
        self.clipboard_clear()
        self.clipboard_append(channel.url)
        self.set_status(f"Copied {channel.url}")

    def _show_mpv_log(self) -> None:
        path = getattr(self.player, "log_path", None)
        text = ""
        if path is not None:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = f"Cannot read {path}: {exc}"
        lines = text.splitlines()[-40:]
        show_text(self, "mpv log", "\n".join(lines) or "(the log is empty)")

    # -- favourites ---------------------------------------------------------
    def _toggle_favourite(self) -> None:
        channel = self._selected_channel()
        if channel is None:
            self.set_status("Select a channel to star", error=True)
            return
        channel.favourite = not channel.favourite
        self.save_library()
        self._apply_filter()
        self._select(channel.id)
        self.set_status(f"{channel.name}: {'starred' if channel.favourite else 'unstarred'}")

    def _open_switcher(self) -> None:
        channels = [channel for channel in self.library.channels if channel.favourite]
        if not channels:
            # Nothing starred yet: let the switcher double as a fast picker.
            channels = list(self.library.channels)
        if not channels:
            self.set_status("No channels stored yet; refresh a playlist first", error=True)
            return
        Switcher(self, channels)

    def _zap(self, step: int) -> None:
        """Play the next/previous favourite (or visible channel), wrapping around."""
        pool = [channel for channel in self._visible if channel.favourite]
        if len(pool) < 2:
            pool = self._visible
        if not pool:
            self.set_status("Nothing to switch between", error=True)
            return

        current = self._playing_channel
        if current in pool:
            index = (pool.index(current) + step) % len(pool)
        else:
            index = 0 if step > 0 else len(pool) - 1
        channel = pool[index]
        self._select(channel.id)
        self.play_channel(channel)

    # -- event loop ---------------------------------------------------------
    def _tick(self) -> None:
        if self._closing:
            return
        self._drain_worker_events()
        self._drain_player_events()
        self._tick_job = self.after(POLL_MS, self._tick)

    def _drain_player_events(self) -> None:
        drain = getattr(self.player, "drain", None)
        if drain is None:
            return
        for event in drain():
            kind = event.get("kind")
            channel = event.get("channel")
            name = channel.name if channel is not None else "channel"
            if kind == "started":
                attempt = int(event.get("attempt", 1))
                total = int(event.get("total", 1))
                suffix = f" (source {attempt}/{total})" if total > 1 else ""
                self.set_status(f"Trying {name}{suffix} …")
            elif kind == "loaded":
                self._playing_channel = channel if isinstance(channel, Channel) else None
                self.set_status(f"Playing {name}")
            elif kind == "source-failed":
                self.set_status(f"{name}: that source failed, trying the next one …")
            elif kind == "finished":
                self._playing_channel = None
                self.set_status(f"{name} finished")
            elif kind == "failed":
                self.set_status(
                    f"{name}: {event.get('message', 'no playable source')}", error=True
                )
            elif kind == "error":
                self.set_status(
                    f"{name}: {event.get('message', 'could not start mpv')}", error=True
                )
            elif kind == "ipc-unavailable":
                self.set_status("mpv IPC unavailable; using one player per channel")

    # -- refreshing ---------------------------------------------------------
    def refresh_playlists(self, only: Optional[Sequence[str]] = None) -> None:
        """Fetch every enabled playlist (or just ``only``) on a worker thread."""
        if self._busy:
            return
        targets = refresh.refresh_targets(self.library, only)
        if not targets:
            self.set_status(
                "No enabled playlist to refresh; add one under “Playlists…”",
                error=True,
            )
            return

        self._set_busy(True)
        self._refresh_results = []
        self.set_status(f"Refreshing {len(targets)} playlist(s) …")
        threading.Thread(
            target=self._worker_refresh,
            args=(targets,),
            name="tdt-refresh",
            daemon=True,
        ).start()

    def _worker_refresh(self, targets: List[Playlist]) -> None:
        """Runs off the event loop: network and parsing only, never Tk or merging."""
        total = len(targets)
        for index, playlist in enumerate(targets, start=1):
            self._events.put(
                {
                    "kind": "progress",
                    "index": index,
                    "total": total,
                    "name": playlist.label,
                }
            )
            fetched = refresh.fetch_playlist(playlist, loader=self.loader)
            self._events.put({"kind": "fetched", "fetched": fetched})
        self._events.put({"kind": "refresh-done"})

    def _drain_worker_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return

            kind = event.get("kind")
            if kind == "progress":
                self.set_status(f"[{event['index']}/{event['total']}] {event['name']} …")
                continue
            if kind == "fetched":
                result = refresh.apply_refresh(self.library, event["fetched"])
                self._refresh_results.append(result)
                if result.ok:
                    self._apply_filter()
                continue
            if kind == "refresh-done":
                self._finish_refresh()
                continue
            if kind == "error":
                self._set_busy(False)
                self.set_status(str(event.get("message") or "refresh failed"), error=True)

    def _finish_refresh(self) -> None:
        self._set_busy(False)
        self._apply_filter()
        results = self._refresh_results
        if not results:
            self.set_status("Nothing was refreshed", error=True)
            return

        failures = [result for result in results if not result.ok]
        summary = refresh.summarise(results)
        if failures:
            summary += f" — {failures[0].error or failures[0].summary}"
        self.set_status(summary, error=bool(failures))

    # -- editing ------------------------------------------------------------
    def _add_channel(self) -> None:
        channel = ask_channel(self, title="Add channel")
        if channel is None:
            return
        self.library.add(channel)
        self.save_library()
        self.search_var.set("")
        self._apply_filter()
        self._select(channel.id)
        self.set_status(f"Added {channel.name}")

    def _edit_selected(self) -> None:
        channel = self._selected_channel()
        if channel is None:
            self.set_status("Select a channel to edit", error=True)
            return
        if ask_channel(self, title=f"Edit {channel.name}", channel=channel) is None:
            return
        self.save_library()
        self._apply_filter()
        self._select(channel.id)
        self.set_status(f"Updated {channel.name}")

    def _remove_selected(self) -> None:
        channel = self._selected_channel()
        if channel is None:
            self.set_status("Select a channel to remove", error=True)
            return
        if not messagebox.askyesno(TITLE, f"Remove {channel.name}?", parent=self):
            return
        if self.library.remove(channel.id):
            self.save_library()
            self._apply_filter()
            self.set_status(f"Removed {channel.name}")

    def _open_playlists(self) -> None:
        PlaylistsDialog(self)

    # -- persistence --------------------------------------------------------
    def save_library(self) -> None:
        """Write the library, reporting a failure in the status bar."""
        try:
            self.library.save()
        except OSError as exc:
            self.set_status(f"Could not save {self.library.path}: {exc}", error=True)

    def reload_channels(self) -> None:
        """Re-read the library into the comboboxes and the table."""
        self._refresh_choices()
        self._apply_filter()

    # -- first run and the catalogue ----------------------------------------
    def _should_offer_welcome(self) -> bool:
        """An empty library that has never seen the wizard should offer it."""
        if self.library.channels:
            return False
        return not bool(self.library.setting("welcome_done", False))

    def _maybe_first_run(self) -> None:
        if self._closing:
            return
        if self._should_offer_welcome():
            self.show_welcome()

    def show_welcome(self) -> Any:
        """Open the first-run wizard (also called directly by the tests)."""
        if self._welcome is not None:
            try:
                if self._welcome.winfo_exists():
                    self._welcome.lift()
                    return self._welcome
            except tk.TclError:  # pragma: no cover - already destroyed
                pass
            self._welcome = None
        self._welcome = WelcomeDialog(self)
        return self._welcome

    def welcome_closed(self) -> None:
        """Forget the wizard once it closed itself."""
        self._welcome = None

    def catalogue_probes(self) -> Dict[str, bool]:
        """The playlist probes remembered from earlier runs."""
        if not self._probes:
            try:
                self._probes = dict(self._probes_loader())
            except Exception:  # noqa: BLE001 - a stale cache is not worth crashing over
                self._probes = {}
        return dict(self._probes)

    def probe_playlist(self, url: str) -> Optional[bool]:
        """Does ``url`` exist?  One ``HEAD`` request, remembered on disk."""
        verdict = catalogue.probe_with_cache(url, cached=self.catalogue_probes())
        if verdict is not None:
            self._probes[url] = verdict
        return verdict

    # -- shutdown -----------------------------------------------------------
    def _on_close(self) -> None:
        self._closing = True
        self.library.settings["geometry"] = self.geometry()
        self.library.settings["last_filter"] = self.search_var.get()
        self.library.settings["last_playlist_filter"] = self.playlist_var.get()
        self.library.settings["favourites_only"] = bool(self.favourites_var.get())
        try:
            self.player.stop()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
        self.save_library()
        self.destroy()

    def destroy(self) -> None:
        """Cancel pending callbacks before tearing the window down.

        ``after`` jobs that outlive their widget make Tk print
        ``invalid command name`` from its background error handler, which is
        noise both for the user closing the window and for the test-suite.
        """
        self._closing = True
        for job in (self._filter_job, self._tick_job, self._welcome_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._filter_job = None
        self._tick_job = None
        self._welcome_job = None
        super().destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``tdtplayer gui`` and ``tdtplayer-gui``."""
    parser = argparse.ArgumentParser(
        prog="tdtplayer-gui", description="Desktop front-end for tdt_player."
    )
    parser.add_argument("--config", metavar="PATH", default=None, help="config file to use")
    parser.add_argument("--mpv", metavar="PATH", default=None, help="mpv executable to use")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    library = store.load(Path(args.config).expanduser() if args.config else None)
    app = App(library=library, mpv=args.mpv or library.setting("mpv_path", "mpv"))
    app.mainloop()
    return 0
