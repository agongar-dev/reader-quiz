from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/nursing_ope_pipeline/normalized_to_quiz_source.py"
CONVERTER = REPO_ROOT / "tools/quiz/convert_quiz.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "normalized_exam_ready.json"
MAGIC = bytes((0x47, 0x51, 0x55, 0x49, 0x5A, 0x0D, 0x0A, 0x1A))
EXPECTED = {
    "format": "quiz-source-v1",
    "deck": {"id": "baleares-2023-primer-llamamiento", "title": "Baleares Ready Deck"},
    "questions": [
        {"prompt": "¿Cuál es la capital de Illes Balears?", "choices": ["Palma", "Mahón", "Ibiza", "Manacor"], "correct": 0},
        {"prompt": "Selecciona la vía de administración enteral.", "choices": ["Intravenosa", "Oral", "Intradérmica", "Subcutánea"], "correct": 1},
    ],
}


def run_script(script: Path, source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(script), str(source), str(output)], cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture_payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def parse_quiz_artifact(data: bytes) -> dict[str, object]:
    assert data[:8] == MAGIC
    title_length = int.from_bytes(data[38:40], "little")
    title = data[72 : 72 + title_length].decode("utf-8")
    index_offset = int.from_bytes(data[24:28], "little")
    questions = []
    for ordinal in range(int.from_bytes(data[20:24], "little")):
        entry_offset = index_offset + ordinal * 8
        entry = int.from_bytes(data[entry_offset : entry_offset + 4], "little")
        size = int.from_bytes(data[entry_offset + 4 : entry_offset + 8], "little")
        record = data[entry : entry + size]
        cursor, choices = 24, []
        choice_lengths = [int.from_bytes(record[8 + index * 2 : 10 + index * 2], "little") for index in range(record[2])]
        prompt_length = int.from_bytes(record[4:6], "little")
        prompt = record[cursor : cursor + prompt_length].decode("utf-8")
        cursor += prompt_length
        for choice_length in choice_lengths:
            choices.append(record[cursor : cursor + choice_length].decode("utf-8"))
            cursor += choice_length
        questions.append({"prompt": prompt, "choices": choices, "correct": record[3]})
    return {"title": title, "questions": questions}


class NormalizedToQuizSourceE2ETest(unittest.TestCase):
    def test_ready_exam_adapts_and_converts_to_quiz(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            quiz_source = Path(tmp_dir) / "deck.json"
            quiz_artifact = Path(tmp_dir) / "deck.quiz"
            adapted = run_script(SCRIPT, FIXTURE, quiz_source)
            self.assertEqual((adapted.returncode, adapted.stdout, adapted.stderr), (0, "", ""))
            converted = run_script(CONVERTER, quiz_source, quiz_artifact)
            self.assertEqual((converted.returncode, converted.stdout, converted.stderr), (0, "", ""))
            self.assertEqual(json.loads(quiz_source.read_text(encoding="utf-8")), EXPECTED)
            self.assertTrue(quiz_source.read_text(encoding="utf-8").endswith("\n"))
            self.assertEqual(parse_quiz_artifact(quiz_artifact.read_bytes()), {"title": EXPECTED["deck"]["title"], "questions": EXPECTED["questions"]})

    def test_rejects_invalid_inputs_without_output(self) -> None:
        cases = {
            "needs-review": (lambda payload: payload["questions"][0].__setitem__("quality_flags", ["missing_official_answer"]), "needs review"),
            "excluded": (lambda payload: payload["exam"].__setitem__("include_by_default", False), "include_by_default"),
            "bad-mapping": (lambda payload: payload["questions"][0]["answer"].__setitem__("choice_id", "Z"), "choice_id"),
            "empty": (lambda payload: [question.update({"quality_flags": []}) or question["answer"].update({"status": "annulled", "choice_id": None}) for question in payload["questions"]], "No ready questions"),
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            for name, (mutate, message) in cases.items():
                with self.subTest(name=name):
                    source = Path(tmp_dir) / f"{name}.json"
                    output = Path(tmp_dir) / "deck.json"
                    payload = fixture_payload()
                    mutate(payload)
                    write_json(source, payload)
                    output.unlink(missing_ok=True)
                    result = run_script(SCRIPT, source, output)
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertFalse(output.exists())
                    self.assertIn(message, result.stderr)

    def test_rejects_duplicate_keys_and_malformed_choices_without_traceback(self) -> None:
        duplicate = '{"schema":"reader-quiz.exam-normalized.v1","exam":{"id":"deck","title":"Deck","include_by_default":true},"questions":[{"prompt":"Question?","choices":[{"id":"A","text":"One"},{"id":"B","text":"Two"},{"id":"C","text":"Three"},{"id":"D","text":"Four"}],"answer":{"choice_id":"A","status":"official","status":"annulled"},"quality_flags":[]}]}'
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "duplicate.json"
            output = Path(tmp_dir) / "deck.json"
            source.write_text(duplicate, encoding="utf-8")
            duplicate_result = run_script(SCRIPT, source, output)
            self.assertEqual((duplicate_result.returncode, duplicate_result.stdout), (1, ""))
            self.assertFalse(output.exists())
            self.assertIn("Duplicate JSON key 'status'", duplicate_result.stderr)
            for malformed in (None, "not-an-array"):
                with self.subTest(malformed=malformed):
                    payload = fixture_payload()
                    payload["questions"][0]["choices"] = malformed
                    write_json(source, payload)
                    malformed_result = run_script(SCRIPT, source, output)
                    self.assertEqual((malformed_result.returncode, malformed_result.stdout), (1, ""))
                    self.assertFalse(output.exists())
                    self.assertNotIn("Traceback", malformed_result.stderr)
                    self.assertIn("questions[0].choices must be an array", malformed_result.stderr)

    def test_preserves_existing_output_on_validation_failure_and_creates_missing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "needs-review.json"
            output = Path(tmp_dir) / "nested" / "deck.json"
            payload = fixture_payload()
            payload["questions"][0]["quality_flags"] = ["missing_official_answer"]
            write_json(source, payload)
            output.parent.mkdir()
            original = '{"preserved":true}\n'
            output.write_text(original, encoding="utf-8")
            result = run_script(SCRIPT, source, output)
            self.assertEqual((result.returncode, result.stdout), (1, ""))
            self.assertIn("needs review", result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), original)
            fresh = Path(tmp_dir) / "fresh" / "quiz-source" / "deck.json"
            created = run_script(SCRIPT, FIXTURE, fresh)
            self.assertEqual((created.returncode, created.stdout, created.stderr), (0, "", ""))
            self.assertTrue(fresh.parent.is_dir())
            self.assertEqual(json.loads(fresh.read_text(encoding="utf-8"))["format"], "quiz-source-v1")


if __name__ == "__main__":
    unittest.main()
