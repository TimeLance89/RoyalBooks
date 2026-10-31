"""Static UI contract: new controls must remain connected to their JavaScript."""
import re
from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "royalbooks" / "static" / "index.html"


class DOMIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.duplicates = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        name = values.get("id")
        if name:
            if name in self.ids:
                self.duplicates.add(name)
            self.ids.add(name)


def test_every_js_dom_identifier_exists_exactly_once():
    text = PAGE.read_text(encoding="utf-8")
    page = DOMIds()
    page.feed(text)
    scripts = text.split("<script>", 1)[1].split("</script>", 1)[0]
    identifiers = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", scripts))
    assert not page.duplicates
    assert identifiers <= page.ids, f"Missing DOM controls: {identifiers - page.ids}"


def test_mobile_and_accessible_core_controls():
    source = PAGE.read_text(encoding="utf-8")
    assert 'name="viewport"' in source
    assert "@media(max-width:700px)" in source
    for expected in ("search","watchlist","catalog-controls","download-center",
                     "sync-csv","sync-rdf","import-status","job-rows","preflight-results"):
        assert f'id="{expected}"' in source
    assert "aria-live" in source
    assert "textContent=" in source
    # Never make the API token persistent in browser storage by default.
    assert "sessionStorage" not in source
    stored_keys = re.findall(r"localStorage\.setItem\('([^']+)'", source)
    assert set(stored_keys) == {'royalbooks-layout', 'royalbooks-theme', 'royalbooks-store-v2', 'royalbooks-kiosk'}
