"""scripts/audit.py: what must never go public is caught, and this repository passes today."""

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "audit.py"
_spec = importlib.util.spec_from_file_location("audit", _PATH)
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit"] = audit
_spec.loader.exec_module(audit)

# Built from pieces so this file never holds a credential-shaped string itself.
FAKE_ANTHROPIC = "sk-" + "ant-" + "api03-" + "A" * 40
FAKE_GITHUB = "gh" + "p_" + "B" * 36
FAKE_CARD = "5555 5555 " + "5555 4444"  # Luhn-valid, not an Airwallex test card
OUTSIDE_EMAIL = "someone@" + "gmail.com"


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.com", "HOME": str(root), "PATH": "/usr/bin:/bin"})


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        git(self.root, "init", "-q")
        (self.root / ".gitignore").write_text(".env\n")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, files, message="c"):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def fails(self):
        findings, _ = audit.audit(self.root)
        return [f for f in findings if f.level == "FAIL"]

    def test_a_clean_tree_passes(self):
        self.commit({"README.md": "See [the code](src/a.py).\n", "src/a.py": "x = 1\n",
                     ".env.example": "ANTHROPIC_API_KEY=\nCOUNTERSIGN_ACP_HMAC_SECRET=\n"})
        self.assertEqual([f for f in self.fails() if "evidence" not in f.what], [])

    def test_credentials_and_card_numbers_fail_without_being_printed(self):
        self.commit({"src/config.py": f'KEY = "{FAKE_ANTHROPIC}"\n',
                     "deploy.sh": "export COUNTERSIGN_LEVER_TOKEN=abcdefghijklmnopqrstuvwxyz123456\n",
                     "notes.md": f"card {FAKE_CARD} and the Airwallex test card 4035 5010 0000 0008\n"})
        fails = [f for f in self.fails() if "evidence" not in f.what]
        whats = " ".join(f.what for f in fails)
        self.assertIn("Anthropic API key", whats)
        self.assertIn("COUNTERSIGN_LEVER_TOKEN has a value", whats)
        self.assertIn("card-like number (555555…)", whats)
        self.assertEqual(sum("card-like" in f.what for f in fails), 1)  # the test card is allowed
        self.assertNotIn(FAKE_ANTHROPIC, whats)
        self.assertNotIn("abcdefghijklmnop", whats)

    def test_a_tracked_env_file_fails(self):
        (self.root / ".gitignore").write_text("")
        self.commit({".env": "X=1\n"})
        whats = [f.what for f in self.fails()]
        self.assertIn(".env is tracked", whats)
        self.assertIn(".env is not in .gitignore", whats)

    def test_a_key_removed_later_is_still_found_in_history(self):
        self.commit({"src/a.py": f'TOKEN = "{FAKE_GITHUB}"\n'})
        self.commit({"src/a.py": "TOKEN = None\n"}, "remove it")
        fails = self.fails()
        self.assertTrue(any(f.what.startswith("GitHub token in history") for f in fails), fails)

    def test_broken_links_fail_and_outside_emails_warn(self):
        self.commit({"README.md": f"Read [the plan](docs/PLAN.md) or write to {OUTSIDE_EMAIL}.\n"})
        findings, _ = audit.audit(self.root)
        self.assertIn(("FAIL", "link to a missing file: docs/PLAN.md"), [(f.level, f.what) for f in findings])
        self.assertIn(("WARN", "e-mail address at gmail.com"), [(f.level, f.what) for f in findings])

    def test_luhn(self):
        self.assertTrue(audit.luhn("4035501000000008"))
        self.assertFalse(audit.luhn("4035501000000009"))


class ThisRepositoryTest(unittest.TestCase):
    def test_nothing_here_must_be_fixed_before_going_public(self):
        root = _PATH.parent.parent
        if not (root / ".git").exists():
            self.skipTest("not a git checkout")
        findings, _ = audit.audit(root)
        self.assertEqual([f for f in findings if f.level == "FAIL"], [])


if __name__ == "__main__":
    unittest.main()
