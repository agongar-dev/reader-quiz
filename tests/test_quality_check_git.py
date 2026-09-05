import importlib.util
import pathlib
import sys
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


git_helpers = load_module("quality_check_git", "scripts/quality_check_git.py")


class QualityCheckGitTests(unittest.TestCase):
    def test_parse_name_status_z_preserves_raw_path_bytes(self):
        entries = git_helpers.parse_name_status_z(
            b"M\x00README.md\x00R100\x00old name.py\x00new name.py\x00"
        )

        self.assertEqual(entries[0].status, "M")
        self.assertEqual(entries[0].paths, (b"README.md",))
        self.assertEqual(entries[1].status, "R100")
        self.assertEqual(entries[1].paths, (b"old name.py", b"new name.py"))

    def test_parse_name_status_z_rejects_invalid_rename_arity(self):
        with self.assertRaisesRegex(ValueError, "R status requires two paths"):
            git_helpers.parse_name_status_z(b"R099\x00old-only\x00")

    def test_parse_pre_push_records_models_update_states(self):
        records = git_helpers.parse_pre_push_stdin(
            "refs/heads/main abcdef refs/heads/main 123456\n"
            "refs/heads/topic fedcba refs/heads/topic " + ("0" * 40) + "\n"
            "delete " + ("0" * 40) + " refs/heads/old deadbeef\n"
        )

        self.assertEqual(records[0].state, "existing_update")
        self.assertEqual(records[0].rev_range, "123456..abcdef")
        self.assertEqual(records[1].state, "new_branch")
        self.assertIsNone(records[1].rev_range)
        self.assertEqual(records[2].state, "deleted_ref")

    def test_new_branch_uses_merge_base_when_available(self):
        record = git_helpers.parse_pre_push_stdin(
            f"refs/heads/topic abcdef refs/heads/topic {'0' * 40}\n"
        )[0]

        resolved = git_helpers.resolve_pre_push_record(
            record,
            lambda local_ref, local_sha: "base123",
        )

        self.assertEqual(resolved.state, "new_branch")
        self.assertEqual(resolved.rev_range, "base123..abcdef")
        self.assertFalse(resolved.fail_closed)

    def test_new_branch_without_baseline_fails_closed(self):
        record = git_helpers.parse_pre_push_stdin(
            f"refs/heads/topic abcdef refs/heads/topic {'0' * 40}\n"
        )[0]

        resolved = git_helpers.resolve_pre_push_record(record, lambda *_: None)

        self.assertTrue(resolved.fail_closed)
        self.assertEqual(resolved.reason, "new branch baseline unresolved")
        self.assertIsNone(resolved.rev_range)

    def test_decode_paths_includes_rename_source_and_destination(self):
        paths = git_helpers.decode_paths(
            [
                git_helpers.NameStatusEntry("R100", (b"README.md", b"src/main.cpp")),
            ]
        )

        self.assertEqual(paths, ["README.md", "src/main.cpp"])

    def test_decode_paths_keeps_copy_destination_only(self):
        paths = git_helpers.decode_paths(
            [
                git_helpers.NameStatusEntry("C100", (b"docs/old.md", b"docs/new.md")),
            ]
        )

        self.assertEqual(paths, ["docs/new.md"])

    def test_make_merge_base_resolver_uses_git_backed_baseline(self):
        responses = {
            ("git", "rev-parse", "--abbrev-ref", "topic@{upstream}"): b"origin/main\n",
            ("git", "merge-base", "origin/main", "abcdef"): b"base123\n",
        }

        def fake_run(argv, check, cwd, shell, capture_output):
            return mock.Mock(stdout=responses[tuple(argv)])

        resolver = git_helpers.make_merge_base_resolver("/repo", git_runner=fake_run)

        self.assertEqual(resolver("refs/heads/topic", "abcdef"), "base123")

    def test_parse_name_status_z_rejects_unknown_status_prefix(self):
        with self.assertRaisesRegex(ValueError, "unsupported status"):
            git_helpers.parse_name_status_z(b"Q\x00mystery\x00")

    def test_managed_pre_push_range_reads_docs_only_env_range(self):
        resolved = git_helpers.resolve_managed_pre_push_range(
            {
                "PRE_COMMIT_FROM_REF": "base123",
                "PRE_COMMIT_TO_REF": "head456",
            }
        )

        self.assertEqual(resolved, "base123..head456")

    def test_managed_pre_push_range_rejects_all_zero_from_to_pair(self):
        with self.assertRaisesRegex(ValueError, "managed pre-push range unavailable"):
            git_helpers.resolve_managed_pre_push_range(
                {
                    "PRE_COMMIT_FROM_REF": "0" * 40,
                    "PRE_COMMIT_TO_REF": "0" * 40,
                }
            )

    def test_managed_pre_push_range_rejects_zero_from_real_to_pair(self):
        with self.assertRaisesRegex(ValueError, "managed pre-push range unavailable"):
            git_helpers.resolve_managed_pre_push_range(
                {
                    "PRE_COMMIT_FROM_REF": "0" * 40,
                    "PRE_COMMIT_TO_REF": "head456",
                }
            )

    def test_managed_pre_push_range_rejects_partial_env(self):
        with self.assertRaisesRegex(
            ValueError, "requires both PRE_COMMIT_FROM_REF and PRE_COMMIT_TO_REF"
        ):
            git_helpers.resolve_managed_pre_push_range(
                {"PRE_COMMIT_FROM_REF": "base123"}
            )

    def test_managed_pre_push_range_rejects_missing_env(self):
        with self.assertRaisesRegex(ValueError, "managed pre-push range unavailable"):
            git_helpers.resolve_managed_pre_push_range({})

    def test_managed_pre_push_range_rejects_whitespace_refs(self):
        with self.assertRaisesRegex(
            ValueError, "managed pre-push range refs must not contain whitespace"
        ):
            git_helpers.resolve_managed_pre_push_range(
                {
                    "PRE_COMMIT_FROM_REF": "base 123",
                    "PRE_COMMIT_TO_REF": "head456",
                }
            )

    def test_managed_pre_push_range_rejects_partial_all_zero_ref_values(self):
        with self.assertRaisesRegex(
            ValueError, "managed pre-push range refs must be full commit SHAs"
        ):
            git_helpers.resolve_managed_pre_push_range(
                {
                    "PRE_COMMIT_FROM_REF": "0" * 39 + "a",
                    "PRE_COMMIT_TO_REF": "head456",
                }
            )


if __name__ == "__main__":
    unittest.main()
