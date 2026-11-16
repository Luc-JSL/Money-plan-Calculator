"""Tiny JSON HTTP helper on top of urllib (no third-party dependency).

Everything that talks to the network goes through `request`, so tests can
monkeypatch a single function.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

USER_AGENT = "clipbot/1.0 (+https://github.com)"


@dataclass
class Response:
    status: int
    body: Any          # parsed JSON, or raw text when not JSON
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Response:
    """Perform an HTTP request and return a Response. Never raises on HTTP errors."""
    if params:
        # Lists become repeated keys (Helix style: ?id=a&id=b).
        flat: list[tuple[str, str]] = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                flat.extend((k, str(x)) for x in v)
            else:
                flat.append((k, str(v)))
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(flat)}"

    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            rh = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        rh = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}

    text = raw.decode("utf-8", errors="replace") if raw else ""
    body: Any = text
    if text and ("json" in rh.get("content-type", "") or text[:1] in "{["):
        try:
            body = json.loads(text)
        except ValueError:
            body = text
    return Response(status=status, body=body, headers=rh)
