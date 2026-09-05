from __future__ import annotations

import importlib.util
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON_TARGETS = ("scripts", "tools", "tests")
CLANG_FORMAT_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
CLANG_FORMAT_EXCLUDED_PREFIXES = (
    "lib/EpdFont/builtinFonts/",
    "lib/Epub/Epub/hyphenation/generated/",
    "lib/uzlib/",
    "lib/miniz/third_party/",
)
CLANG_FORMAT_EXCLUDED_FILES = {
    "lib/I18n/I18nKeys.h",
    "lib/I18n/I18nStrings.h",
    "lib/I18n/I18nStrings.cpp",
}
VALIDATE_YAML_FILES = (
    ".github/workflows/ci.yml",
    ".github/workflows/pr-formatting-check.yml",
    ".github/workflows/release-fonts.yml",
    ".github/workflows/release.yml",
    ".github/workflows/release_candidate.yml",
    ".pre-commit-config.yaml",
    ".yamllint.yaml",
)
COMMAND_TIMEOUT_ENV = "QUALITY_CHECK_COMMAND_TIMEOUT_SECONDS"
HEARTBEAT_INTERVAL_ENV = "QUALITY_CHECK_HEARTBEAT_SECONDS"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 900.0
DEFAULT_HEARTBEAT_SECONDS = 30.0
PROCESS_TERMINATION_GRACE_SECONDS = 5.0


def _load_sibling_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / filename
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


quality_check_plan = _load_sibling_module("quality_check_plan", "quality_check_plan.py")
quality_check_git = _load_sibling_module("quality_check_git", "quality_check_git.py")


OPERATIONS = {
    "clang-format-check",
    "clang-format-check-files",
    "format-changed",
    "format-check",
    "host-tests",
    "python-tests",
    "static-analysis",
    "firmware-build",
    "validate",
    "repository-policy",
    "pre-push",
    "managed-pre-push",
    "complete",
}


def _python_executable() -> str:
    return pathlib.Path(sys.executable).name or "python"


def _module_name_for_test(path: pathlib.Path, repo_root: pathlib.Path) -> str:
    return ".".join(path.relative_to(repo_root).with_suffix("").parts)


def build_python_test_modules(repo_root: pathlib.Path) -> list[str]:
    modules: list[str] = []
    seen: set[str] = set()

    def add(module: str) -> None:
        if module not in seen:
            seen.add(module)
            modules.append(module)

    add("tools.quiz.tests.test_convert_quiz")

    tests_dir = repo_root / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("test_*.py")):
            add(_module_name_for_test(path, repo_root))

    optional = (
        "scripts.nursing_ope_pipeline.tests.test_normalized_to_quiz_source_e2e",
        "scripts.nursing_ope_pipeline.tests.test_ope_corpus_overlay",
    )
    for module in optional:
        if (repo_root / pathlib.Path(*module.split("."))).with_suffix(".py").exists():
            add(module)
    return modules


def _decode_git_ls_files_z(data: bytes) -> list[str]:
    tokens = data.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    return [os.fsdecode(token) for token in tokens if token]


