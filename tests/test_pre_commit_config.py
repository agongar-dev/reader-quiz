import pathlib
import re
import textwrap
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"


def hook_block(text: str, hook_id: str) -> str:
    pattern = re.compile(
        rf"^\s*- id: {re.escape(hook_id)}\n(?:^\s+.*\n)*?(?=^\s*- id: |^\s*- repo: |\Z)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        raise AssertionError(f"missing hook block for {hook_id}")
    return match.group(0)


def hook_scalar(block: str, key: str) -> str:
    lines = block.splitlines()
    for index, line in enumerate(lines):
        match = re.match(rf"^(\s+){re.escape(key)}:\s*(.*)$", line)
        if not match:
            continue

        indentation = len(match.group(1))
        value = match.group(2).strip()
        if value and value not in {">-", "|", "|-", ">"}:
            return value

        nested_lines = []
        for nested_line in lines[index + 1 :]:
            if not nested_line.strip():
                nested_lines.append("")
                continue
            nested_indentation = len(nested_line) - len(nested_line.lstrip())
            if nested_indentation <= indentation:
                break
            nested_lines.append(nested_line[indentation + 2 :])

        if value in {">-", ">"}:
            return textwrap.dedent("\n".join(nested_lines)).replace("\n", " ").strip()
        return textwrap.dedent("\n".join(nested_lines)).strip()

    raise AssertionError(f"missing scalar {key}")


class PreCommitConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CONFIG_PATH.read_text(encoding="utf-8")

    def test_default_install_hook_types_include_pre_commit_and_pre_push(self):
        self.assertRegex(
            self.text,
            r"(?m)^default_install_hook_types:\s*\[pre-commit, pre-push\]\s*$",
        )

    def test_pins_verified_repositories(self):
        self.assertIn("repo: https://github.com/pre-commit/pre-commit-hooks", self.text)
        self.assertIn("rev: v6.0.0", self.text)
        self.assertIn("repo: https://github.com/astral-sh/ruff-pre-commit", self.text)
        self.assertIn("rev: v0.16.0", self.text)
        self.assertIn(
            "repo: https://github.com/pre-commit/mirrors-clang-format", self.text
        )
        self.assertIn("rev: v21.1.8", self.text)
        self.assertIn("repo: https://github.com/gitleaks/gitleaks", self.text)
        self.assertIn("rev: v8.30.1", self.text)

    def test_all_hooks_are_restricted_to_pre_commit_stage(self):
        hook_ids = [
            "trailing-whitespace",
            "end-of-file-fixer",
            "check-merge-conflict",
            "check-json",
            "check-yaml",
            "check-case-conflict",
            "check-added-large-files",
            "ruff-check",
            "ruff-format",
            "clang-format",
            "gitleaks",
            "repository-policy",
        ]
        for hook_id in hook_ids:
            self.assertIn("stages: [pre-commit]", hook_block(self.text, hook_id))

    def test_clang_format_excludes_generated_and_vendor_paths(self):
        clang_block = hook_block(self.text, "clang-format")
        exclude_regex = hook_scalar(clang_block, "exclude")
        pattern = re.compile(exclude_regex)

        for excluded_path in [
            "lib/EpdFont/builtinFonts/generated.h",
            "lib/Epub/Epub/hyphenation/generated/table.cpp",
            "lib/uzlib/source.c",
            "lib/miniz/third_party/vendor.cpp",
            "src/network/html/page.generated.h",
        ]:
            self.assertRegex(excluded_path, pattern)

        self.assertNotRegex("src/main.cpp", pattern)

    def test_local_repository_policy_hook_uses_system_language_and_quality_check_entry(
        self,
    ):
        self.assertIn("repo: local", self.text)
        policy_block = hook_block(self.text, "repository-policy")
        self.assertIn("name: repository policy", policy_block)
        self.assertIn(
            "entry: python scripts/quality_check.py repository-policy", policy_block
        )
        self.assertIn("language: system", policy_block)
        self.assertIn("pass_filenames: true", policy_block)
        self.assertIn("stages: [pre-commit]", policy_block)

    def test_local_managed_pre_push_hook_uses_managed_runner_contract(self):
        pre_push_block = hook_block(self.text, "managed-pre-push")
        self.assertIn("name: managed pre-push", pre_push_block)
        self.assertIn(
            "entry: python scripts/quality_check.py managed-pre-push", pre_push_block
        )
        self.assertIn("language: system", pre_push_block)
        self.assertIn("stages: [pre-push]", pre_push_block)
        self.assertIn("pass_filenames: false", pre_push_block)
        self.assertIn("always_run: true", pre_push_block)
        self.assertIn("require_serial: true", pre_push_block)


if __name__ == "__main__":
    unittest.main()
