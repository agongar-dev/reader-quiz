import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_WORKFLOW = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
QUALITY_REQUIREMENTS = (REPO_ROOT / "requirements-quality.txt").read_text(
    encoding="utf-8"
)


class CiWorkflowTests(unittest.TestCase):
    def test_ci_triggers_permissions_and_concurrency_match_contract(self):
        self.assertRegex(
            CI_WORKFLOW,
            r"(?ms)^on:\n  push:\n    branches:\s*\[develop\]\n  pull_request:\s*\n  workflow_dispatch:\s*$",
        )
        self.assertRegex(CI_WORKFLOW, r"(?m)^permissions:\n(?:  [a-z-]+: read\n)+")
        self.assertRegex(
            CI_WORKFLOW,
            r"(?ms)^concurrency:\n  group: .*\n  cancel-in-progress: true\s*$",
        )

    def test_ci_pins_immutable_actions_and_tool_versions(self):
        pinned_actions = {
            "actions/checkout": "d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
            "actions/setup-go": "924ae3a1cded613372ab5595356fb5720e22ba16",
            "actions/cache": "0057852bfaa89a56745cba8c7296529d2fc39830",
        }
        for action, sha in pinned_actions.items():
            self.assertIn(f"uses: {action}@{sha} # ", CI_WORKFLOW)

        for snippet in [
            "python-version: '3.14'",
            "go-version: '1.25.5'",
            "v6.1.19.zip",
            "go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12",
            "go install github.com/zricethezav/gitleaks/v8@v8.30.1",
        ]:
            self.assertIn(snippet, CI_WORKFLOW)
        self.assertNotIn("sudo apt-get install -y ninja-build shellcheck", CI_WORKFLOW)
        self.assertIn("sudo apt-get install -y ninja-build", CI_WORKFLOW)
        self.assertIn("shellcheck-py==0.11.0.1", QUALITY_REQUIREMENTS)

        for snippet in [
            "prek==0.4.14",
            "ruff==0.16.0",
            "clang-format==21.1.8",
            "yamllint==1.38.0",
        ]:
            self.assertIn(snippet, QUALITY_REQUIREMENTS)

    def test_ci_uses_dependency_only_caches_and_shared_quality_command(self):
        for cache_path in ["~/.platformio", "~/.cache/pip", ".cache/cmake-fetch"]:
            self.assertIn(f"path: {cache_path}", CI_WORKFLOW)
        self.assertNotIn("path: build/test/_deps/googletest-src", CI_WORKFLOW)
        self.assertNotIn("path: .pio", CI_WORKFLOW)
        self.assertIn("python scripts/quality_check.py complete", CI_WORKFLOW)
        self.assertRegex(
            CI_WORKFLOW,
            r"(?ms)^\s+quality:\n.*?python scripts/quality_check\.py complete",
        )

    def test_ci_keeps_stable_test_status_aggregate(self):
        self.assertRegex(CI_WORKFLOW, r"(?ms)^\s+test-status:\n\s+name: Test Status\n")
        self.assertIn("if: always()", CI_WORKFLOW)
        self.assertIn(
            "contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')",
            CI_WORKFLOW,
        )


if __name__ == "__main__":
    unittest.main()
