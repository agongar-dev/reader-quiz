import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plan = load_module("quality_check_plan", "scripts/quality_check_plan.py")


class QualityCheckPlanTests(unittest.TestCase):
    def test_forbidden_artifact_paths_are_reported_without_flagging_ordinary_sources(
        self,
    ):
        forbidden = plan.find_forbidden_policy_paths(
            [
                "build/output.elf",
                "scripts/nursing_ope_pipeline/corpus/session.json",
                "tests/__pycache__/test_quality_check.cpython-314.pyc",
                "fixtures/generated.quiz",
                "src/corpus_manager.cpp",
                "scripts/nursing_ope_pipeline/ope_corpus.py",
            ]
        )

        self.assertEqual(
            forbidden,
            (
                "build/output.elf",
                "fixtures/generated.quiz",
                "scripts/nursing_ope_pipeline/corpus/session.json",
                "tests/__pycache__/test_quality_check.cpython-314.pyc",
            ),
        )

    def test_docs_only_paths_only_require_validate(self):
        result = plan.plan_for_paths(["README.md", "docs/usage.md"])

        self.assertEqual(result.profile, "docs_only")
        self.assertEqual(result.checks, ("validate",))
        self.assertEqual(
            result.reason,
            "docs_only: README.md, docs/usage.md",
        )

    def test_quiz_python_paths_select_python_profile(self):
        result = plan.plan_for_paths(["tools/quiz/tests/test_convert_quiz.py"])

        self.assertEqual(result.profile, "quiz_ope_python")
        self.assertEqual(result.checks, ("format-check", "python-tests", "validate"))
        self.assertEqual(
            result.reason,
            "quiz_ope_python: tools/quiz/tests/test_convert_quiz.py",
        )

    def test_cpp_paths_select_cpp_profile(self):
        result = plan.plan_for_paths(["src/main.cpp"])

        self.assertEqual(result.profile, "cpp")
        self.assertEqual(
            result.checks,
            ("format-check", "host-tests", "static-analysis", "firmware-build"),
        )

    def test_mixed_python_and_cpp_escalates_to_broad_shared(self):
        result = plan.plan_for_paths(
            [
                "tools/quiz/tests/test_convert_quiz.py",
                "src/main.cpp",
            ]
        )

        self.assertEqual(result.profile, "broad_shared")
        self.assertEqual(
            result.checks,
            (
                "format-check",
                "host-tests",
                "python-tests",
                "static-analysis",
                "firmware-build",
                "validate",
            ),
        )
        self.assertEqual(
            result.reason,
            "broad_shared: mixed categories (cpp, quiz_ope_python)",
        )

    def test_workflow_and_generated_outputs_fail_closed(self):
        workflow_result = plan.plan_for_paths([".github/workflows/ci.yml"])
        generated_result = plan.plan_for_paths([".pio/build/default/firmware.bin"])

        self.assertEqual(workflow_result.profile, "broad_shared")
        self.assertEqual(
            workflow_result.reason, "broad_shared: shared path .github/workflows/ci.yml"
        )
        self.assertEqual(generated_result.profile, "unknown")
        self.assertIn("fail_closed", generated_result.reason)
        self.assertEqual(generated_result.checks, plan.FULL_SAFE_CHECKS)

    def test_policy_allows_canonical_quiz_fixture_and_rejects_other_quiz_outputs(self):
        fixture_result = plan.plan_for_paths(["test/quiz/fixtures/valid.quiz"])
        generated_result = plan.plan_for_paths(["fixtures/generated.quiz"])

        self.assertEqual(fixture_result.profile, "cpp")
        self.assertEqual(
            fixture_result.reason,
            "cpp: test/quiz/fixtures/valid.quiz",
        )
        self.assertEqual(generated_result.profile, "unknown")
        self.assertIn("policy path", generated_result.reason)

    def test_cli_renders_explicit_path_plan_without_git(self):
        exit_code, rendered = plan.main(
            [
                "tools/quiz/tests/test_convert_quiz.py",
                "src/main.cpp",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            rendered,
            "profile=broad_shared checks=format-check,host-tests,python-tests,static-analysis,firmware-build,validate reason=broad_shared: mixed categories (cpp, quiz_ope_python)",
        )

    def test_normalization_rejects_paths_outside_repository(self):
        with self.assertRaisesRegex(ValueError, "escapes repository"):
            plan.plan_for_paths(["..\\secret.txt"])


if __name__ == "__main__":
    unittest.main()
