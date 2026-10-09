"""Documentation stays reachable and its local links resolve without network access."""
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("user-guide.md", "reference.md", "architecture.md", "development.md", "operations.md")


class Docs(unittest.TestCase):
    def test_links_and_discoverability(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in DOCS:
            self.assertIn(f"docs/{name}", readme)
            self.assertTrue((ROOT / "docs" / name).is_file(), name)
        files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]
        for source in files:
            text = source.read_text(encoding="utf-8")
            for link in re.findall(r"\]\(([^\s)]+)\)", text):
                url = urlsplit(link)
                if url.scheme or url.netloc:
                    continue
                target = (source.parent / unquote(url.path)).resolve() if url.path else source
                with self.subTest(source=source.name, link=link):
                    self.assertTrue(target.is_relative_to(ROOT))
                    self.assertTrue(target.exists(), f"Missing target: {link}")
                    if url.fragment and target.suffix == ".md":
                        headings = re.findall(r"^#{1,6} (.+)$", target.read_text(encoding="utf-8"), re.M)
                        anchors = [re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in headings]
                        self.assertIn(unquote(url.fragment), anchors)


if __name__ == "__main__":
    unittest.main()
