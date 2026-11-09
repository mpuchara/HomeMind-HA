"""Check the shipped UI release label and active asset cache keys."""

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest
from urllib.parse import parse_qs, urlsplit


ADDON = Path(__file__).resolve().parents[1] / "adaptive_ai"


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.styles = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.scripts.append(attrs["src"])
        if tag == "link" and "stylesheet" in (attrs.get("rel") or "").split():
            self.styles.append(attrs["href"])


class ReleaseUiVersionTests(unittest.TestCase):
    def test_active_assets_and_release_label_match_addon_version(self):
        config = (ADDON / "config.yaml").read_text(encoding="utf-8")
        version = re.search(r'^version: "([^"]+)"$', config, re.MULTILINE).group(1)
        static = ADDON / "src" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(r"LOCAL HOME INTELLIGENCE · ([0-9.]+)", html), [version]
        )

        parser = AssetParser()
        parser.feed(html)
        self.assertTrue(parser.scripts)
        self.assertTrue(parser.styles)
        for url in parser.scripts + parser.styles:
            with self.subTest(url=url):
                asset = urlsplit(url)
                self.assertFalse(asset.scheme or asset.netloc)
                self.assertTrue((static / asset.path).is_file())
                self.assertEqual(parse_qs(asset.query).get("v"), [version])
