import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentor.extract import extract_items


class TestCheckboxMode(unittest.TestCase):
    def test_unchecked_item_extracted(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            f = root / "backlog.md"
            f.write_text("- [ ] Fix crash on startup\n  Repro: tap empty slot.\n")
            items = extract_items(f, "checkbox", root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].title, "Fix crash on startup")
            self.assertEqual(items[0].body, "Repro: tap empty slot.")
            self.assertEqual(items[0].source_file, "backlog.md")
            self.assertEqual(items[0].source_line, 1)

    def test_checked_item_skipped(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            f = root / "backlog.md"
            f.write_text("- [x] done thing\n- [ ] pending thing\n")
            items = extract_items(f, "checkbox", root)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].title, "pending thing")

    def test_tags_parsed(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            f = root / "backlog.md"
            f.write_text("- [ ] Fix crash @priority:high @type:bug\n  more detail\n")
            items = extract_items(f, "checkbox", root)
            self.assertEqual(items[0].title, "Fix crash")
            self.assertEqual(items[0].tags, {"priority": "high", "type": "bug"})

    def test_stable_id(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            f = root / "backlog.md"
            f.write_text("- [ ] A thing\n- [ ] Another thing\n")
            items1 = extract_items(f, "checkbox", root)
            items2 = extract_items(f, "checkbox", root)
            self.assertEqual(items1[0].id, items2[0].id)
            self.assertNotEqual(items1[0].id, items1[1].id)


class TestHeadingMode(unittest.TestCase):
    def test_heading_items(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            f = root / "ideas.md"
            f.write_text(
                "# Ideas\n"
                "\n"
                "## Add undo button\n"
                "Sits next to redo.\n"
                "\n"
                "## Dark mode\n"
                "Follow system.\n"
            )
            items = extract_items(f, "heading", root)
            titles = [i.title for i in items]
            self.assertIn("Add undo button", titles)
            self.assertIn("Dark mode", titles)
            undo = next(i for i in items if i.title == "Add undo button")
            self.assertEqual(undo.body, "Sits next to redo.")

    def test_heading_body_stops_at_same_level(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            f = root / "ideas.md"
            f.write_text(
                "## First\n"
                "### subsection included\n"
                "body\n"
                "## Second\n"
                "other\n"
            )
            items = extract_items(f, "heading", root)
            first = next(i for i in items if i.title == "First")
            self.assertIn("subsection included", first.body)
            self.assertNotIn("Second", first.body)


if __name__ == "__main__":
    unittest.main()
