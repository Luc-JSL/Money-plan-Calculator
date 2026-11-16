import asyncio
import unittest

from clipbot import irc
from clipbot.irc import ChatPool, count_emotes, parse_line, parse_tags

LINE = ("@badge-info=;badges=;color=#FF0000;display-name=Chatter;emotes=25:0-4,12-16/1902:6-10;id=abc;"
        "mod=0;room-id=1;subscriber=0;tmi-sent-ts=1700000000000;turbo=0;user-id=42;user-type= "
        ":chatter!chatter@chatter.tmi.twitch.tv PRIVMSG #somestreamer :Kappa Keepo Kappa hello")


class ParseTests(unittest.TestCase):
    def test_privmsg(self):
        m = parse_line(LINE)
        self.assertEqual(m.channel, "somestreamer")
        self.assertEqual(m.user, "chatter")
        self.assertEqual(m.user_id, "42")
        self.assertEqual(m.text, "Kappa Keepo Kappa hello")
        self.assertEqual(m.emote_count, 3)

    def test_action(self):
        m = parse_line(":u!u@u.tmi.twitch.tv PRIVMSG #c :\x01ACTION waves\x01")
        self.assertEqual(m.text, "waves")

    def test_colon_in_message_and_no_tags(self):
        m = parse_line(":u!u@u.tmi.twitch.tv PRIVMSG #c :time is 10:30 :)")
        self.assertEqual(m.text, "time is 10:30 :)")

    def test_non_privmsg_lines(self):
        for line in [":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!", "PING :tmi.twitch.tv",
                     ":u!u@u.tmi.twitch.tv JOIN #c", "@msg-id=slow_on :tmi.twitch.tv NOTICE #c :slow mode", ""]:
            self.assertIsNone(parse_line(line), line)

    def test_tag_unescape(self):
        self.assertEqual(parse_tags(r"system-msg=a\sb\:c;x=")["system-msg"], "a b;c")

    def test_count_emotes(self):
        self.assertEqual(count_emotes(""), 0)
        self.assertEqual(count_emotes("25:0-4"), 1)
        self.assertEqual(count_emotes("25:0-4,6-10/1902:12-16"), 3)


class FakeTMI:
    """Minimal fake Twitch IRC server for integration tests."""

    def __init__(self):
        self.joined: list[str] = []
        self.pongs = 0
        self.writers = []
        self.ready = asyncio.Event()

    async def handle(self, reader, writer):
        self.writers.append(writer)
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode().strip()
            if line.startswith("NICK"):
                writer.write(b":tmi.twitch.tv 001 x :Welcome, GLHF!\r\n")
                await writer.drain()
            elif line.startswith("JOIN"):
                self.joined.append(line.split("#", 1)[1])
                self.ready.set()
            elif line.startswith("PONG"):
                self.pongs += 1
        writer.close()

    async def send(self, line: str):
        for w in self.writers:
            w.write((line + "\r\n").encode())
            await w.drain()


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeTMI()
        self.server = await asyncio.start_server(self.fake.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self._saved = (irc.HOST, irc.PORT, irc.USE_TLS, irc.JOIN_INTERVAL)
        irc.HOST, irc.PORT, irc.USE_TLS, irc.JOIN_INTERVAL = "127.0.0.1", port, False, 0.0

    async def asyncTearDown(self):
        irc.HOST, irc.PORT, irc.USE_TLS, irc.JOIN_INTERVAL = self._saved
        for w in self.fake.writers:
            w.close()
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), 5)

    async def test_join_ping_and_message_delivery(self):
        pool = ChatPool()
        await pool.set_channels({"StreamerA", "#streamerb"})
        await asyncio.wait_for(self.fake.ready.wait(), 5)
        await asyncio.sleep(0.2)
        self.assertEqual(sorted(self.fake.joined), ["streamera", "streamerb"])
        await self.fake.send("PING :tmi.twitch.tv")
        await self.fake.send("@user-id=7;emotes= :bob!bob@bob.tmi.twitch.tv PRIVMSG #streamera :LUL no way")
        msg = await asyncio.wait_for(pool.queue.get(), 5)
        self.assertEqual((msg.channel, msg.user, msg.text), ("streamera", "bob", "LUL no way"))
        await asyncio.sleep(0.1)
        self.assertEqual(self.fake.pongs, 1)
        joined, parted = await pool.set_channels({"streamera"})
        self.assertEqual((joined, parted), (set(), {"streamerb"}))
        self.assertEqual(pool.channels, {"streamera"})
        await pool.close()

    async def test_spreads_over_connections(self):
        pool = ChatPool(max_per_conn=2)
        await pool.set_channels({"a", "b", "c"})
        await asyncio.sleep(0.3)
        self.assertEqual(len(pool.conns), 2)
        self.assertEqual(sorted(self.fake.joined), ["a", "b", "c"])
        await pool.close()


if __name__ == "__main__":
    unittest.main()
