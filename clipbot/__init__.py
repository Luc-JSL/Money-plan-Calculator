"""clipbot: automatic Twitch clipper driven by chat hype detection.

Pipeline: discover live streams for a game -> read their chat anonymously
-> detect message-rate spikes with hype signals -> create a Twitch clip via
the Helix API -> download it -> optionally render a 9:16 vertical version
-> notify a Discord webhook. See README-clipbot.md for setup.
"""

__version__ = "1.0.0"
