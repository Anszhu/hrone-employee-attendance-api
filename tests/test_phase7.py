"""Phase 7 repository-hygiene regression tests. Standard library only: no MongoDB, no third-party packages.
Run:  python -m unittest tests.test_phase7 -v"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class GitignoreTests(unittest.TestCase):
    def test_gitignore_has_no_byte_order_mark(self):
        # A UTF-8 BOM glues itself to the first pattern ("\ufeff.venv/"), so a local virtualenv was not ignored.
        self.assertFalse((ROOT / ".gitignore").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_gitignore_covers_secrets_and_local_artifacts(self):
        patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for needed in (".venv/", "venv/", "__pycache__/", ".env"):
            self.assertIn(needed, patterns)


class DocumentationReferenceTests(unittest.TestCase):
    def test_markdown_does_not_reference_files_the_repository_lacks(self):
        for name in ("PHASE_NOTES.md", "PROBLEM_STATEMENT.docx", "sample_seed.py"):
            if (ROOT / name).exists():
                continue
            for md in ROOT.glob("*.md"):
                self.assertNotIn(name, md.read_text(encoding="utf-8"), f"{md.name} mentions missing {name}")

    def test_documented_sample_data_files_exist(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for rel in re.findall(r"`(sample_data/[A-Za-z0-9_.]+)`", readme):
            self.assertTrue((ROOT / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
