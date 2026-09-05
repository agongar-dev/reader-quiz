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


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: quality_check.py <operation>", file=sys.stderr)
        return 2
    command = args[0]
    if command not in OPERATIONS:
        print(f"unknown operation: {command}", file=sys.stderr)
        return 2
    print(f"quality-check operation dispatch is unavailable: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

