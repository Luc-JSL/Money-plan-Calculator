"""Anonymous Twitch IRC (TMI) client built on asyncio. Read-only.

Twitch lets anyone read chat with a `justinfan<digits>` nickname and no
token. We request the tags capability to get emote counts and user ids.
"""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

log = logging.getLogger(__name__)

HOST = "irc.chat.twitch.tv"
PORT = 6697
JOIN_INTERVAL = 0.6          # Twitch allows 20 JOINs / 10s for normal users
MAX_CHANNELS_PER_CONN = 50
USE_TLS = True               # tests point this at a plain local server


@dataclass
class ChatMessage:
    channel: str            # without '#'
    user: str               # login
    user_id: str
    text: str
    emote_count: int
    ts: float = field(default_factory=time.time)
    tags: dict[str, str] = field(default_factory=dict)


def parse_tags(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in raw.split(";"):
        if not part:
            continue
        k, _, v = part.partition("=")
        tags[k] = v.replace(r"\s", " ").replace(r"\:", ";").replace(r"\\", "\\")
    return tags


def count_emotes(emotes_tag: str) -> int:
    """emotes tag looks like '25:0-4,12-16/1902:6-10' -> 3 emote occurrences."""
    if not emotes_tag:
        return 0
    n = 0
    for group in emotes_tag.split("/"):
        _, _, positions = group.partition(":")
        if positions:
            n += len(positions.split(","))
    return n


def parse_line(line: str) -> ChatMessage | None:
    """Parse one IRC line; returns a ChatMessage for PRIVMSG, else None."""
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        tags = parse_tags(raw_tags)
    if not rest.startswith(":"):
        return None
    prefix, _, rest = rest[1:].partition(" ")
    if not rest.startswith("PRIVMSG "):
        return None
    rest = rest[len("PRIVMSG "):]
    channel, _, text = rest.partition(" :")
    channel = channel.strip().lstrip("#").lower()
    user = prefix.split("!", 1)[0].lower()
    if tags.get("display-name") and not user:
        user = tags["display-name"].lower()
    text = text.rstrip("\r\n")
    # ACTION (/me) messages are wrapped in \x01ACTION ...\x01
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    return ChatMessage(
        channel=channel, user=user, user_id=tags.get("user-id", ""), text=text,
        emote_count=count_emotes(tags.get("emotes", "")), tags=tags,
    )


class IRCConnection:
    """One TLS connection to TMI that can hold up to MAX_CHANNELS_PER_CONN channels."""

    def __init__(self, queue: "asyncio.Queue[ChatMessage]", name: str = "irc"):
        self.queue = queue
        self.name = name
        self.channels: set[str] = set()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._join_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self.closed = False

    # ----- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"{self.name}-loop")

    async def close(self) -> None:
        self.closed = True
        if self._task:
            self._task.cancel()
        await self._disconnect()

    async def _disconnect(self) -> None:
        self._connected.clear()
        w, self._writer, self._reader = self._writer, None, None
        if w:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass

    async def _connect(self) -> None:
        ctx = ssl.create_default_context() if USE_TLS else None
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, PORT, ssl=ctx), timeout=20)
        nick = f"justinfan{random.randint(10000, 99999)}"
        await self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        await self._send("PASS SCHMOOPIIE")
        await self._send(f"NICK {nick}")
        self._connected.set()
        log.info("[%s] connected as %s", self.name, nick)
        for ch in sorted(self.channels):
            await self._send(f"JOIN #{ch}")
            await asyncio.sleep(JOIN_INTERVAL)

    async def _send(self, line: str) -> None:
        if not self._writer:
            raise ConnectionError("not connected")
        self._writer.write((line + "\r\n").encode("utf-8"))
        await self._writer.drain()

    async def _run(self) -> None:
        backoff = 1.0
        while not self.closed:
            try:
                await self._connect()
                backoff = 1.0
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any network error -> reconnect
                log.warning("[%s] connection error: %s; reconnecting in %.0fs", self.name, e, backoff)
            await self._disconnect()
            if self.closed:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            raw = await asyncio.wait_for(self._reader.readline(), timeout=360)  # TMI pings every ~5 min
            if not raw:
                raise ConnectionError("server closed connection")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("PING"):
                await self._send("PONG :tmi.twitch.tv")
                continue
            if " RECONNECT" in line:
                raise ConnectionError("server requested reconnect")
            msg = parse_line(line)
            if msg is not None:
                self.queue.put_nowait(msg)

    # ----- channel management --------------------------------------------
    async def join(self, channel: str) -> None:
        channel = channel.lower().lstrip("#")
        if channel in self.channels:
            return
        self.channels.add(channel)
        if self._connected.is_set():
            async with self._join_lock:
                try:
                    await self._send(f"JOIN #{channel}")
                except Exception as e:  # reconnect loop will re-join from self.channels
                    log.debug("join %s deferred: %s", channel, e)
                await asyncio.sleep(JOIN_INTERVAL)

    async def part(self, channel: str) -> None:
        channel = channel.lower().lstrip("#")
        if channel not in self.channels:
            return
        self.channels.discard(channel)
        if self._connected.is_set():
            try:
                await self._send(f"PART #{channel}")
            except Exception:
                pass


class ChatPool:
    """Spreads channels over several IRCConnections and exposes one message stream."""

    def __init__(self, max_per_conn: int = MAX_CHANNELS_PER_CONN):
        self.queue: asyncio.Queue[ChatMessage] = asyncio.Queue()
        self.conns: list[IRCConnection] = []
        self.max_per_conn = max_per_conn

    @property
    def channels(self) -> set[str]:
        return set().union(*(c.channels for c in self.conns)) if self.conns else set()

    async def set_channels(self, wanted: set[str]) -> tuple[set[str], set[str]]:
        wanted = {w.lower().lstrip("#") for w in wanted}
        current = self.channels
        to_part, to_join = current - wanted, wanted - current
        for ch in to_part:
            for c in self.conns:
                if ch in c.channels:
                    await c.part(ch)
        for ch in sorted(to_join):
            conn = next((c for c in self.conns if len(c.channels) < self.max_per_conn), None)
            if conn is None:
                conn = IRCConnection(self.queue, name=f"irc{len(self.conns) + 1}")
                self.conns.append(conn)
                await conn.start()
            await conn.join(ch)
        if to_part or to_join:
            log.info("chat: joined %s, parted %s (now %d channels)", sorted(to_join), sorted(to_part), len(wanted))
        return to_join, to_part

    async def messages(self) -> AsyncIterator[ChatMessage]:
        while True:
            yield await self.queue.get()

    async def close(self) -> None:
        for c in self.conns:
            await c.close()
