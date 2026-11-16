"""Optional Discord webhook notifications."""
from __future__ import annotations

import logging

from . import http

log = logging.getLogger(__name__)


def discord(webhook_url: str, content: str) -> bool:
    if not webhook_url:
        return False
    r = http.request("POST", webhook_url, json_body={"content": content[:1900]})
    if not r.ok:
        log.warning("discord webhook failed: HTTP %s %s", r.status, r.body)
    return r.ok
