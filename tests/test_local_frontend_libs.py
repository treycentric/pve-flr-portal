"""The pages load their JavaScript, stylesheets, and fonts from /static,
never from a CDN.

A script or stylesheet from a third-party host runs/applies with the
page's full access, and the login page is where users type their PVE
password, so a compromised or hijacked CDN could read it. htmx and
Alpine are served from backend/static/vendor/, and Open Sans from
backend/static/fonts/, instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted((ROOT / "backend" / "templates").rglob("*.html"))
STYLESHEETS = sorted((ROOT / "backend" / "static").glob("*.css"))
EXTERNAL_SCRIPT = re.compile(r"<script[^>]*\ssrc=[\"']?(https?:)?//", re.IGNORECASE)
EXTERNAL_LINK = re.compile(r"<link\b[^>]*\shref=[\"'](https?:)?//", re.IGNORECASE)
EXTERNAL_CSS_REF = re.compile(r"(?:url\(|@import\s)\s*[\"']?(https?:)?//", re.IGNORECASE)


def test_no_page_loads_a_script_from_another_host():
    offenders = [
        f"{page.relative_to(ROOT)}: {match.group(0)}"
        for page in PAGES
        for match in EXTERNAL_SCRIPT.finditer(page.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_page_links_to_a_stylesheet_or_font_on_another_host():
    offenders = [
        f"{page.relative_to(ROOT)}: {match.group(0)}"
        for page in PAGES
        for match in EXTERNAL_LINK.finditer(page.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_stylesheet_references_another_host():
    offenders = [
        f"{css.relative_to(ROOT)}: {match.group(0)}"
        for css in STYLESHEETS
        for match in EXTERNAL_CSS_REF.finditer(css.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_vendored_libraries_are_present_and_licensed():
    vendor = ROOT / "backend" / "static" / "vendor"
    for name in ("htmx.min.js", "alpine.min.js", "htmx.LICENSE", "alpine.LICENSE.md"):
        assert (vendor / name).stat().st_size > 0, name


def test_vendored_fonts_are_present_and_licensed():
    fonts = ROOT / "backend" / "static" / "fonts"
    for name in (
        "open-sans-latin-400-normal.woff2",
        "open-sans-latin-600-normal.woff2",
        "open-sans-latin-700-normal.woff2",
        "OFL.txt",
    ):
        assert (fonts / name).stat().st_size > 0, name
