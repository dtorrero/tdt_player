"""Start ``mpv`` for a channel, switch between channels and report what it does.

Half of an IPTV list is dead on any given day: geo-blocked, moved, or "up" but
serving nothing.  TDTChannels ships several mirrors per station precisely because
of that, so playing a channel means *trying* its sources rather than trusting the
first one.  :func:`iter_attempts` yields them in order, :func:`run_blocking`
plays them one after another for the CLI and :class:`Player` does the same for the
GUI, reporting progress through a queue the Tk event loop drains (no widget is
ever touched from a worker thread).

The GUI drives mpv over its **JSON IPC socket** (:class:`MpvIPC`): one long-lived
mpv process is started with ``--idle=yes --input-ipc-server=...`` and every
channel switch is a ``loadfile <url> replace`` command, so zapping between
favourites does not close and reopen the player window.  That connection is also
what makes "is it *actually* playing?" answerable: mpv reports ``file-loaded``
when a stream really started and ``end-file`` with ``reason="error"`` when it did
not (dead host, HTTP 404, geo-block), and both are forwarded to the GUI.  When
the socket cannot be used at all, the player falls back to spawning one mpv per
channel, exactly like version 0.1 did.

mpv is started in its own session, so it owes nothing to the app that launched
it: :meth:`Player.detach` lets go of it without asking it to quit (that is what
the window does when it closes with *Keep player open* ticked), and
:meth:`Player.stop` is the one that really closes the player down.

mpv does not understand ``#EXTVLCOPT``, so the referrer/user-agent an entry asked
for are translated into ``--referrer=``/``--user-agent=`` flags (or, while
switching, into ``set_property`` commands).  mpv's own default user agent
(``libmpv``) is refused by several providers, hence the browser-like
:data:`DEFAULT_STREAM_USER_AGENT`.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from .models import Channel, Source

#: Browser-like agent; mpv's default ("libmpv") is rejected by some providers.
DEFAULT_STREAM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

#: How many mirrors of one channel to try before declaring it unplayable.
MAX_ATTEMPTS = 3

#: Pause between two attempts; a doomed mirror usually fails at once.
RETRY_DELAY = 0.5

#: How long mpv may take to report ``file-loaded`` before a source counts as dead.
LOAD_TIMEOUT = 10.0

#: How long to wait for mpv to create its IPC socket.
IPC_TIMEOUT = 5.0


class PlayerError(RuntimeError):
    """Raised when mpv cannot be started at all."""


@dataclass(frozen=True)
class Attempt:
    """One step of the fallback sequence produced by :func:`iter_attempts`."""

    index: int
    source: Source
    number: int


def mpv_available(mpv: str = "mpv") -> Optional[str]:
    """Return the resolved path of ``mpv``, or ``None`` when it is not installed."""
    if not mpv:
        return None
    return shutil.which(mpv)


def default_log_path() -> Path:
    """Where mpv's output is appended: ``$XDG_CACHE_HOME/tdt_player/mpv.log``."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tdt_player" / "mpv.log"


def build_mpv_command(
    channel: Channel,
    source: Optional[Source] = None,
    *,
    extra_args: Sequence[str] = (),
    user_agent: str = "",
    mpv: str = "mpv",
) -> List[str]:
    """Build the ``mpv`` command line for one channel/source.

    The URL is always last so anything appended by the caller's ``extra_args``
    applies to it.
    """
    chosen = source or (channel.sources[0] if channel.sources else None)
    if chosen is None or not chosen.url:
        raise ValueError(f"channel {channel.name!r} has no stream URL")

    command: List[str] = [mpv or "mpv"]
    command.append("--force-window=yes")
    command.append(f"--title={channel.name or chosen.url}")
    command.append(
        f"--user-agent={chosen.user_agent or user_agent or DEFAULT_STREAM_USER_AGENT}"
    )
    if chosen.referrer:
        command.append(f"--referrer={chosen.referrer}")
    command.extend(str(argument) for argument in extra_args)
    command.append(chosen.url)
    return command


