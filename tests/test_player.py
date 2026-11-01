"""Tests for :mod:`tdt_player.player`: command building, fallback, threading.

No test starts a real mpv: ``spawn`` is injected and ``subprocess.call`` is
monkeypatched, so what is verified is the command we build and the mirror
fallback logic, not whether a stream happens to be alive today.
"""

from __future__ import annotations

import threading
import time

import pytest

from tdt_player import player as playback
from tdt_player.models import Channel, Source

LOG = "/tmp/tdt-player-test.log"


def channel_with(*urls: str) -> Channel:
    return Channel(name="La 1", sources=[Source(url=url) for url in urls])


def test_command_contains_the_expected_flags():
    channel = Channel(
        name="La 1",
        sources=[
            Source(
                url="https://example.test/la1.m3u8",
                referrer="https://player.test/",
                user_agent="Agent/1.0",
            )
        ],
    )
    command = playback.build_mpv_command(channel)

    assert command[0] == "mpv"
    assert "--force-window=yes" in command
    assert "--title=La 1" in command
    assert "--user-agent=Agent/1.0" in command
    assert "--referrer=https://player.test/" in command
    assert command[-1] == "https://example.test/la1.m3u8"


def test_command_uses_the_default_agent_and_skips_absent_referrer():
    command = playback.build_mpv_command(channel_with("https://example.test/x"))
    assert f"--user-agent={playback.DEFAULT_STREAM_USER_AGENT}" in command
    assert not any(part.startswith("--referrer=") for part in command)


def test_extra_arguments_are_applied_to_the_url():
    command = playback.build_mpv_command(
        channel_with("https://example.test/x"), extra_args=["--mute=yes", "--cache=no"]
    )
    assert command.index("--mute=yes") < command.index("https://example.test/x")
    assert command.index("--cache=no") < command.index("https://example.test/x")
    assert command[-1] == "https://example.test/x"


def test_command_requires_a_source():
    with pytest.raises(ValueError):
        playback.build_mpv_command(Channel(name="Empty"))


def test_iter_attempts_wraps_and_is_capped():
    channel = channel_with(*[f"https://example.test/{index}" for index in range(5)])
    attempts = list(playback.iter_attempts(channel, start_index=4))
    assert [attempt.index for attempt in attempts] == [4, 0, 1]
    assert [attempt.number for attempt in attempts] == [1, 2, 3]


def test_iter_attempts_yields_nothing_without_sources():
    assert list(playback.iter_attempts(Channel(name="Empty"))) == []


def test_run_blocking_tries_the_next_mirror(monkeypatch):
    played = []
    statuses = [1, 0]

    def fake_call(command):
        played.append(command[-1])
        return statuses.pop(0)

    monkeypatch.setattr(playback.subprocess, "call", fake_call)
    channel = channel_with("https://example.test/dead", "https://example.test/alive")

    assert playback.run_blocking(channel) == 0
    assert played == ["https://example.test/dead", "https://example.test/alive"]


