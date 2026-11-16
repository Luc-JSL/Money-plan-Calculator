"""Optional TikTok posting through the MIT-licensed `tiktok-uploader` package
(https://github.com/wkaisertexas/tiktok-uploader), which drives a Playwright
browser with your exported tiktok.com cookies.

This is opt-in. TikTok's terms do not allow automated posting from a normal
account, so accounts used this way can be rate-limited or banned. Use a
dedicated clips account, keep volume human (a handful per day), and prefer
posting by hand for anything you care about.

Install:  pip install tiktok-uploader && playwright install chromium
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import tiktok_uploader  # noqa: F401
        return True
    except ImportError:
        return False


def post_tiktok(video: Path, description: str, cookies: str, headless: bool = True,
                uploader=None) -> bool:
    """Upload one video. Returns True on success. `uploader` is injectable for tests."""
    video = Path(video)
    if not video.is_file():
        log.error("post: %s does not exist", video)
        return False
    if not cookies or not Path(cookies).is_file():
        log.error("post: TIKTOK_COOKIES file not found: %r", cookies)
        return False
    if uploader is None:
        if not available():
            log.error("post: tiktok-uploader not installed (pip install tiktok-uploader; playwright install chromium)")
            return False
        from tiktok_uploader.upload import upload_video  # type: ignore
        uploader = upload_video
    try:
        failed = uploader(str(video), description=description, cookies=cookies, headless=headless,
                          browser="chromium")
    except Exception as e:  # noqa: BLE001
        log.error("post: upload failed for %s: %s", video, e)
        return False
    if failed:
        log.error("post: TikTok rejected %s: %s", video, failed)
        return False
    log.info("post: uploaded %s to TikTok", video.name)
    return True
