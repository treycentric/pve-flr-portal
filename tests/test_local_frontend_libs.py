"""The pages load their JavaScript from /static, never from a CDN.

A script from a third-party host runs with the page's full access, and
the login page is where users type their PVE password, so a compromised
or hijacked CDN could read it. htmx and Alpine are served from
backend/static/vendor/ instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = [
    *sorted((ROOT / "backend" / "templates").rglob("*.html")),
    ROOT / "backend" / "static" / "timeline-preview.html",
]
EXTERNAL_SCRIPT = re.compile(r"<script[^>]*\ssrc=[\"']?(https?:)?//", re.IGNORECASE)


def test_no_page_loads_a_script_from_another_host():
    offenders = [
        f"{page.relative_to(ROOT)}: {match.group(0)}"
        for page in PAGES
        for match in EXTERNAL_SCRIPT.finditer(page.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_vendored_libraries_are_present_and_licensed():
    vendor = ROOT / "backend" / "static" / "vendor"
    for name in ("htmx.min.js", "alpine.min.js", "htmx.LICENSE", "alpine.LICENSE.md"):
        assert (vendor / name).stat().st_size > 0, name
