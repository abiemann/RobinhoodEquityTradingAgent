import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import rules_version


SCRIPT = os.path.join(ROOT, "rules_version.py")
COMMITTED_CONSTANTS = """# Constants

| Name | Value | Description |
|---|---|---|
| `DRY_RUN` | `true` | Safe default. |
| `OTHER` | `1` | Other. |
"""


class FakeGit:
    def __init__(
        self,
        *,
        revision="abc1234",
        status="",
        committed=COMMITTED_CONSTANTS,
    ):
        self.revision = revision
        self.status = status
        self.committed = committed
        self.calls = []

    def __call__(self, root, arguments):
        self.calls.append(tuple(arguments))
        if arguments[0] == "log":
            return self.revision + "\n"
        if arguments[0] == "status":
            return self.status
        if arguments[:2] == ("show", "HEAD:constants.md"):
            return self.committed
        raise AssertionError(arguments)


class RulesVersionTests(unittest.TestCase):
    def resolve(
        self,
        *,
        status="",
        current=COMMITTED_CONSTANTS,
        revision="abc1234",
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "constants.md").write_text(current, encoding="utf-8")
            fake = FakeGit(revision=revision, status=status)
            result = rules_version.resolve_rules_version(root, git=fake)
            return result, fake.calls

    def test_clean_or_unrelated_changes_return_clean_rules_hash(self):
        result, calls = self.resolve(status="")
        self.assertEqual(result, "abc1234")
        status_call = next(call for call in calls if call[0] == "status")
        self.assertIn("--porcelain=v1", status_call)
        self.assertIn("--untracked-files=all", status_call)
        self.assertEqual(
            tuple(rules_version.RULE_SET_FILES),
            status_call[-len(rules_version.RULE_SET_FILES) :],
        )

    def test_only_expected_true_to_false_dry_run_edit_is_clean(self):
        current = COMMITTED_CONSTANTS.replace(
            "| `DRY_RUN` | `true` |",
            "| `DRY_RUN` | `false` |",
        )
        result, _calls = self.resolve(
            status=" M constants.md\n", current=current
        )
        self.assertEqual(result, "abc1234")

    def test_other_constants_or_multiple_rule_changes_are_dirty(self):
        other = COMMITTED_CONSTANTS.replace(
            "| `OTHER` | `1` |", "| `OTHER` | `2` |"
        )
        result, _ = self.resolve(status=" M constants.md\n", current=other)
        self.assertEqual(result, "abc1234-dirty")

        staged_live = COMMITTED_CONSTANTS.replace(
            "| `DRY_RUN` | `true` |",
            "| `DRY_RUN` | `false` |",
        )
        result, _ = self.resolve(
            status="M  constants.md\n", current=staged_live
        )
        self.assertEqual(result, "abc1234-dirty")

        live = COMMITTED_CONSTANTS.replace(
            "| `DRY_RUN` | `true` |",
            "| `DRY_RUN` | `false` |",
        )
        result, _ = self.resolve(
            status=" M constants.md\n M run_lifecycle.py\n",
            current=live,
        )
        self.assertEqual(result, "abc1234-dirty")

    def test_untracked_rule_file_is_dirty(self):
        result, _ = self.resolve(status="?? rules_version.py\n")
        self.assertEqual(result, "abc1234-dirty")

    def test_invalid_hash_or_git_failure_returns_unknown(self):
        result, _ = self.resolve(revision="release-14-gabc1234")
        self.assertEqual(result, "unknown")

        def unavailable(_root, _arguments):
            raise rules_version.RulesVersionError("no git")

        self.assertEqual(
            rules_version.resolve_rules_version(Path(ROOT), git=unavailable),
            "unknown",
        )

    def test_cli_emits_one_exact_nonblocking_json_envelope(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        document = json.loads(proc.stdout)
        self.assertEqual(
            set(document),
            {"schema_version", "status", "rules_version"},
        )
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["status"], "valid")
        self.assertRegex(
            document["rules_version"],
            r"^(?:[0-9a-f]{4,40}(?:-dirty)?|unknown)$",
        )


if __name__ == "__main__":
    unittest.main()