def iter_attempts(channel: Channel, start_index: int = 0) -> Iterator[Attempt]:
    """Yield up to :data:`MAX_ATTEMPTS` sources, starting at ``start_index``.

    Two playlists can advertise the same stream; the URL is therefore tried once.
    Wrapping around means "play the next source" from the last mirror continues
    with the first one instead of doing nothing.
    """
    sources = channel.playable_sources() if channel.sources else []
    if not sources:
        return
    total = len(sources)
    start = start_index % total
    limit = min(total, MAX_ATTEMPTS)
    for step in range(limit):
        index = (start + step) % total
        yield Attempt(index=index, source=sources[index], number=step + 1)


def spawn_mpv(command: Sequence[str], log_path: Path) -> Any:
    """Start mpv detached, appending its output to ``log_path``."""
    target = Path(log_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "ab") as log:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = " ".join(shlex.quote(part) for part in command)
        log.write(f"\n--- {stamp} {line}\n".encode("utf-8"))
        log.flush()
        return subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def run_blocking(
    channel: Channel,
    *,
    start_index: int = 0,
    extra_args: Sequence[str] = (),
    user_agent: str = "",
    mpv: str = "mpv",
) -> int:
    """Play ``channel`` in the foreground, trying mirrors; return mpv's exit code."""
    status = 1
    for attempt in iter_attempts(channel, start_index):
        command = build_mpv_command(
            channel,
            attempt.source,
            extra_args=extra_args,
            user_agent=user_agent,
            mpv=mpv,
        )
        try:
            status = subprocess.call(command)
        except OSError as exc:
            raise PlayerError(str(exc)) from exc
        if status == 0:
            return 0
    return status


