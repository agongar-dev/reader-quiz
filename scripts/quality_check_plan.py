from __future__ import annotations

import posixpath
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DOCS_ONLY_CHECKS = ("validate",)
QUIZ_OPE_PYTHON_CHECKS = ("format-check", "python-tests", "validate")
CPP_CHECKS = ("format-check", "host-tests", "static-analysis", "firmware-build")
BROAD_SHARED_CHECKS = (
    "format-check",
    "host-tests",
    "python-tests",
    "static-analysis",
    "firmware-build",
    "validate",
)
FULL_SAFE_CHECKS = BROAD_SHARED_CHECKS


@dataclass(frozen=True)
class QualityPlan:
    profile: str
    checks: tuple[str, ...]
    reason: str
    normalized_paths: tuple[str, ...]


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    normalized = posixpath.normpath(normalized)
    if normalized in (".", ""):
        raise ValueError("empty path")
    if normalized.startswith("../") or normalized == "..":
        raise ValueError(f"path escapes repository: {path}")
    if posixpath.isabs(normalized):
        raise ValueError(f"absolute path not allowed: {path}")
    return normalized


def normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized = sorted({_normalize_path(path) for path in paths})
    if not normalized:
        raise ValueError("at least one path is required")
    return tuple(normalized)


def _contains_path_segment(path: str, segment: str) -> bool:
    parts = path.split("/")
    return segment in parts


def _is_policy_path(path: str) -> bool:
    if path.startswith((".pio/", ".cache/", "build/")):
        return True
    if path.endswith(".quiz") and not path.startswith("test/quiz/fixtures/"):
        return True
    if _contains_path_segment(path, "__pycache__"):
        return True
    return path.startswith("corpus/") or "/corpus/" in path


def find_forbidden_policy_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized_paths = normalize_paths(paths)
    return tuple(path for path in normalized_paths if _is_policy_path(path))


def _is_docs_path(path: str) -> bool:
    return path.startswith("docs/") or path.lower().endswith((".md", ".rst", ".txt"))


def _is_quiz_ope_python_path(path: str) -> bool:
    return path.startswith(("tools/quiz/", "scripts/nursing_ope_pipeline/"))


def _is_shared_path(path: str) -> bool:
    if path.startswith((".github/workflows/", ".githooks/")):
        return True
    if path in {"platformio.ini", "CMakeLists.txt"}:
        return True
    if (
        path.startswith("scripts/")
        and path.endswith(".py")
        and not path.startswith("scripts/nursing_ope_pipeline/")
    ):
        return True
    if path.endswith((".yml", ".yaml", ".toml", ".ini", ".json")):
        return True
    return path.endswith("CMakeLists.txt") or "/CMakeLists.txt" in path


def _is_cpp_path(path: str) -> bool:
    return path.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"))


def _classify_path(path: str) -> tuple[str, str]:
    if _is_policy_path(path):
        return "unknown", f"unknown/fail_closed: policy path {path}"
    if _is_shared_path(path):
        return "broad_shared", f"broad_shared: shared path {path}"
    if _is_docs_path(path):
        return "docs_only", f"docs_only: {path}"
    if _is_quiz_ope_python_path(path):
        return "quiz_ope_python", f"quiz_ope_python: {path}"
    if _is_cpp_path(path) or path.startswith(("src/", "lib/", "test/")):
        return "cpp", f"cpp: {path}"
    return "unknown", f"unknown/fail_closed: unclassified path {path}"


def _checks_for_profile(profile: str) -> tuple[str, ...]:
    return {
        "docs_only": DOCS_ONLY_CHECKS,
        "quiz_ope_python": QUIZ_OPE_PYTHON_CHECKS,
        "cpp": CPP_CHECKS,
        "broad_shared": BROAD_SHARED_CHECKS,
        "unknown": FULL_SAFE_CHECKS,
    }[profile]


def plan_for_paths(paths: Sequence[str]) -> QualityPlan:
    normalized_paths = normalize_paths(paths)
    categories = []
    first_reasons = {}
    for path in normalized_paths:
        category, reason = _classify_path(path)
        if category not in first_reasons:
            first_reasons[category] = reason
        if category not in categories:
            categories.append(category)
    category_set = set(categories)
    if "unknown" in category_set:
        reason = next(first_reasons["unknown"] for _ in [0])
        return QualityPlan("unknown", FULL_SAFE_CHECKS, reason, normalized_paths)
    if "broad_shared" in category_set:
        reason = next(first_reasons["broad_shared"] for _ in [0])
        return QualityPlan(
            "broad_shared", BROAD_SHARED_CHECKS, reason, normalized_paths
        )
    if len(category_set) == 1:
        profile = categories[0]
        if profile == "docs_only":
            reason = f"docs_only: {', '.join(normalized_paths)}"
        elif profile == "quiz_ope_python":
            reason = f"quiz_ope_python: {', '.join(normalized_paths)}"
        elif profile == "cpp":
            reason = f"cpp: {', '.join(normalized_paths)}"
        else:
            reason = first_reasons[profile]
        return QualityPlan(
            profile, _checks_for_profile(profile), reason, normalized_paths
        )
    ordered = ", ".join(sorted(category_set))
    return QualityPlan(
        "broad_shared",
        BROAD_SHARED_CHECKS,
        f"broad_shared: mixed categories ({ordered})",
        normalized_paths,
    )


def render_plan(plan: QualityPlan) -> str:
    return f"profile={plan.profile} checks={','.join(plan.checks)} reason={plan.reason}"


def main(argv: Sequence[str] | None = None) -> tuple[int, str]:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        plan = plan_for_paths(args)
    except ValueError as exc:
        return 2, str(exc)
    rendered = render_plan(plan)
    if argv is None:
        print(rendered)
    return 0, rendered


if __name__ == "__main__":
    raise SystemExit(main()[0])
