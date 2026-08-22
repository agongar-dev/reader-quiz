from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "tools/quiz/convert_quiz.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAGIC = bytes((0x47, 0x51, 0x55, 0x49, 0x5A, 0x0D, 0x0A, 0x1A))

CANONICAL_SOURCE = {
    "deck": {"id": "mini-quiz", "title": "Mini Quiz"},
    "questions": [
        {"prompt": "First?", "choices": ["A", "B"], "correct": 1},
        {"prompt": "Pi?", "choices": ["2", "3.14", "4"], "correct": 1, "explanation": "Approx."},
    ],
}


def run_converter(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(output)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_diagnostics(stderr: str) -> list[dict[str, object]]:
    if not stderr.strip():
        return []
    return [json.loads(line) for line in stderr.splitlines() if line.strip()]


def length_prefixed(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(2, "little") + raw


def expected_revision_identity() -> bytes:
    digest = hashlib.sha256()
    deck_identity = hashlib.sha256(b"quiz-deck-id-v1\0" + CANONICAL_SOURCE["deck"]["id"].encode("utf-8")).digest()[:16]
    digest.update(b"quiz-revision-v1\0")
    digest.update(deck_identity)
    digest.update(len(CANONICAL_SOURCE["questions"]).to_bytes(4, "little"))
    for question in CANONICAL_SOURCE["questions"]:
        digest.update(length_prefixed(question["prompt"]))
        digest.update(bytes((len(question["choices"]),)))
        for choice in question["choices"]:
            digest.update(length_prefixed(choice))
        digest.update(bytes((question["correct"],)))
        digest.update(length_prefixed(question.get("explanation", "")))
    return digest.digest()[:16]


def write_source(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_quiz_artifact(data: bytes) -> dict[str, object]:
    assert data[:8] == MAGIC
    assert int.from_bytes(data[8:10], "little") == 1
    assert int.from_bytes(data[10:12], "little") == 176
    assert int.from_bytes(data[12:16], "little") == 0
    declared_size = int.from_bytes(data[16:20], "little")
    question_count = int.from_bytes(data[20:24], "little")
    index_offset = int.from_bytes(data[24:28], "little")
    index_bytes = int.from_bytes(data[28:32], "little")
    records_offset = int.from_bytes(data[32:36], "little")
    index_entry_size = int.from_bytes(data[36:38], "little")
    title_length = int.from_bytes(data[38:40], "little")
    deck_identity = data[40:56]
    revision_identity = data[56:72]
    title = data[72 : 72 + title_length].decode("utf-8")
    assert declared_size == len(data)
    assert question_count == 2
    assert index_offset == 176
    assert index_entry_size == 8
    assert index_bytes == question_count * 8
    assert records_offset == 176 + index_bytes
    assert data[72 + title_length : 168] == b"\0" * (96 - title_length)
    assert data[168:176] == b"\0" * 8

    questions: list[dict[str, object]] = []
    expected_offset = records_offset
    for ordinal in range(question_count):
        entry_offset = index_offset + ordinal * 8
        record_offset = int.from_bytes(data[entry_offset : entry_offset + 4], "little")
        record_bytes = int.from_bytes(data[entry_offset + 4 : entry_offset + 8], "little")
        assert record_offset == expected_offset
        record = data[record_offset : record_offset + record_bytes]
        assert int.from_bytes(record[0:2], "little") == 24
        choice_count = record[2]
        correct = record[3]
        prompt_length = int.from_bytes(record[4:6], "little")
        explanation_length = int.from_bytes(record[6:8], "little")
        choice_lengths = [int.from_bytes(record[8 + i * 2 : 10 + i * 2], "little") for i in range(6)]
        payload_length = int.from_bytes(record[20:24], "little")
        active_choice_lengths = choice_lengths[:choice_count]
        assert payload_length == prompt_length + explanation_length + sum(active_choice_lengths)
        assert record_bytes == 24 + payload_length
        cursor = 24
        prompt = record[cursor : cursor + prompt_length].decode("utf-8")
        cursor += prompt_length
        choices = []
        for choice_length in active_choice_lengths:
            choices.append(record[cursor : cursor + choice_length].decode("utf-8"))
            cursor += choice_length
        explanation = record[cursor : cursor + explanation_length].decode("utf-8")
        cursor += explanation_length
        assert cursor == len(record)
        questions.append(
            {
                "prompt": prompt,
                "choices": choices,
                "correct": correct,
                "explanation": explanation,
            }
        )
        expected_offset += record_bytes

    assert expected_offset == len(data)
    return {
        "title": title,
        "deck_identity": deck_identity,
        "revision_identity": revision_identity,
        "questions": questions,
    }


class ConvertQuizTest(unittest.TestCase):
    def test_canonical_source_is_deterministic_and_matches_the_quiz_v1_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_one = Path(tmp_dir) / "one.quiz"
            out_two = Path(tmp_dir) / "two.quiz"

            first = run_converter(FIXTURES / "canonical.json", out_one)
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            self.assertEqual(first.stdout, "")
            self.assertEqual(first.stderr, "")

            second = run_converter(FIXTURES / "canonical.json", out_two)
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertEqual(second.stdout, "")
            self.assertEqual(second.stderr, "")

            artifact_bytes = out_one.read_bytes()
            self.assertEqual(artifact_bytes, out_two.read_bytes())

            artifact = parse_quiz_artifact(artifact_bytes)
            self.assertEqual(artifact["title"], "Mini Quiz")
            self.assertEqual(
                artifact["deck_identity"],
                hashlib.sha256(b"quiz-deck-id-v1\0mini-quiz").digest()[:16],
            )
            self.assertEqual(artifact["revision_identity"], expected_revision_identity())
            self.assertEqual(
                artifact["questions"],
                [
                    {"prompt": "First?", "choices": ["A", "B"], "correct": 1, "explanation": ""},
                    {"prompt": "Pi?", "choices": ["2", "3.14", "4"], "correct": 1, "explanation": "Approx."},
                ],
            )

    def test_invalid_fields_and_bool_correct_produce_stable_located_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "bad.quiz"
            result = run_converter(FIXTURES / "invalid_diagnostics.json", out_path)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out_path.exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "unknown-field",
                        "path": "$.surprise",
                        "question_ordinal": None,
                        "message": "unknown field 'surprise'",
                    },
                    {
                        "severity": "error",
                        "code": "unknown-field",
                        "path": "$.deck.extra",
                        "question_ordinal": None,
                        "message": "unknown field 'extra'",
                    },
                    {
                        "severity": "error",
                        "code": "unknown-field",
                        "path": "$.questions[0].extra",
                        "question_ordinal": 1,
                        "message": "unknown field 'extra'",
                    },
                    {
                        "severity": "error",
                        "code": "invalid-type",
                        "path": "$.questions[0].correct",
                        "question_ordinal": 1,
                        "message": "expected integer, got bool",
                    },
                ],
            )

    def test_choice_count_bounds_are_reported_with_question_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "bounds.quiz"
            result = run_converter(FIXTURES / "invalid_bounds.json", out_path)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out_path.exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "out-of-bounds",
                        "path": "$.questions[0].choices",
                        "question_ordinal": 1,
                        "message": "expected 2..6 items, got 1",
                    }
                ],
            )

    def test_duplicate_keys_fail_without_clobbering_existing_output_or_leaking_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "existing.quiz"
            out_path.write_bytes(b"keep-me")
            result = run_converter(FIXTURES / "invalid_duplicate_key.json", out_path)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(out_path.read_bytes(), b"keep-me")
            self.assertFalse((out_path.parent / f"{out_path.name}.tmp").exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "duplicate-key",
                        "path": "$",
                        "question_ordinal": None,
                        "message": "duplicate key 'format'",
                    }
                ],
            )

    def test_utf8_bom_is_rejected_before_json_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "bom.quiz"
            result = run_converter(FIXTURES / "invalid_bom.json", out_path)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out_path.exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "byte-order-mark",
                        "path": "$",
                        "question_ordinal": None,
                        "message": "UTF-8 BOM is not allowed",
                    }
                ],
            )

    def test_deck_id_must_fit_within_64_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "deck-id-too-long.json"
            out_path = Path(tmp_dir) / "deck-id-too-long.quiz"
            write_source(
                source_path,
                {
                    "format": "quiz-source-v1",
                    "deck": {"id": "a" * 65, "title": "Deck Id Too Long"},
                    "questions": [{"prompt": "Q?", "choices": ["A", "B"], "correct": 0}],
                },
            )

            result = run_converter(source_path, out_path)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out_path.exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "out-of-bounds",
                        "path": "$.deck.id",
                        "question_ordinal": None,
                        "message": "expected 1..64 UTF-8 bytes, got 65",
                    }
                ],
            )

    def test_missing_source_uses_io_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "missing.json"
            out_path = Path(tmp_dir) / "missing.quiz"

            result = run_converter(source_path, out_path)

            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out_path.exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "read-failed",
                        "path": "$",
                        "question_ordinal": None,
                        "message": "failed to read source: No such file or directory",
                    }
                ],
            )

    def test_missing_output_directory_uses_publication_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "canonical.json"
            write_source(source_path, {"format": "quiz-source-v1", **CANONICAL_SOURCE})
            out_path = Path(tmp_dir) / "missing" / "output.quiz"

            result = run_converter(source_path, out_path)

            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, "")
            self.assertFalse(out_path.exists())
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "write-failed",
                        "path": "$",
                        "question_ordinal": None,
                        "message": "failed to write output: No such file or directory",
                    }
                ],
            )

    def test_preexisting_sibling_temp_file_fails_without_clobbering_output_or_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "canonical.json"
            write_source(source_path, {"format": "quiz-source-v1", **CANONICAL_SOURCE})
            out_path = Path(tmp_dir) / "existing.quiz"
            out_path.write_bytes(b"keep-output")
            temp_path = out_path.parent / f"{out_path.name}.tmp"
            temp_path.write_bytes(b"keep-temp")

            result = run_converter(source_path, out_path)

            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, "")
            self.assertEqual(out_path.read_bytes(), b"keep-output")
            self.assertEqual(temp_path.read_bytes(), b"keep-temp")
            self.assertEqual(
                parse_diagnostics(result.stderr),
                [
                    {
                        "severity": "error",
                        "code": "write-failed",
                        "path": "$",
                        "question_ordinal": None,
                        "message": "failed to write output: File exists",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
