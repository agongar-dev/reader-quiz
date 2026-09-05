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

    def test_python_tests_include_repository_quality_and_optional_ope_modules_when_present(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            (root / "tools/quiz/tests").mkdir(parents=True)
            (root / "tools/quiz/tests/test_convert_quiz.py").write_text(
                "", encoding="utf-8"
            )
            (root / "tests").mkdir(parents=True)
            (root / "tests/test_quality_check.py").write_text("", encoding="utf-8")
            (root / "tests/test_quality_docs.py").write_text("", encoding="utf-8")
            (root / "scripts/nursing_ope_pipeline/tests").mkdir(parents=True)
            (
                root
                / "scripts/nursing_ope_pipeline/tests/test_normalized_to_quiz_source_e2e.py"
            ).write_text("", encoding="utf-8")
            (
                root / "scripts/nursing_ope_pipeline/tests/test_ope_corpus_overlay.py"
            ).write_text("", encoding="utf-8")

            commands = quality_check.build_operation_commands("python-tests", root)

        self.assertEqual(
            commands,
            [
                [
                    "python",
                    "-m",
                    "unittest",
                    "tools.quiz.tests.test_convert_quiz",
                    "tests.test_quality_check",
                    "tests.test_quality_docs",
                    "scripts.nursing_ope_pipeline.tests.test_normalized_to_quiz_source_e2e",
                    "scripts.nursing_ope_pipeline.tests.test_ope_corpus_overlay",
                ]
            ],
        )

    def test_complete_operation_expands_to_full_safe_union(self):
        commands = quality_check.build_operation_commands("complete", REPO_ROOT)

        self.assertEqual(
            quality_check_plan_profiles(commands), list(plan.BROAD_SHARED_CHECKS)
        )

    def test_python_tests_omit_optional_ope_modules_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            (root / "tools/quiz/tests").mkdir(parents=True)
            (root / "tools/quiz/tests/test_convert_quiz.py").write_text(
                "", encoding="utf-8"
            )
            (root / "tests").mkdir(parents=True)
            (root / "tests/test_quality_check.py").write_text("", encoding="utf-8")

            commands = quality_check.build_operation_commands("python-tests", root)

        self.assertEqual(
            commands,
            [
                [
                    "python",
                    "-m",
                    "unittest",
                    "tools.quiz.tests.test_convert_quiz",
                    "tests.test_quality_check",
                ]
            ],
        )

    def test_validate_operation_runs_all_non_mutating_quality_validators(self):
        with mock.patch.object(
            quality_check,
            "_repository_policy_targets",
            return_value=["README.md", "build/output.elf", "scripts/quality_check.py"],
        ):
            commands = quality_check.build_operation_commands("validate", REPO_ROOT)

        self.assertEqual(
            commands,
            [
                [
                    "python",
                    "-m",
                    "py_compile",
                    "scripts/quality_check.py",
                    "scripts/quality_check_plan.py",
                    "scripts/quality_check_git.py",
                ],
                ["python", "-m", "prek", "validate-config", ".pre-commit-config.yaml"],
                [
                    "actionlint",
                    ".github/workflows/ci.yml",
                    ".github/workflows/pr-formatting-check.yml",
                    ".github/workflows/release-fonts.yml",
                    ".github/workflows/release.yml",
                    ".github/workflows/release_candidate.yml",
                ],
                [
                    "shellcheck",
                    "bin/clang-format-fix",
                    "scripts/script_profile_mem.sh",
                    "scripts/update_hyphenation.sh",
                ],
                [
                    "python",
                    "-m",
                    "yamllint",
                    "-c",
                    ".yamllint.yaml",
                    ".github/workflows/ci.yml",
                    ".github/workflows/pr-formatting-check.yml",
                    ".github/workflows/release-fonts.yml",
                    ".github/workflows/release.yml",
                    ".github/workflows/release_candidate.yml",
                    ".pre-commit-config.yaml",
                    ".yamllint.yaml",
                ],
                ["gitleaks", "git", "--redact", "--no-banner", "."],
                [
                    "python",
                    "scripts/quality_check.py",
                    "repository-policy",
                    "README.md",
                    "build/output.elf",
                    "scripts/quality_check.py",
                ],
            ],
        )

    def test_format_check_runs_python_and_clang_checks_without_mutation(self):
        commands = quality_check.build_operation_commands("format-check", REPO_ROOT)

        self.assertEqual(
            commands,
            [
                [
                    "python",
                    "-m",
                    "ruff",
                    "format",
                    "--check",
                    "scripts",
                    "tools",
                    "tests",
                ],
                ["python", "-m", "ruff", "check", "scripts", "tools", "tests"],
                ["python", "scripts/quality_check.py", "clang-format-check"],
            ],
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

    def test_host_tests_pass_fetchcontent_cache_dir_to_cmake_configure(self):
        commands = quality_check.build_operation_commands("host-tests", REPO_ROOT)

        self.assertEqual(
            commands,
            [
                [
                    "cmake",
                    "-S",
                    "test",
                    "-B",
                    "build/test",
                    "-G",
                    "Ninja",
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DFETCHCONTENT_BASE_DIR=.cache/cmake-fetch",
                ],
                ["cmake", "--build", "build/test"],
                ["ctest", "--test-dir", "build/test", "--output-on-failure", "-j"],
            ],
        )
        self.assertEqual(quality_check.command_env_for_operation("host-tests"), {})


if __name__ == "__main__":
    unittest.main()