class FakeProcess:
    """Stand-in for ``subprocess.Popen``; returns a scripted exit status."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self.status

    def poll(self):
        return self.status

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class BlockingProcess(FakeProcess):
    """Exits only once ``terminate()`` is called - used to test ``stop()``."""

    def __init__(self) -> None:
        super().__init__(status=0)
        self.released = threading.Event()

    def wait(self, timeout=None):
        self.released.wait(5)
        return 1 if self.terminated else 0

    def poll(self):
        return self.status if self.terminated else None

    def terminate(self):
        self.terminated = True
        self.released.set()


def test_player_falls_back_to_the_next_mirror(monkeypatch):
    monkeypatch.setattr(playback, "RETRY_DELAY", 0)
    spawned = []
    statuses = [1, 0]

    def spawn(command, log_path):
        spawned.append(command[-1])
        return FakeProcess(statuses.pop(0))

    player = playback.Player(spawn=spawn, log_path=LOG)
    channel = channel_with("https://example.test/dead", "https://example.test/alive")

    assert player.play(channel) is True
    assert player.wait(5) is True

    events = player.drain()
    assert [event["kind"] for event in events] == ["started", "started", "finished"]
    assert spawned == ["https://example.test/dead", "https://example.test/alive"]


def test_player_reports_failure_after_every_mirror(monkeypatch):
    monkeypatch.setattr(playback, "RETRY_DELAY", 0)
    player = playback.Player(spawn=lambda command, log: FakeProcess(1), log_path=LOG)

    assert player.play(channel_with("https://example.test/a", "https://example.test/b")) is True
    assert player.wait(5) is True

    events = player.drain()
    assert [event["kind"] for event in events] == ["started", "started", "failed"]
    assert "status 1" in events[-1]["message"]


def test_player_missing_binary_is_reported():
    def spawn(command, log_path):
        raise FileNotFoundError("mpv: command not found")

    player = playback.Player(spawn=spawn, log_path=LOG)
    assert player.play(channel_with("https://example.test/a")) is True
    assert player.wait(5) is True

    events = player.drain()
    assert events[-1]["kind"] == "error"
    assert "not found" in events[-1]["message"]


def test_player_plays_nothing_without_sources():
    player = playback.Player(spawn=lambda command, log: FakeProcess(0), log_path=LOG)
    assert player.play(Channel(name="Empty")) is False


def test_stop_terminates_the_running_process():
    process = BlockingProcess()
    player = playback.Player(spawn=lambda command, log: process, log_path=LOG)

    assert player.play(channel_with("https://example.test/a")) is True

    deadline = time.monotonic() + 5
    while not player.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert player.running is True

    player.stop()

    assert process.terminated is True
    assert player.wait(5) is True
    # A stopped run reports nothing: not a success and not a failure.
    assert [event["kind"] for event in player.drain()] == ["started"]


def test_mpv_available_reports_a_missing_binary():
    assert playback.mpv_available("tdt-definitely-not-installed") is None


def test_default_log_path_honours_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert playback.default_log_path() == tmp_path / "tdt_player" / "mpv.log"


class LiveProcess(FakeProcess):
    """An mpv that keeps running: ``poll()`` stays ``None``."""

    def poll(self):
        return None


class FakeIPC:
    """Records commands and replays scripted mpv messages."""

    def __init__(self, *, fail_connect: bool = False) -> None:
        self.commands = []
        self.messages = []
        self.connected = False
        self.closed = False
        self.fail_connect = fail_connect

    def connect(self):
        if self.fail_connect:
            raise OSError("control socket never appeared")
        self.connected = True

    def command(self, *args):
        self.commands.append(tuple(args))
        return len(self.commands)

    def drain(self):
        messages, self.messages = self.messages, []
        return messages

    def close(self):
        self.closed = True


def ipc_player(*, load_timeout: float = 5.0, fail_connect: bool = False):
    ipc = FakeIPC(fail_connect=fail_connect)
    spawned = []

    def spawn(command, log_path):
        spawned.append(list(command))
        return LiveProcess(0)

    player = playback.Player(
        spawn=spawn,
        ipc_factory=lambda path: ipc,
        log_path=LOG,
        load_timeout=load_timeout,
    )
    return player, ipc, spawned


def test_ipc_play_starts_mpv_with_a_socket_and_loads_the_url():
    player, ipc, spawned = ipc_player()
    channel = channel_with("https://example.test/a")

    assert player.play(channel) is True

    assert ipc.connected is True
    assert player.ipc_active is True
    assert any(
        str(argument).startswith("--input-ipc-server=") for argument in spawned[0]
    )
    assert "--idle=yes" in spawned[0]
    assert ("set_property", "title", "La 1") in ipc.commands
    assert ("loadfile", "https://example.test/a", "replace") in ipc.commands
    assert [event["kind"] for event in player.drain()] == ["started"]


def test_ipc_file_loaded_means_it_is_really_playing():
    player, ipc, _ = ipc_player()
    player.play(channel_with("https://example.test/a"))
    player.drain()

    ipc.messages.append({"event": "file-loaded"})

    assert [event["kind"] for event in player.drain()] == ["loaded"]


def test_ipc_error_tries_the_next_mirror_in_the_same_player():
    player, ipc, spawned = ipc_player()
    channel = channel_with("https://example.test/dead", "https://example.test/alive")
    player.play(channel)
    player.drain()

    ipc.messages.append({"event": "end-file", "reason": "error"})
    events = player.drain()

    assert [event["kind"] for event in events] == ["source-failed", "started"]
    assert ("loadfile", "https://example.test/alive", "replace") in ipc.commands
    assert len(spawned) == 1  # the same mpv process is reused


def test_ipc_end_of_file_finishes_and_errors_after_the_last_mirror():
    player, ipc, _ = ipc_player()
    player.play(channel_with("https://example.test/a"))
    player.drain()
    ipc.messages.append({"event": "end-file", "reason": "eof"})
    assert [event["kind"] for event in player.drain()] == ["finished"]

    player.play(channel_with("https://example.test/only"))
    player.drain()
    ipc.messages.append({"event": "end-file", "reason": "error"})
    events = player.drain()
    assert [event["kind"] for event in events] == ["source-failed", "failed"]


def test_ipc_replacing_a_file_is_not_a_finish():
    player, ipc, _ = ipc_player()
    player.play(channel_with("https://example.test/a"))
    player.drain()

    ipc.messages.append({"event": "end-file", "reason": "stop"})

    assert player.drain() == []


def test_switching_channels_reuses_the_same_mpv():
    player, ipc, spawned = ipc_player()
    player.play(channel_with("https://example.test/a"))
    player.play(channel_with("https://example.test/b"))

    assert len(spawned) == 1  # one mpv for both channels
    assert ("loadfile", "https://example.test/b", "replace") in ipc.commands


def test_ipc_timeout_moves_on_to_the_next_source():
    player, ipc, _ = ipc_player(load_timeout=0.0)
    channel = channel_with("https://example.test/dead", "https://example.test/alive")

    player.play(channel)
    events = player.drain()

    assert [event["kind"] for event in events] == [
        "started",
        "source-failed",
        "started",
    ]
    assert ("loadfile", "https://example.test/alive", "replace") in ipc.commands


def test_ipc_sets_the_headers_a_playlist_asked_for():
    player, ipc, _ = ipc_player()
    channel = Channel(
        name="12tv",
        sources=[Source(url="https://example.test/x", referrer="https://ref.test/")],
    )

    player.play(channel)

    assert ("set_property", "referrer", "https://ref.test/") in ipc.commands
    assert any(command[:2] == ("set_property", "user-agent") for command in ipc.commands)


def test_ipc_unavailable_falls_back_to_one_player_per_channel():
    spawned = []

    def spawn(command, log_path):
        spawned.append(list(command))
        return FakeProcess(0)

    player = playback.Player(
        spawn=spawn,
        ipc_factory=lambda path: FakeIPC(fail_connect=True),
        log_path=LOG,
        use_ipc=True,
    )
    channel = channel_with("https://example.test/a")

    assert player.play(channel) is True
    assert player.use_ipc is False
    assert player.wait(5) is True

    kinds = [event["kind"] for event in player.drain()]
    assert kinds[0] == "ipc-unavailable"
    assert "started" in kinds
    assert any(command[-1] == "https://example.test/a" for command in spawned)


def test_stop_sends_quit_and_closes_the_socket():
    player, ipc, _ = ipc_player()
    player.play(channel_with("https://example.test/a"))
    player.drain()  # consume the "started" event

    player.stop()

    assert ("quit",) in ipc.commands
    assert ipc.closed is True
    assert player.ipc_active is False
    assert player.drain() == []


def test_ipc_mpv_closing_behind_our_back_is_reported():
    player, _ipc, _ = ipc_player()
    player.play(channel_with("https://example.test/a"))
    player.drain()

    player._process = FakeProcess(1)  # mpv exited on its own

    events = player.drain()

    assert [event["kind"] for event in events] == ["finished"]
    assert player.ipc_active is False
