#!/usr/bin/env python3
"""Contract tests for the trusted-base pull-request safety workflow."""

import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WORKFLOW = os.path.join(
    ROOT, ".github", "workflows", "pr-dry-run-safety.yml"
)


class PullRequestSafetyWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW, encoding="utf-8") as handle:
            cls.workflow = handle.read()

    def test_runs_trusted_base_code_for_every_main_pull_request(self):
        workflow = self.workflow
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("branches:\n      - main", workflow)
        for activity in (
            "opened",
            "synchronize",
            "reopened",
            "ready_for_review",
            "edited",
        ):
            self.assertIn(f"      - {activity}", workflow)
        self.assertNotIn("paths:", workflow)
        self.assertNotIn("paths-ignore:", workflow)
        self.assertNotIn("actions/checkout", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("statuses: write", workflow)

    def test_preserves_required_status_context_and_fails_closed(self):
        workflow = self.workflow
        self.assertGreaterEqual(
            workflow.count("safety/committed-dry-run"),
            2,
        )
        self.assertIn('final_state="failure"', workflow)
        self.assertIn('trap finish EXIT', workflow)
        self.assertIn(
            'post_status "pending" "Checking that the PR does not touch constants.md"',
            workflow,
        )
        self.assertIn('final_state="success"', workflow)
        self.assertIn('final_description="PR does not touch constants.md"', workflow)

    def test_checks_the_complete_race_bound_pr_file_list(self):
        workflow = self.workflow
        self.assertIn(
            'pr_url="https://api.github.com/repos/${BASE_REPO}/pulls/${PR_NUMBER}"',
            workflow,
        )
        self.assertIn(
            '"${pr_url}/files?per_page=100&page=${page}"',
            workflow,
        )
        self.assertIn("EXPECTED_CHANGED_FILES > 3000", workflow)
        self.assertIn("observed != EXPECTED_CHANGED_FILES", workflow)
        self.assertIn('initial_head="$(jq -er', workflow)
        self.assertIn('final_head="$(jq -er', workflow)
        self.assertIn('initial_count="$(jq -er', workflow)
        self.assertIn('final_count="$(jq -er', workflow)
        self.assertIn('$initial_head" != "$HEAD_SHA', workflow)
        self.assertIn('$final_head" != "$HEAD_SHA', workflow)

    def test_rejects_every_root_constants_diff_shape(self):
        workflow = self.workflow
        self.assertIn('(.filename == "constants.md")', workflow)
        self.assertIn('(.previous_filename == "constants.md")', workflow)
        self.assertIn(
            "Pull requests may not add, modify, delete, or rename constants.md.",
            workflow,
        )
        self.assertNotIn("/contents/constants.md", workflow)
        self.assertNotIn("HEAD_REPO", workflow)
        self.assertNotIn("DRY_RUN table row", workflow)


if __name__ == "__main__":
    unittest.main()
