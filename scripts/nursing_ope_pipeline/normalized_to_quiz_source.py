from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

INPUT_SCHEMA = "reader-quiz.exam-normalized.v1"
OUTPUT_FORMAT = "quiz-source-v1"


class ConversionError(ValueError):
    pass


def fail(message: str) -> None:
    raise ConversionError(message)


def require(value: Any, ok: bool, context: str, expected: str) -> Any:
    if not ok:
        fail(f"{context} must be {expected}")
    return value


def require_object(value: Any, context: str) -> dict[str, Any]:
    return require(value, isinstance(value, dict), context, "an object")


def require_string(value: Any, context: str) -> str:
    return require(value, isinstance(value, str) and bool(value), context, "a non-empty string")


def require_bool(value: Any, context: str) -> bool:
    return require(value, isinstance(value, bool), context, "a boolean")


def require_list(value: Any, context: str) -> list[Any]:
    return require(value, isinstance(value, list), context, "an array")


def convert_question(question: Any, *, index: int) -> dict[str, Any] | None:
    item = require_object(question, f"questions[{index}]")
    answer = require_object(item.get("answer"), f"questions[{index}].answer")
    status = require_string(answer.get("status"), f"questions[{index}].answer.status")
    if status == "annulled":
        return None

    raw_choices = require_list(item.get("choices"), f"questions[{index}].choices")
    flags = require_list(item.get("quality_flags", []), f"questions[{index}].quality_flags")
    if status != "official" or len(raw_choices) != 4 or flags:
        fail(f"Question {index + 1} needs review and cannot be converted")

    prompt = require_string(item.get("prompt"), f"questions[{index}].prompt")
    answer_choice_id = require_string(answer.get("choice_id"), f"questions[{index}].answer.choice_id")
    choices, choice_ids = [], []
    for choice_index, choice in enumerate(raw_choices):
        choice_item = require_object(choice, f"questions[{index}].choices[{choice_index}]")
        choice_id = require_string(choice_item.get("id"), f"questions[{index}].choices[{choice_index}].id")
        choice_text = require_string(choice_item.get("text"), f"questions[{index}].choices[{choice_index}].text")
        if choice_id in choice_ids:
            fail(f"Question {index + 1} has duplicate choice_id {choice_id!r}")
        choice_ids.append(choice_id)
        choices.append(choice_text)
    if answer_choice_id not in choice_ids:
        fail(f"Question {index + 1} answer.choice_id {answer_choice_id!r} does not match any choice_id")
    return {"prompt": prompt, "choices": choices, "correct": choice_ids.index(answer_choice_id)}


def reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"Duplicate JSON key {key!r} is not allowed")
        result[key] = value
    return result


def load_json_document(source: Path) -> Any:
    try:
        text = source.read_text(encoding="utf-8")
        return json.loads(text, object_pairs_hook=reject_duplicate_object_keys)
    except FileNotFoundError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"Failed to read {source}: {exc.strerror or exc}")
    except json.JSONDecodeError as exc:
        fail(f"Invalid JSON: {exc.msg}")


def publish_output(output: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
    except OSError as exc:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        fail(f"Failed to write {output}: {exc.strerror or exc}")


def convert_payload(document: Any) -> dict[str, Any]:
    root = require_object(document, "document")
    schema = require_string(root.get("schema"), "schema")
    if schema != INPUT_SCHEMA:
        fail(f"Unsupported schema {schema!r}; expected {INPUT_SCHEMA!r}")

    exam = require_object(root.get("exam"), "exam")
    if not require_bool(exam.get("include_by_default"), "exam.include_by_default"):
        fail("exam.include_by_default must be true for quiz export")

    questions = [
        converted
        for index, question in enumerate(require_list(root.get("questions"), "questions"))
        if (converted := convert_question(question, index=index)) is not None
    ]
    if not questions:
        fail("No ready questions remain after removing annulled questions")
    return {
        "format": OUTPUT_FORMAT,
        "deck": {
            "id": require_string(exam.get("id"), "exam.id"),
            "title": require_string(exam.get("title"), "exam.title"),
        },
        "questions": questions,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert reader-quiz.exam-normalized.v1 JSON into quiz-source-v1 JSON.")
    parser.add_argument("input", type=Path, help="input normalized JSON path")
    parser.add_argument("output", type=Path, help="output quiz-source JSON path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        publish_output(args.output, convert_payload(load_json_document(args.input)))
    except ConversionError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