def _tracked_relative_paths(
    repo_root: pathlib.Path,
    git_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> list[str]:
    try:
        completed = git_runner(
            ["git", "ls-files", "-z"],
            check=True,
            cwd=str(repo_root),
            shell=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git ls-files -z failed: git not found") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("git ls-files -z failed") from exc
    return _decode_git_ls_files_z(completed.stdout)


def _repository_policy_targets(
    repo_root: pathlib.Path,
    tracked_paths_resolver: Callable[
        [pathlib.Path], Sequence[str]
    ] = _tracked_relative_paths,
) -> list[str]:
    return list(quality_check_plan.normalize_paths(tracked_paths_resolver(repo_root)))


def _is_clang_format_path(path: str) -> bool:
    if pathlib.PurePosixPath(path).suffix not in CLANG_FORMAT_EXTENSIONS:
        return False
    if any(path.startswith(prefix) for prefix in CLANG_FORMAT_EXCLUDED_PREFIXES):
        return False
    if path.endswith(".generated.h"):
        return False
    return path not in CLANG_FORMAT_EXCLUDED_FILES


def _clang_format_paths(
    repo_root: pathlib.Path,
    tracked_paths_resolver: Callable[
        [pathlib.Path], Sequence[str]
    ] = _tracked_relative_paths,
) -> list[str]:
    return sorted(
        path
        for path in tracked_paths_resolver(repo_root)
        if _is_clang_format_path(path)
    )


def _chunked(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def command_env_for_operation(operation: str) -> dict[str, str]:
    del operation
    return {}


def _build_external_commands(
    operation: str, repo_root: pathlib.Path
) -> list[list[str]]:
    python = _python_executable()
    if operation == "format-changed":
        return [
            [python, "-m", "ruff", "format", *PYTHON_TARGETS],
            [python, "-m", "ruff", "check", "--fix", *PYTHON_TARGETS],
        ]
    if operation == "format-check":
        return [
            [python, "-m", "ruff", "format", "--check", *PYTHON_TARGETS],
            [python, "-m", "ruff", "check", *PYTHON_TARGETS],
            [python, "scripts/quality_check.py", "clang-format-check"],
        ]
    if operation == "clang-format-check":
        return [
            [
                python,
                "scripts/quality_check.py",
                "clang-format-check-files",
                "clang-format-21|clang-format",
                *chunk,
            ]
            for chunk in _chunked(_clang_format_paths(repo_root), 100)
        ]
    if operation == "host-tests":
        return [
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
        ]
    if operation == "python-tests":
        return [[python, "-m", "unittest", *build_python_test_modules(repo_root)]]
    if operation == "static-analysis":
        return [
            [
                "pio",
                "check",
                "--fail-on-defect",
                "low",
                "--fail-on-defect",
                "medium",
                "--fail-on-defect",
                "high",
            ]
        ]
    if operation == "firmware-build":
        return [["pio", "run", "-e", "default", "-e", "sticky"]]
    if operation == "validate":
        return [
            [
                python,
                "-m",
                "py_compile",
                "scripts/quality_check.py",
                "scripts/quality_check_plan.py",
                "scripts/quality_check_git.py",
            ],
            [python, "-m", "prek", "validate-config", ".pre-commit-config.yaml"],
            ["actionlint", *VALIDATE_YAML_FILES[:5]],
            [
                "shellcheck",
                "bin/clang-format-fix",
                "scripts/script_profile_mem.sh",
                "scripts/update_hyphenation.sh",
            ],
            [python, "-m", "yamllint", "-c", ".yamllint.yaml", *VALIDATE_YAML_FILES],
            ["gitleaks", "git", "--redact", "--no-banner", "."],
            [
                python,
                "scripts/quality_check.py",
                "repository-policy",
                *_repository_policy_targets(repo_root),
            ],
        ]
    return []


def build_operation_commands(
    operation: str, repo_root: pathlib.Path | str
) -> list[list[str]]:
    root_path = pathlib.Path(repo_root)
    if operation == "complete":
        commands: list[list[str]] = []
        for nested in quality_check_plan.BROAD_SHARED_CHECKS:
            commands.extend(_build_external_commands(nested, root_path))
        return commands
    return _build_external_commands(operation, root_path)


def _report_command_error(
    argv: Sequence[str], exc: OSError | subprocess.CalledProcessError | RuntimeError
) -> int:
    if isinstance(exc, FileNotFoundError):
        print(f"quality-check failed: command not found: {argv[0]}", file=sys.stderr)
        return 1
    if isinstance(exc, RuntimeError):
        print(f"quality-check failed: {exc}", file=sys.stderr)
        return 1
    print(f"quality-check failed: {' '.join(argv)}", file=sys.stderr)
    if isinstance(exc, subprocess.CalledProcessError):
        return int(exc.returncode) or 1
    return 1


def _positive_seconds(env: dict[str, str], name: str, default: float) -> float:
    raw_value = env.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def _wait_remaining_grace(started_at: float) -> None:
    remaining = PROCESS_TERMINATION_GRACE_SECONDS - (time.monotonic() - started_at)
    if remaining > 0:
        time.sleep(remaining)


def _terminate_process_group(process: subprocess.Popen) -> None:
    if os.name == "nt":
        started_at = time.monotonic()
        try:
            process.send_signal(subprocess.CTRL_BREAK_EVENT)
            process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _wait_remaining_grace(started_at)
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=PROCESS_TERMINATION_GRACE_SECONDS,
            )
            if completed.returncode and process.poll() is None:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
        try:
            process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
        return

    started_at = time.monotonic()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _wait_remaining_grace(started_at)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _run_command(
    argv: Sequence[str],
    cwd: pathlib.Path | str,
    env: dict[str, str],
    timeout_seconds: float,
    heartbeat_seconds: float,
) -> None:
    popen_kwargs = {
        "cwd": str(cwd),
        "shell": False,
        "env": env,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(list(argv), **popen_kwargs)
    started_at = time.monotonic()
    while True:
        elapsed = time.monotonic() - started_at
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            _terminate_process_group(process)
            raise RuntimeError(
                f"command timed out after {timeout_seconds:g}s: {' '.join(argv)}"
            )
        try:
            return_code = process.wait(timeout=min(heartbeat_seconds, remaining))
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                _terminate_process_group(process)
                raise RuntimeError(
                    f"command timed out after {timeout_seconds:g}s: {' '.join(argv)}"
                )
            print(
                f"quality-check heartbeat: still running after {elapsed:g}s: {' '.join(argv)}",
                file=sys.stderr,
                flush=True,
            )
            continue

        if return_code:
            raise subprocess.CalledProcessError(return_code, list(argv))
        return


def run_commands(
    commands: Iterable[Sequence[str]],
    cwd: pathlib.Path | str,
    env: dict[str, str] | None = None,
) -> int:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    command_env["GIT_TERMINAL_PROMPT"] = "0"
    command_env["PIP_NO_INPUT"] = "1"

    try:
        timeout_seconds = _positive_seconds(
            command_env, COMMAND_TIMEOUT_ENV, DEFAULT_COMMAND_TIMEOUT_SECONDS
        )
        heartbeat_seconds = _positive_seconds(
            command_env, HEARTBEAT_INTERVAL_ENV, DEFAULT_HEARTBEAT_SECONDS
        )
    except RuntimeError as exc:
        return _report_command_error([], exc)

    for argv in commands:
        try:
            _run_command(
                argv,
                cwd,
                command_env,
                timeout_seconds,
                heartbeat_seconds,
            )
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            return _report_command_error(list(argv), exc)
    return 0


def _plan_commands_from_paths(
    paths: Sequence[str], repo_root: pathlib.Path
) -> list[list[str]]:
    plan = quality_check_plan.plan_for_paths(paths)
    commands = []
    python = _python_executable()
    for operation in plan.checks:
        if operation in {"format-check", "validate"}:
            commands.append([python, "scripts/quality_check.py", operation])
        else:
            commands.extend(build_operation_commands(operation, repo_root))
    return commands


def _run_repository_policy(paths: Sequence[str]) -> int:
    if not paths:
        return 0
    forbidden_paths = quality_check_plan.find_forbidden_policy_paths(paths)
    if not forbidden_paths:
        return 0
    print(
        "repository-policy rejected tracked artifacts: " + ", ".join(forbidden_paths),
        file=sys.stderr,
    )
    return 1


def _commands_for_rev_ranges(
    repo_root: pathlib.Path, rev_ranges: Sequence[str]
) -> list[list[str]]:
    paths = []
    for rev_range in rev_ranges:
        paths.extend(
            quality_check_git.decode_paths(
                quality_check_git.git_diff_name_status(str(repo_root), rev_range)
            )
        )
    return _plan_commands_from_paths(paths, repo_root)


def _run_pre_push(
    repo_root: pathlib.Path,
    stdin_text: str | None = None,
    merge_base_resolver=None,
) -> int:
    payload = sys.stdin.read() if stdin_text is None else stdin_text
    resolver = merge_base_resolver or quality_check_git.make_merge_base_resolver(
        str(repo_root)
    )
    try:
        records = [
            quality_check_git.resolve_pre_push_record(record, resolver)
            for record in quality_check_git.parse_pre_push_stdin(payload)
        ]
        if any(record.fail_closed for record in records):
            return run_commands(
                build_operation_commands("complete", repo_root), repo_root
            )
        rev_ranges = [record.rev_range for record in records if record.rev_range]
        if not rev_ranges:
            return 0
        return run_commands(_commands_for_rev_ranges(repo_root, rev_ranges), repo_root)
    except ValueError as exc:
        print(f"quality-check input error: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as exc:
        argv = exc.cmd if isinstance(exc, subprocess.CalledProcessError) else ["git"]
        return _report_command_error(argv, exc)


def _run_managed_pre_push(
    repo_root: pathlib.Path,
    env: dict[str, str] | None = None,
) -> int:
    managed_env = os.environ if env is None else env
    try:
        rev_range = quality_check_git.resolve_managed_pre_push_range(managed_env)
    except ValueError as exc:
        if str(exc) == "managed pre-push range unavailable":
            print(
                "quality-check managed pre-push: range unavailable; running complete profile",
                file=sys.stderr,
            )
            return run_commands(
                build_operation_commands("complete", repo_root), repo_root
            )
        print(f"quality-check input error: {exc}", file=sys.stderr)
        return 2

    try:
        return run_commands(_commands_for_rev_ranges(repo_root, [rev_range]), repo_root)
    except ValueError as exc:
        print(f"quality-check input error: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as exc:
        argv = exc.cmd if isinstance(exc, subprocess.CalledProcessError) else ["git"]
        return _report_command_error(argv, exc)


def _clang_format_binary() -> str:
    for candidate in ("clang-format-21", "clang-format"):
        try:
            completed = subprocess.run(
                [candidate, "--version"],
                check=True,
                cwd=str(REPO_ROOT),
                shell=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            continue
        version_text = completed.stdout.strip() or completed.stderr.strip()
        match = re.search(r"(\d+)", version_text)
        if not match or int(match.group(1)) < 21:
            raise RuntimeError(
                f"{candidate} must be version 21 or newer ({version_text or 'unknown version'})"
            )
        return candidate
    raise RuntimeError("clang-format-21 or clang-format 21+ is required")


def _run_clang_format_check_files(paths: Sequence[str]) -> int:
    if not paths:
        return 0
    selector, *targets = list(paths)
    del selector
    try:
        binary = _clang_format_binary()
        return run_commands(
            [[binary, "--dry-run", "--Werror", "-style=file", *targets]], REPO_ROOT
        )
    except RuntimeError as exc:
        return _report_command_error(["clang-format"], exc)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo_root = REPO_ROOT
    if not args:
        print("usage: quality_check.py <operation>|paths <path...>", file=sys.stderr)
        return 2
    command = args[0]
    try:
        if command == "paths":
            if len(args) < 2:
                print("paths requires at least one explicit path", file=sys.stderr)
                return 2
            return run_commands(
                _plan_commands_from_paths(args[1:], repo_root), repo_root
            )
        if command == "repository-policy":
            return _run_repository_policy(args[1:])
        if command == "pre-push":
            return _run_pre_push(repo_root)
        if command == "managed-pre-push":
            return _run_managed_pre_push(repo_root)
        if command == "clang-format-check-files":
            return _run_clang_format_check_files(args[1:])
        if command not in OPERATIONS:
            print(f"unknown operation: {command}", file=sys.stderr)
            return 2
        return run_commands(
            build_operation_commands(command, repo_root),
            repo_root,
            env=command_env_for_operation(command),
        )
    except ValueError as exc:
        print(f"quality-check input error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        return _report_command_error(["git"], exc)


if __name__ == "__main__":
    raise SystemExit(main())
