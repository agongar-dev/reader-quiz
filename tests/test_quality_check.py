import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


quality_check = load_module("quality_check", "scripts/quality_check.py")
plan = load_module("quality_check_plan", "scripts/quality_check_plan.py")


class QualityCheckRunnerTests(unittest.TestCase):
    def test_direct_execution_fails_closed_until_dispatch_is_available(self):
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts/quality_check.py"), "format-check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("operation dispatch is unavailable", completed.stderr)

    def test_repository_policy_targets_use_tracked_paths_from_git_ls_files(self):
        targets = quality_check._repository_policy_targets(
            REPO_ROOT,
            tracked_paths_resolver=lambda _repo_root: [
                "README.md",
                "build/output.elf",
                "scripts/quality_check.py",
            ],
        )

        self.assertEqual(
            targets, ["README.md", "build/output.elf", "scripts/quality_check.py"]
        )

    def test_tracked_paths_decode_git_ls_files_z_output(self):
        self.assertEqual(
            quality_check._decode_git_ls_files_z(
                b"README.md\0docs/contributing/getting-started.md\0"
            ),
            ["README.md", "docs/contributing/getting-started.md"],
        )

    def test_clang_format_check_uses_tracked_file_discovery_and_shared_exclusions(self):
        paths = quality_check._clang_format_paths(
            REPO_ROOT,
            tracked_paths_resolver=lambda _repo_root: [
                "src/main.cpp",
                "lib/EpdFont/EpdFont.h",
                "lib/EpdFont/builtinFonts/generated.h",
                "lib/Epub/Epub/hyphenation/generated/table.cpp",
                "src/generated/header.generated.h",
                "lib/I18n/I18nKeys.h",
                "lib/I18n/I18nStrings.h",
                "lib/I18n/I18nStrings.cpp",
                "docs/readme.md",
            ],
        )

        self.assertEqual(paths, ["lib/EpdFont/EpdFont.h", "src/main.cpp"])



if __name__ == "__main__":
    unittest.main()