class MpvIPC:
    """A connection to mpv's ``--input-ipc-server`` socket.

    The protocol is one JSON object per line in each direction, so a reader
    thread turns the socket into a queue of messages and :meth:`command` writes
    requests.  Everything mpv sends is kept as a plain dict; interpreting the
    ``event`` entries is the caller's job.
    """

    def __init__(self, path: os.PathLike, *, timeout: float = IPC_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._closed = False

    def connect(self) -> None:
        """Wait for mpv to create the socket and start reading from it."""
        deadline = time.monotonic() + self.timeout
        while True:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(str(self.path))
            except OSError as exc:
                connection.close()
                if time.monotonic() >= deadline:
                    raise OSError(f"{self.path}: {exc}") from exc
                time.sleep(0.05)
                continue
            connection.settimeout(0.5)
            self._socket = connection
            self._thread = threading.Thread(
                target=self._read_loop, name="tdt-mpv-ipc", daemon=True
            )
            self._thread.start()
            return

    def command(self, *args: Any) -> int:
        """Send one command; returns its request id (``-1`` when the link is gone)."""
        if self._socket is None or self._closed:
            return -1
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        try:
            self._socket.sendall(payload.encode("utf-8"))
        except OSError:
            return -1
        return request_id

    def drain(self) -> List[Dict[str, Any]]:
        """Return every message received since the last call (never blocks)."""
        messages: List[Dict[str, Any]] = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                return messages

    def close(self) -> None:
        """Close the socket and stop the reader thread."""
        self._closed = True
        connection = self._socket
        self._socket = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def _read_loop(self) -> None:
        buffer = b""
        while not self._closed:
            connection = self._socket
            if connection is None:
                return
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._queue.put(message)


class Player:
    """Run mpv (or drive it over IPC) and report what happens through a queue.

    ``spawn`` is injectable (it must accept ``(command, log_path)`` and return an
    object with ``poll()``/``terminate()``/``kill()``, and for the simple path
    ``wait()``), and so is ``ipc_factory`` (a callable taking the socket path and
    returning an object with ``connect()``/``command()``/``drain()``/``close()``).
    That is how the tests exercise both paths without starting a real player.

    A custom ``spawn`` without a custom ``ipc_factory`` is treated as a test
    double and keeps the simple one-process-per-channel path.
    """

    def __init__(
        self,
        *,
        mpv: str = "mpv",
        extra_args: Sequence[str] = (),
        user_agent: str = "",
        log_path: Optional[os.PathLike] = None,
        spawn: Optional[Callable[[Sequence[str], Path], Any]] = None,
        use_ipc: Optional[bool] = None,
        ipc_factory: Optional[Callable[[Path], Any]] = None,
        load_timeout: float = LOAD_TIMEOUT,
    ) -> None:
        self.mpv = mpv or "mpv"
        self.extra_args = list(extra_args)
        self.user_agent = user_agent
        self.log_path = Path(log_path) if log_path is not None else default_log_path()
        self.load_timeout = float(load_timeout)
        self._spawn = spawn or spawn_mpv
        self._ipc_factory = ipc_factory or MpvIPC
        if use_ipc is None:
            use_ipc = spawn is None or ipc_factory is not None
        self.use_ipc = bool(use_ipc)

        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._lock = threading.Lock()
        self._process: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._run_id = 0

        # IPC state: the connection, the channel being played and where in its
        # mirror list we are.
        self._ipc: Optional[Any] = None
        self._ipc_path: Optional[Path] = None
        self._channel: Optional[Channel] = None
        self._attempts: Optional[Iterator[Attempt]] = None
        self._pending: Optional[Attempt] = None
        self._playing: Optional[Source] = None
        self._load_deadline = 0.0

    # -- public API ---------------------------------------------------------
    @property
    def ipc_active(self) -> bool:
        """``True`` while mpv is driven over its IPC socket."""
        return self._ipc is not None

    @property
    def running(self) -> bool:
        """``True`` while an mpv process we started is alive."""
        process = self._process
        if process is None:
            return False
        try:
            return process.poll() is None
        except Exception:  # noqa: BLE001 - injected test doubles may not poll
            return True

    def play(self, channel: Channel, *, start_index: int = 0) -> bool:
        """Start playing ``channel``; switching channels reuses the same mpv."""
        if not channel.sources:
            return False

        if self.use_ipc:
            if self._ipc is None:
                try:
                    self._ensure_ipc(channel)
                except Exception as exc:  # noqa: BLE001 - fall back to one mpv per channel
                    self.use_ipc = False
                    self._emit("ipc-unavailable", message=str(exc))
            if self._ipc is not None:
                with self._lock:
                    self._run_id += 1
                self._channel = channel
                self._attempts = iter_attempts(channel, start_index)
                self._pending = None
                self._playing = None
                for event in self._advance("play"):
                    self._events.put(event)
                return True

        self.stop()
        with self._lock:
            self._run_id += 1
            run_id = self._run_id
        return self._start_spawn(channel, start_index, run_id)

    # -- IPC internals ------------------------------------------------------
    def _ensure_ipc(self, channel: Channel) -> None:
        """Start mpv in idle mode with an IPC socket and connect to it."""
        if self._ipc is not None:
            return

        directory = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        path = directory / f"tdt_player-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        try:
            path.unlink()
        except OSError:
            pass

        command = [self.mpv]
        command.extend(str(argument) for argument in self.extra_args)
        # Ours come last so a user-supplied --input-ipc-server cannot shadow it.
        command.extend(
            [
                "--idle=yes",
                "--force-window=yes",
                f"--input-ipc-server={path}",
                f"--title={channel.name or 'tdt_player'}",
                f"--user-agent={self.user_agent or DEFAULT_STREAM_USER_AGENT}",
            ]
        )

        process = self._spawn(command, self.log_path)
        connection = self._ipc_factory(path)
        try:
            connection.connect()
        except Exception:
            self._terminate(process)
            raise

        with self._lock:
            self._process = process
        self._ipc = connection
        self._ipc_path = path

    def _advance(self, reason: str) -> List[Dict[str, Any]]:
        """Load the next mirror (or report that none is left)."""
        attempt = next(self._attempts, None) if self._attempts is not None else None
        if attempt is None:
            self._pending = None
            self._playing = None
            return [
                {
                    "kind": "failed",
                    "channel": self._channel,
                    "message": f"mpv could not play any source ({reason})",
                }
            ]

        self._pending = attempt
        self._playing = attempt.source
        self._load_deadline = time.monotonic() + self.load_timeout
        self._apply_source_headers(attempt.source)

        if self._ipc is not None:
            if self._channel is not None:
                self._ipc.command("set_property", "title", self._channel.name)
            self._ipc.command("loadfile", attempt.source.url, "replace")

        total = len(self._channel.playable_sources()) if self._channel is not None else 1
        return [
            {
                "kind": "started",
                "channel": self._channel,
                "source": attempt.source,
                "attempt": attempt.number,
                "total": min(total, MAX_ATTEMPTS),
            }
        ]

    def _apply_source_headers(self, source: Source) -> None:
        """Set the referrer/agent a playlist asked for before loading the URL."""
        if self._ipc is None:
            return
        agent = source.user_agent or self.user_agent or DEFAULT_STREAM_USER_AGENT
        self._ipc.command("set_property", "user-agent", agent)
        self._ipc.command("set_property", "referrer", source.referrer or "")

    def stop(self) -> None:
        """Stop playing: quit mpv, drop the socket and forget the state."""
        self._release(quit_mpv=True)

    def detach(self) -> None:
        """Leave mpv running on its own and forget about it.

        The window calls this when it closes with *Keep player open* enabled:
        the socket is closed and the player is handed over to the session (it
        was started with ``start_new_session=True``, so nothing ties it to this
        process), but it is neither told to quit nor terminated, so its window
        keeps playing.  ``idle`` is switched off first, so the detached player
        closes itself when the stream ends instead of lingering as an empty
        black window.
        """
        self._release(quit_mpv=False)

    def _release(self, *, quit_mpv: bool) -> None:
        """Forget the current player, actually quitting mpv only on request."""
        with self._lock:
            self._run_id += 1
            process = self._process
            connection = self._ipc
            self._process = None
            self._ipc = None

        self._pending = None
        self._attempts = None
        self._playing = None
        self._channel = None
        self._load_deadline = 0.0

        if connection is not None:
            try:
                if quit_mpv:
                    connection.command("quit")
                else:
                    # Let mpv exit by itself once the current file ends.
                    connection.command("set_property", "idle", "no")
            except Exception:  # noqa: BLE001 - letting go must never raise
                pass
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

        if process is not None:
            if quit_mpv:
                if connection is not None:
                    # mpv was asked to quit: give it a moment to close its window.
                    self._wait_for_exit(process, 1.0)
                    if not self._has_exited(process):
                        self._terminate(process)
                else:
                    self._terminate(process)
            else:
                self._reap_later(process)

        path = self._ipc_path
        self._ipc_path = None
        if path is not None:
            # mpv does not remove its socket on exit; it is ours to delete.
            try:
                Path(path).unlink()
            except OSError:
                pass

    @staticmethod
    def _reap_later(process: Any) -> None:
        """Collect a detached mpv in the background so it cannot become a zombie."""

        def reap() -> None:
            try:
                process.wait()
            except Exception:  # noqa: BLE001 - the player is no longer ours to mind
                pass

        threading.Thread(target=reap, name="tdt-player-reap", daemon=True).start()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Join the spawn-mode worker thread; ``True`` when it has finished."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def drain(self) -> List[Dict[str, Any]]:
        """Return every event since the last call, including mpv's own reports."""
        events: List[Dict[str, Any]] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break

        connection = self._ipc
        if connection is None:
            return events

        for message in connection.drain():
            events.extend(self._handle_message(message))

        process = self._process
        if process is not None and self._has_exited(process):
            # mpv was closed behind our back (window closed, crash, quit).
            events.append(
                {"kind": "finished", "channel": self._channel, "source": self._playing}
            )
            self._shutdown_ipc()
            return events

        if self._pending is not None and time.monotonic() > self._load_deadline:
            source = self._pending.source
            self._pending = None
            events.append(
                {
                    "kind": "source-failed",
                    "channel": self._channel,
                    "source": source,
                    "message": "timed out waiting for mpv",
                }
            )
            events.extend(self._advance("timeout"))
        return events

    # -- mpv messages -------------------------------------------------------
    def _handle_message(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Translate one mpv IPC message into zero or more player events."""
        event = message.get("event")

        if event == "file-loaded":
            source = self._playing
            self._pending = None
            return [{"kind": "loaded", "channel": self._channel, "source": source}]

        if event == "end-file":
            reason = str(message.get("reason") or "")
            if reason == "error":
                source = self._pending.source if self._pending is not None else None
                self._pending = None
                events = [
                    {
                        "kind": "source-failed",
                        "channel": self._channel,
                        "source": source,
                        "message": "mpv could not open this stream",
                    }
                ]
                events.extend(self._advance("error"))
                return events
            if reason in ("eof", "quit"):
                self._pending = None
                return [
                    {"kind": "finished", "channel": self._channel, "source": self._playing}
                ]
            # "stop"/"redirect" describe the file this player just replaced.
        return []

    def _shutdown_ipc(self) -> None:
        """Forget the IPC connection after mpv went away on its own."""
        connection = self._ipc
        self._ipc = None
        self._pending = None
        self._attempts = None
        with self._lock:
            self._process = None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
        path = self._ipc_path
        self._ipc_path = None
        if path is not None:
            try:
                Path(path).unlink()
            except OSError:
                pass

    # -- spawn fallback -----------------------------------------------------
    def _start_spawn(self, channel: Channel, start_index: int, run_id: int) -> bool:
        """Play one mpv process per channel (the path used without IPC)."""
        thread = threading.Thread(
            target=self._run,
            args=(channel, start_index, run_id),
            name="tdt-player",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def _run(self, channel: Channel, start_index: int, run_id: int) -> None:
        last_status = 1
        for attempt in iter_attempts(channel, start_index):
            if not self._is_current(run_id):
                return
            command = build_mpv_command(
                channel,
                attempt.source,
                extra_args=self.extra_args,
                user_agent=self.user_agent,
                mpv=self.mpv,
            )
            self._emit(
                "started",
                channel=channel,
                source=attempt.source,
                attempt=attempt.number,
                total=min(len(channel.playable_sources()), MAX_ATTEMPTS),
            )
            try:
                process = self._spawn(command, self.log_path)
            except OSError as exc:
                self._emit("error", channel=channel, message=str(exc))
                return

            with self._lock:
                self._process = process
            try:
                last_status = process.wait()
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None

            if not self._is_current(run_id):
                return
            if last_status == 0:
                self._emit("finished", channel=channel, source=attempt.source)
                return
            time.sleep(RETRY_DELAY)

        self._emit(
            "failed",
            channel=channel,
            message=f"mpv exited with status {last_status}",
        )

    # -- helpers ------------------------------------------------------------
    def _emit(self, kind: str, **payload: Any) -> None:
        payload["kind"] = kind
        self._events.put(payload)

    def _is_current(self, run_id: int) -> bool:
        with self._lock:
            return run_id == self._run_id

    @staticmethod
    def _has_exited(process: Any) -> bool:
        try:
            return process.poll() is not None
        except Exception:  # noqa: BLE001 - injected doubles may not implement poll
            return False

    def _wait_for_exit(self, process: Any, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._has_exited(process):
                return
            time.sleep(0.02)

    @staticmethod
    def _terminate(process: Any) -> None:
        for action in ("terminate", "kill"):
            method = getattr(process, action, None)
            if method is None:
                continue
            try:
                method()
                return
            except OSError:
                continue
            except Exception:  # noqa: BLE001
                return

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Player(mpv={self.mpv!r}, ipc={self.use_ipc!r}, "
            f"extra_args={self.extra_args!r})"
        )
