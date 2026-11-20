"""Web console assets.

The console is deliberately build-free: HTML, CSS and vanilla JavaScript are
served as static package files.  Keeping markup out of Python strings makes
the UX easier to inspect and avoids a front-end build chain in a small gateway.
"""

from pathlib import Path

WEB_DIR = Path(__file__).with_name("web")


def admin_html() -> str:
    return (WEB_DIR / "admin.html").read_text(encoding="utf-8")


# Kept for callers that imported the historical constant.
ADMIN_HTML = admin_html()
