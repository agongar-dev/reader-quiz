import contextlib
import importlib.util
import io
import pathlib
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
    def test_repository_policy_rejects_forbidden_artifact_paths(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check.main(
                [
                    "repository-policy",
                    "build/output.elf",
                    "scripts/nursing_ope_pipeline/corpus/session.json",
                    "fixtures/generated.quiz",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "repository-policy rejected tracked artifacts: build/output.elf, fixtures/generated.quiz, scripts/nursing_ope_pipeline/corpus/session.json",
        )

    def test_repository_policy_allows_ordinary_source_paths(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check.main(
                [
                    "repository-policy",
                    "src/corpus_manager.cpp",
                    "scripts/nursing_ope_pipeline/ope_corpus.py",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_repository_policy_allows_tracked_canonical_quiz_fixture(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check.main(
                ["repository-policy", "test/quiz/fixtures/valid.quiz"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")

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

    def test_plan_execution_uses_process_groups_and_stops_on_failure(self):
        processes = [mock.Mock(), mock.Mock()]
        processes[0].wait.return_value = 0
        processes[1].wait.return_value = 2

        with mock.patch.object(
            quality_check.subprocess, "Popen", side_effect=processes
        ) as popen:
            exit_code = quality_check.run_commands(
                quality_check.build_operation_commands("host-tests", REPO_ROOT),
                REPO_ROOT,
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(popen.call_count, 2)
        first_argv, first_kwargs = popen.call_args_list[0]
        self.assertIsInstance(first_argv[0], list)
        self.assertFalse(first_kwargs["shell"])
        self.assertIs(first_kwargs["stdin"], quality_check.subprocess.DEVNULL)
        self.assertEqual(first_kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(first_kwargs["env"]["PIP_NO_INPUT"], "1")
        if quality_check.os.name == "nt":
            self.assertEqual(
                first_kwargs["creationflags"],
                quality_check.subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertTrue(first_kwargs["start_new_session"])

    def test_active_command_emits_periodic_heartbeat(self):
        process = mock.Mock()
        process.wait.side_effect = [
            quality_check.subprocess.TimeoutExpired(["slow-command"], 5.0),
            0,
        ]
        stderr = io.StringIO()

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    quality_check.subprocess, "Popen", return_value=process
                )
            )
            stack.enter_context(
                mock.patch.object(
                    quality_check.time,
                    "monotonic",
                    side_effect=[0.0, 0.0, 5.0, 5.0],
                )
            )
            stack.enter_context(contextlib.redirect_stderr(stderr))
            exit_code = quality_check.run_commands(
                [["slow-command"]],
                REPO_ROOT,
                env={
                    "QUALITY_CHECK_COMMAND_TIMEOUT_SECONDS": "60",
                    "QUALITY_CHECK_HEARTBEAT_SECONDS": "5",
                },
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "quality-check heartbeat: still running after 5s: slow-command",
            stderr.getvalue(),
        )

    def test_command_timeout_terminates_process_group(self):
        process = mock.Mock()
        process.wait.side_effect = quality_check.subprocess.TimeoutExpired(
            ["stuck-command"], 1.0
        )
        stderr = io.StringIO()

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    quality_check.subprocess, "Popen", return_value=process
                )
            )
            stack.enter_context(
                mock.patch.object(
                    quality_check.time, "monotonic", side_effect=[0.0, 0.0, 2.0]
                )
            )
            terminate = stack.enter_context(
                mock.patch.object(quality_check, "_terminate_process_group")
            )
            stack.enter_context(contextlib.redirect_stderr(stderr))
            exit_code = quality_check.run_commands(
                [["stuck-command"]],
                REPO_ROOT,
                env={
                    "QUALITY_CHECK_COMMAND_TIMEOUT_SECONDS": "1",
                    "QUALITY_CHECK_HEARTBEAT_SECONDS": "1",
                },
            )

        self.assertEqual(exit_code, 1)
        terminate.assert_called_once_with(process)
        self.assertIn(
            "quality-check failed: command timed out after 1s: stuck-command",
            stderr.getvalue(),
        )

    def test_posix_process_group_termination_escalates_after_grace(self):
        process = mock.Mock(pid=123)
        process.wait.side_effect = [0, 0]

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(quality_check.os, "name", "posix"))
            killpg = stack.enter_context(
                mock.patch.object(quality_check.os, "killpg")
            )
            stack.enter_context(
                mock.patch.object(
                    quality_check.time, "monotonic", side_effect=[0.0, 5.0]
                )
            )
            sleep = stack.enter_context(mock.patch.object(quality_check.time, "sleep"))
            quality_check._terminate_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(123, quality_check.signal.SIGTERM),
                mock.call(123, quality_check.signal.SIGKILL),
            ],
        )
        sleep.assert_not_called()

    def test_invalid_command_timing_configuration_fails_closed(self):
        stderr = io.StringIO()

        with contextlib.ExitStack() as stack:
            popen = stack.enter_context(
                mock.patch.object(quality_check.subprocess, "Popen")
            )
            stack.enter_context(contextlib.redirect_stderr(stderr))
            exit_code = quality_check.run_commands(
                [["unused-command"]],
                REPO_ROOT,
                env={"QUALITY_CHECK_COMMAND_TIMEOUT_SECONDS": "0"},
            )

        self.assertEqual(exit_code, 1)
        popen.assert_not_called()
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check failed: QUALITY_CHECK_COMMAND_TIMEOUT_SECONDS must be a positive number",
        )

    def test_plan_from_explicit_paths_runs_union_once(self):
        observed = []

        with mock.patch.object(
            quality_check,
            "run_commands",
            side_effect=lambda commands, cwd: observed.append(commands) or 0,
        ):
            exit_code = quality_check.main(
                [
                    "paths",
                    "tools/quiz/tests/test_convert_quiz.py",
                    "src/main.cpp",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_pre_push_rename_considers_source_and_destination_profiles(self):
        observed = []

        with (
            mock.patch.object(
                quality_check.quality_check_git,
                "parse_pre_push_stdin",
                return_value=[
                    quality_check.quality_check_git.PrePushRecord(
                        "refs/heads/topic",
                        "abcdef",
                        "refs/heads/topic",
                        "123456",
                        "existing_update",
                        "123456..abcdef",
                    )
                ],
            ),
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                return_value=[
                    quality_check.quality_check_git.NameStatusEntry(
                        "R100",
                        (b"README.md", b"src/main.cpp"),
                    )
                ],
            ),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_pre_push(REPO_ROOT, stdin_text="ignored")

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_pre_push_uses_git_backed_baseline_resolver_for_new_branch(self):
        observed = []

        with (
            mock.patch.object(
                quality_check.quality_check_git,
                "make_merge_base_resolver",
                return_value=lambda local_ref, local_sha: "base123",
            ) as make_resolver,
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                return_value=[
                    quality_check.quality_check_git.NameStatusEntry(
                        "M", (b"README.md",)
                    )
                ],
            ),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_pre_push(
                REPO_ROOT,
                stdin_text=f"refs/heads/topic abcdef refs/heads/topic {'0' * 40}\n",
            )

        self.assertEqual(exit_code, 0)
        make_resolver.assert_called_once_with(str(REPO_ROOT))
        self.assertEqual(quality_check_plan_profiles(observed[0]), ["validate"])

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

    def test_run_commands_reports_missing_tool_without_traceback(self):
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check.subprocess,
                "Popen",
                side_effect=FileNotFoundError("cmake"),
            ),
        ):
            exit_code = quality_check.run_commands([["cmake", "--version"]], REPO_ROOT)

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue().strip(), "quality-check failed: command not found: cmake"
        )

    def test_clang_format_check_reports_too_old_version_without_traceback(self):
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check.subprocess,
                "run",
                return_value=mock.Mock(stdout="clang-format version 20.1.0", stderr=""),
            ),
        ):
            exit_code = quality_check.main(
                [
                    "clang-format-check-files",
                    "clang-format-21|clang-format",
                    "src/main.cpp",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check failed: clang-format-21 must be version 21 or newer (clang-format version 20.1.0)",
        )

    def test_main_paths_reports_invalid_explicit_path_without_traceback(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check.main(["paths", "../secret.txt"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check input error: path escapes repository: ../secret.txt",
        )

    def test_pre_push_reports_malformed_pre_push_record_without_traceback(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check._run_pre_push(REPO_ROOT, stdin_text="broken\n")

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check input error: invalid pre-push record: 'broken'",
        )

    def test_pre_push_reports_malformed_git_diff_without_traceback(self):
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                side_effect=ValueError("unsupported status: 'Q'"),
            ),
        ):
            exit_code = quality_check._run_pre_push(
                REPO_ROOT,
                stdin_text="refs/heads/topic abcdef refs/heads/topic 123456\n",
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check input error: unsupported status: 'Q'",
        )

    def test_managed_pre_push_uses_docs_only_range_from_env(self):
        observed = []

        with (
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                return_value=[
                    quality_check.quality_check_git.NameStatusEntry(
                        "M", (b"README.md",)
                    )
                ],
            ),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "base123",
                    "PRE_COMMIT_TO_REF": "head456",
                },
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(quality_check_plan_profiles(observed[0]), ["validate"])

    def test_managed_pre_push_rename_escalates_from_source_and_destination(self):
        observed = []

        with (
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                return_value=[
                    quality_check.quality_check_git.NameStatusEntry(
                        "R100",
                        (b"README.md", b"src/main.cpp"),
                    )
                ],
            ),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "base123",
                    "PRE_COMMIT_TO_REF": "head456",
                },
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_managed_pre_push_without_range_returns_complete_profile_result(self):
        stderr = io.StringIO()
        observed = []

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(REPO_ROOT, env={})

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check managed pre-push: range unavailable; running complete profile",
        )
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_managed_pre_push_without_range_propagates_complete_profile_failure(self):
        stderr = io.StringIO()
        observed = []

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 7,
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(REPO_ROOT, env={})

        self.assertEqual(exit_code, 7)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check managed pre-push: range unavailable; running complete profile",
        )
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_managed_pre_push_all_zero_from_and_to_falls_back_to_complete_profile(self):
        stderr = io.StringIO()
        observed = []

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "0" * 40,
                    "PRE_COMMIT_TO_REF": "0" * 40,
                },
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check managed pre-push: range unavailable; running complete profile",
        )
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_managed_pre_push_zero_from_real_to_falls_back_to_complete_profile(self):
        stderr = io.StringIO()
        observed = []

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check,
                "run_commands",
                side_effect=lambda commands, cwd: observed.append(commands) or 0,
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "0" * 40,
                    "PRE_COMMIT_TO_REF": "head456",
                },
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check managed pre-push: range unavailable; running complete profile",
        )
        self.assertEqual(
            quality_check_plan_profiles(observed[0]),
            list(plan.BROAD_SHARED_CHECKS),
        )

    def test_managed_pre_push_rejects_partial_range_env(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={"PRE_COMMIT_FROM_REF": "base123"},
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check input error: managed pre-push requires both PRE_COMMIT_FROM_REF and PRE_COMMIT_TO_REF",
        )

    def test_managed_pre_push_reports_malformed_git_diff_without_traceback(self):
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                side_effect=ValueError("unsupported status: 'Q'"),
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "base123",
                    "PRE_COMMIT_TO_REF": "head456",
                },
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check input error: unsupported status: 'Q'",
        )

    def test_managed_pre_push_reports_git_failure_without_traceback(self):
        stderr = io.StringIO()

        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                quality_check.quality_check_git,
                "git_diff_name_status",
                side_effect=quality_check.subprocess.CalledProcessError(
                    returncode=7,
                    cmd=["git", "diff", "--name-status", "-z", "base123..head456"],
                ),
            ),
        ):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "base123",
                    "PRE_COMMIT_TO_REF": "head456",
                },
            )

        self.assertEqual(exit_code, 7)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check failed: git diff --name-status -z base123..head456",
        )

    def test_managed_pre_push_rejects_whitespace_refs(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = quality_check._run_managed_pre_push(
                REPO_ROOT,
                env={
                    "PRE_COMMIT_FROM_REF": "base 123",
                    "PRE_COMMIT_TO_REF": "head456",
                },
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "quality-check input error: managed pre-push range refs must not contain whitespace",
        )


def quality_check_plan_profiles(commands):
    operations = []
    for command in commands:
        if (
            command[:5] == ["python", "-m", "ruff", "format", "--check"]
            or command[:3]
            == ["python", "scripts/quality_check.py", "clang-format-check"]
            or command[:3] == ["python", "scripts/quality_check.py", "format-check"]
        ):
            operations.append("format-check")
        elif command[:2] == ["cmake", "-S"]:
            operations.append("host-tests")
        elif command[:2] == ["python", "-m"] and "unittest" in command:
            operations.append("python-tests")
        elif command[:2] == ["pio", "check"]:
            operations.append("static-analysis")
        elif command[:2] == ["pio", "run"]:
            operations.append("firmware-build")
        elif command[:4] == [
            "python",
            "-m",
            "py_compile",
            "scripts/quality_check.py",
        ] or command[:3] == ["python", "scripts/quality_check.py", "validate"]:
            operations.append("validate")
    seen = []
    for item in operations:
        if item not in seen:
            seen.append(item)
    return seen


if __name__ == "__main__":
    unittest.main()
