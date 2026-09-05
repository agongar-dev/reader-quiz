from __future__ import annotations

import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC_PATHS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs/contributing/development-workflow.md",
    REPO_ROOT / "docs/contributing/getting-started.md",
    REPO_ROOT / "docs/contributing/testing-debugging.md",
]
DOC_TEXT = "\n".join(path.read_text(encoding="utf-8") for path in DOC_PATHS)


def load_simple_toml(path: pathlib.Path) -> dict[str, object]:
    config: dict[str, object] = {}
    section: dict[str, str] | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1]
            section = {}
            config[section_name] = section
            continue
        key, _, raw_value = line.partition("=")
        value = raw_value.strip().strip('"')
        if section is None:
            config[key.strip()] = value
        else:
            section[key.strip()] = value

    return config


RUFF_CONFIG = load_simple_toml(REPO_ROOT / "ruff.toml")


class QualityDocsTests(unittest.TestCase):
    def test_docs_migrate_from_core_hookspath_to_prek_install(self):
        self.assertIn("git config --unset core.hooksPath", DOC_TEXT)
        self.assertIn(
            "prek install --hook-type pre-commit --hook-type pre-push", DOC_TEXT
        )
        self.assertNotIn("git config core.hooksPath .githooks", DOC_TEXT)

    def test_docs_share_quick_and_complete_quality_commands(self):
        for command in [
            "prek run --all-files",
            "python scripts/quality_check.py complete",
        ]:
            self.assertIn(command, DOC_TEXT)

    def test_docs_explain_local_hooks_are_bypassable_but_ci_enforces_test_status(self):
        self.assertIn("Local hooks can be bypassed", DOC_TEXT)
        self.assertIn("Test Status", DOC_TEXT)
        self.assertIn("protected develop", DOC_TEXT)

    def test_docs_list_quality_tool_prerequisites_and_pinned_install_steps(self):
        for snippet in [
            "cmake",
            "ctest",
            "ninja",
            "requirements-quality.txt",
            "shellcheck-py==0.11.0.1",
            "actionlint v1.7.12",
            "gitleaks v8.30.1",
        ]:
            self.assertIn(snippet, DOC_TEXT)
        self.assertNotIn("apt-get install -y cmake ninja-build shellcheck", DOC_TEXT)

    def test_docs_distinguish_historical_python_38_scripts_from_python_314_ci_parity(
        self,
    ):
        self.assertIn("Python 3.8+", DOC_TEXT)
        self.assertIn("Python 3.14 CI-parity quality environment", DOC_TEXT)

    def test_docs_keep_windows_git_bash_and_device_guidance(self):
        self.assertIn("Git Bash on Windows", DOC_TEXT)
        self.assertIn("USB-C cable for device testing", DOC_TEXT)
        self.assertNotIn("host-only", DOC_TEXT)

    def test_ruff_config_pins_py38_without_global_ignores(self):
        self.assertEqual(RUFF_CONFIG.get("target-version"), "py38")
        self.assertNotIn("ignore", RUFF_CONFIG)
        self.assertNotIn("extend-ignore", RUFF_CONFIG)
        lint_config = RUFF_CONFIG.get("lint", {})
        self.assertIsInstance(lint_config, dict)
        self.assertNotIn("ignore", lint_config)
        self.assertNotIn("extend-ignore", lint_config)


if __name__ == "__main__":
    unittest.main()
