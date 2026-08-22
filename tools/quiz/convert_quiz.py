#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO

QUIZ_SOURCE_FORMAT = "quiz-source-v1"
QUIZ_MAGIC = bytes((0x47, 0x51, 0x55, 0x49, 0x5A, 0x0D, 0x0A, 0x1A))
QUIZ_VERSION = 1
QUIZ_HEADER_BYTES = 176
QUIZ_INDEX_ENTRY_BYTES = 8
QUIZ_RECORD_HEADER_BYTES = 24
QUIZ_MAX_FILE_BYTES = 64 * 1024 * 1024
QUIZ_MAX_TITLE_BYTES = 96
QUIZ_MAX_DECK_ID_BYTES = 64
QUIZ_MAX_QUESTIONS = 10_000
QUIZ_MIN_CHOICES = 2
QUIZ_MAX_CHOICES = 6
QUIZ_MAX_PROMPT_BYTES = 1_024
QUIZ_MAX_CHOICE_BYTES = 384
QUIZ_MAX_EXPLANATION_BYTES = 1_024
QUIZ_IDENTITY_BYTES = 16
UTF8_BOM = b"\xef\xbb\xbf"


class JSONObject(dict[str, Any]):
    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__()
        self.duplicate_keys: list[str] = []
        seen: set[str] = set()
        for key, value in pairs:
            if key in seen:
                self.duplicate_keys.append(key)
                continue
            seen.add(key)
            self[key] = value


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    path: str
    question_ordinal: int | None
    message: str

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "severity": self.severity,
                "code": self.code,
                "path": self.path,
                "question_ordinal": self.question_ordinal,
                "message": self.message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class QuestionSource:
    prompt: str
    choices: tuple[str, ...]
    correct: int
    explanation: str


@dataclass(frozen=True)
class DeckSource:
    deck_id: str
    title: str
    questions: tuple[QuestionSource, ...]


class ArtifactValidationError(RuntimeError):
    pass


def question_ordinal_for_path(path: str) -> int | None:
    prefix = "$.questions["
    if not path.startswith(prefix):
        return None
    suffix = path[len(prefix) :]
    close = suffix.find("]")
    if close < 0:
        return None
    text = suffix[:close]
    return int(text) + 1 if text.isdigit() else None


def error(code: str, path: str, message: str) -> Diagnostic:
    return Diagnostic("error", code, path, question_ordinal_for_path(path), message)


def append_text_diagnostics(
    diagnostics: list[Diagnostic],
    *,
    path: str,
    value: Any,
    min_bytes: int,
    max_bytes: int | None,
    field_name: str,
) -> str | None:
    if not isinstance(value, str):
        diagnostics.append(error("invalid-type", path, f"expected string, got {type(value).__name__}"))
        return None
    if "\x00" in value:
        diagnostics.append(error("embedded-nul", path, f"{field_name} must not contain NUL bytes"))
        return None
    raw = value.encode("utf-8")
    upper = max_bytes if max_bytes is not None else "∞"
    if len(raw) < min_bytes or (max_bytes is not None and len(raw) > max_bytes):
        diagnostics.append(error("out-of-bounds", path, f"expected {min_bytes}..{upper} UTF-8 bytes, got {len(raw)}"))
        return None
    return value


def validate_source_document(raw: bytes) -> tuple[DeckSource | None, list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    if raw.startswith(UTF8_BOM):
        return None, [error("byte-order-mark", "$", "UTF-8 BOM is not allowed")]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [error("invalid-utf8", "$", f"input is not valid UTF-8: {exc.reason}")]

    try:
        document = json.loads(text, object_pairs_hook=JSONObject)
    except json.JSONDecodeError as exc:
        return None, [error("invalid-json", "$", f"invalid JSON: {exc.msg}")]

    collect_duplicate_key_diagnostics(document, "$", diagnostics)
    if not isinstance(document, JSONObject):
        diagnostics.append(error("invalid-type", "$", f"expected object, got {type(document).__name__}"))
        return None, diagnostics

    deck_value: Any = None
    questions_value: Any = None
    format_value: Any = None
    for key, value in document.items():
        if key == "format":
            format_value = value
            if not isinstance(value, str):
                diagnostics.append(error("invalid-type", "$.format", f"expected string, got {type(value).__name__}"))
            elif value != QUIZ_SOURCE_FORMAT:
                diagnostics.append(error("unsupported-format", "$.format", f"expected '{QUIZ_SOURCE_FORMAT}', got {value!r}"))
        elif key == "deck":
            deck_value = value
        elif key == "questions":
            questions_value = value
        else:
            diagnostics.append(error("unknown-field", f"$.{key}", f"unknown field {key!r}"))

    if format_value is None:
        diagnostics.append(error("missing-field", "$.format", "missing required field 'format'"))
    if deck_value is None:
        diagnostics.append(error("missing-field", "$.deck", "missing required field 'deck'"))
    if questions_value is None:
        diagnostics.append(error("missing-field", "$.questions", "missing required field 'questions'"))

    deck_id: str | None = None
    title: str | None = None
    if deck_value is not None:
        deck_id, title = validate_deck(deck_value, diagnostics)

    questions = validate_questions(questions_value, diagnostics) if questions_value is not None else None
    if diagnostics:
        return None, diagnostics

    assert deck_id is not None and title is not None and questions is not None
    return DeckSource(deck_id, title, tuple(questions)), diagnostics


def collect_duplicate_key_diagnostics(value: Any, path: str, diagnostics: list[Diagnostic]) -> None:
    if isinstance(value, JSONObject):
        for duplicate_key in value.duplicate_keys:
            diagnostics.append(error("duplicate-key", path, f"duplicate key {duplicate_key!r}"))
        for key, child in value.items():
            collect_duplicate_key_diagnostics(child, f"{path}.{key}", diagnostics)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            collect_duplicate_key_diagnostics(child, f"{path}[{index}]", diagnostics)


def validate_deck(value: Any, diagnostics: list[Diagnostic]) -> tuple[str | None, str | None]:
    path = "$.deck"
    if not isinstance(value, JSONObject):
        diagnostics.append(error("invalid-type", path, f"expected object, got {type(value).__name__}"))
        return None, None

    deck_id: str | None = None
    title: str | None = None
    for key, child in value.items():
        child_path = f"{path}.{key}"
        if key == "id":
            deck_id = append_text_diagnostics(
                diagnostics,
                path=child_path,
                value=child,
                min_bytes=1,
                max_bytes=QUIZ_MAX_DECK_ID_BYTES,
                field_name="deck id",
            )
        elif key == "title":
            title = append_text_diagnostics(
                diagnostics,
                path=child_path,
                value=child,
                min_bytes=1,
                max_bytes=QUIZ_MAX_TITLE_BYTES,
                field_name="title",
            )
        else:
            diagnostics.append(error("unknown-field", child_path, f"unknown field {key!r}"))

    if "id" not in value:
        diagnostics.append(error("missing-field", f"{path}.id", "missing required field 'id'"))
    if "title" not in value:
        diagnostics.append(error("missing-field", f"{path}.title", "missing required field 'title'"))
    return deck_id, title


def validate_questions(value: Any, diagnostics: list[Diagnostic]) -> list[QuestionSource] | None:
    path = "$.questions"
    if not isinstance(value, list):
        diagnostics.append(error("invalid-type", path, f"expected array, got {type(value).__name__}"))
        return None
    if len(value) < 1 or len(value) > QUIZ_MAX_QUESTIONS:
        diagnostics.append(error("out-of-bounds", path, f"expected 1..{QUIZ_MAX_QUESTIONS} items, got {len(value)}"))

    questions: list[QuestionSource] = []
    for index, item in enumerate(value):
        question = validate_question(item, index, diagnostics)
        if question is not None:
            questions.append(question)
    return questions


def validate_question(value: Any, index: int, diagnostics: list[Diagnostic]) -> QuestionSource | None:
    path = f"$.questions[{index}]"
    if not isinstance(value, JSONObject):
        diagnostics.append(error("invalid-type", path, f"expected object, got {type(value).__name__}"))
        return None

    start_errors = len(diagnostics)
    prompt: str | None = None
    choices: tuple[str, ...] | None = None
    correct: int | None = None
    explanation = ""

    for key, child in value.items():
        child_path = f"{path}.{key}"
        if key == "prompt":
            prompt = append_text_diagnostics(
                diagnostics,
                path=child_path,
                value=child,
                min_bytes=1,
                max_bytes=QUIZ_MAX_PROMPT_BYTES,
                field_name="prompt",
            )
        elif key == "choices":
            choices = validate_choices(value=child, path=child_path, diagnostics=diagnostics)
        elif key == "correct":
            if isinstance(child, bool):
                diagnostics.append(error("invalid-type", child_path, "expected integer, got bool"))
            elif not isinstance(child, int):
                diagnostics.append(error("invalid-type", child_path, f"expected integer, got {type(child).__name__}"))
            else:
                correct = child
        elif key == "explanation":
            text = append_text_diagnostics(
                diagnostics,
                path=child_path,
                value=child,
                min_bytes=0,
                max_bytes=QUIZ_MAX_EXPLANATION_BYTES,
                field_name="explanation",
            )
            if text is not None:
                explanation = text
        else:
            diagnostics.append(error("unknown-field", child_path, f"unknown field {key!r}"))

    for required in ("prompt", "choices", "correct"):
        if required not in value:
            diagnostics.append(error("missing-field", f"{path}.{required}", f"missing required field '{required}'"))

    if isinstance(correct, int) and isinstance(choices, tuple):
        if correct < 0 or correct >= len(choices):
            diagnostics.append(error("out-of-bounds", f"{path}.correct", f"expected 0..{len(choices) - 1}, got {correct}"))

    if prompt is None or choices is None or correct is None or len(diagnostics) != start_errors:
        return None
    return QuestionSource(prompt, choices, correct, explanation)


def validate_choices(*, path: str, value: Any, diagnostics: list[Diagnostic]) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        diagnostics.append(error("invalid-type", path, f"expected array, got {type(value).__name__}"))
        return None
    if len(value) < QUIZ_MIN_CHOICES or len(value) > QUIZ_MAX_CHOICES:
        diagnostics.append(error("out-of-bounds", path, f"expected {QUIZ_MIN_CHOICES}..{QUIZ_MAX_CHOICES} items, got {len(value)}"))

    choices: list[str] = []
    for index, item in enumerate(value):
        text = append_text_diagnostics(
            diagnostics,
            path=f"{path}[{index}]",
            value=item,
            min_bytes=1,
            max_bytes=QUIZ_MAX_CHOICE_BYTES,
            field_name="choice",
        )
        if text is not None:
            choices.append(text)
    return tuple(choices) if len(choices) == len(value) else None


def build_quiz_artifact(source: DeckSource) -> tuple[bytes | None, list[Diagnostic]]:
    records: list[bytes] = []
    index_entries: list[tuple[int, int]] = []
    index_bytes = len(source.questions) * QUIZ_INDEX_ENTRY_BYTES
    records_offset = QUIZ_HEADER_BYTES + index_bytes
    next_record_offset = records_offset

    for question in source.questions:
        prompt = question.prompt.encode("utf-8")
        explanation = question.explanation.encode("utf-8")
        choice_bytes = [choice.encode("utf-8") for choice in question.choices]
        payload_length = len(prompt) + len(explanation) + sum(len(choice) for choice in choice_bytes)
        descriptor = bytearray(QUIZ_RECORD_HEADER_BYTES)
        descriptor[0:2] = QUIZ_RECORD_HEADER_BYTES.to_bytes(2, "little")
        descriptor[2] = len(choice_bytes)
        descriptor[3] = question.correct
        descriptor[4:6] = len(prompt).to_bytes(2, "little")
        descriptor[6:8] = len(explanation).to_bytes(2, "little")
        for index, raw in enumerate(choice_bytes):
            descriptor[8 + index * 2 : 10 + index * 2] = len(raw).to_bytes(2, "little")
        descriptor[20:24] = payload_length.to_bytes(4, "little")
        record = bytes(descriptor) + prompt + b"".join(choice_bytes) + explanation
        index_entries.append((next_record_offset, len(record)))
        records.append(record)
        next_record_offset += len(record)

    file_size = next_record_offset
    if file_size > QUIZ_MAX_FILE_BYTES:
        return None, [error("out-of-bounds", "$", f"artifact size {file_size} exceeds {QUIZ_MAX_FILE_BYTES} bytes")]

    deck_identity = compute_deck_identity(source.deck_id)
    revision_identity = compute_revision_identity(deck_identity, source.questions)
    title = source.title.encode("utf-8")
    header = bytearray(QUIZ_HEADER_BYTES)
    header[0:8] = QUIZ_MAGIC
    header[8:10] = QUIZ_VERSION.to_bytes(2, "little")
    header[10:12] = QUIZ_HEADER_BYTES.to_bytes(2, "little")
    header[16:20] = file_size.to_bytes(4, "little")
    header[20:24] = len(source.questions).to_bytes(4, "little")
    header[24:28] = QUIZ_HEADER_BYTES.to_bytes(4, "little")
    header[28:32] = index_bytes.to_bytes(4, "little")
    header[32:36] = records_offset.to_bytes(4, "little")
    header[36:38] = QUIZ_INDEX_ENTRY_BYTES.to_bytes(2, "little")
    header[38:40] = len(title).to_bytes(2, "little")
    header[40:56] = deck_identity
    header[56:72] = revision_identity
    header[72 : 72 + len(title)] = title

    index = bytearray(index_bytes)
    for ordinal, (record_offset, record_bytes) in enumerate(index_entries):
        entry_offset = ordinal * QUIZ_INDEX_ENTRY_BYTES
        index[entry_offset : entry_offset + 4] = record_offset.to_bytes(4, "little")
        index[entry_offset + 4 : entry_offset + 8] = record_bytes.to_bytes(4, "little")

    artifact = bytes(header) + bytes(index) + b"".join(records)
    validate_quiz_artifact_bytes(artifact)
    return artifact, []


def compute_deck_identity(deck_id: str) -> bytes:
    return hashlib.sha256(b"quiz-deck-id-v1\0" + deck_id.encode("utf-8")).digest()[:QUIZ_IDENTITY_BYTES]


def compute_revision_identity(deck_identity: bytes, questions: Iterable[QuestionSource]) -> bytes:
    question_list = list(questions)
    digest = hashlib.sha256()
    digest.update(b"quiz-revision-v1\0")
    digest.update(deck_identity)
    digest.update(len(question_list).to_bytes(4, "little"))
    for question in question_list:
        digest.update(length_prefixed_utf8(question.prompt))
        digest.update(bytes((len(question.choices),)))
        for choice in question.choices:
            digest.update(length_prefixed_utf8(choice))
        digest.update(bytes((question.correct,)))
        digest.update(length_prefixed_utf8(question.explanation))
    return digest.digest()[:QUIZ_IDENTITY_BYTES]


def length_prefixed_utf8(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(2, "little") + raw


def validate_quiz_artifact_bytes(data: bytes) -> None:
    if len(data) < QUIZ_HEADER_BYTES:
        raise ArtifactValidationError("truncated header")
    if len(data) > QUIZ_MAX_FILE_BYTES:
        raise ArtifactValidationError("file too large")
    if data[:8] != QUIZ_MAGIC:
        raise ArtifactValidationError("invalid magic")
    if le16(data, 8) != QUIZ_VERSION:
        raise ArtifactValidationError("unsupported version")
    if le16(data, 10) != QUIZ_HEADER_BYTES or le32(data, 12) != 0:
        raise ArtifactValidationError("invalid fixed header fields")

    declared_size = le32(data, 16)
    question_count = le32(data, 20)
    index_offset = le32(data, 24)
    index_bytes = le32(data, 28)
    records_offset = le32(data, 32)
    index_entry_size = le16(data, 36)
    title_length = le16(data, 38)
    if declared_size != len(data):
        raise ArtifactValidationError("declared file size mismatch")
    if not (1 <= question_count <= QUIZ_MAX_QUESTIONS):
        raise ArtifactValidationError("question count out of bounds")
    if index_offset != QUIZ_HEADER_BYTES:
        raise ArtifactValidationError("invalid index offset")
    if index_entry_size != QUIZ_INDEX_ENTRY_BYTES:
        raise ArtifactValidationError("invalid index entry size")
    if title_length < 1 or title_length > QUIZ_MAX_TITLE_BYTES:
        raise ArtifactValidationError("title length out of bounds")
    if data[40:56] == bytes(QUIZ_IDENTITY_BYTES) or data[56:72] == bytes(QUIZ_IDENTITY_BYTES):
        raise ArtifactValidationError("zero identity")
    if data[72 + title_length : 168] != bytes(QUIZ_MAX_TITLE_BYTES - title_length) or data[168:176] != bytes(8):
        raise ArtifactValidationError("non-zero padding")
    decode_artifact_text(data[72 : 72 + title_length])

    expected_index_bytes = question_count * QUIZ_INDEX_ENTRY_BYTES
    if index_bytes != expected_index_bytes or records_offset != QUIZ_HEADER_BYTES + expected_index_bytes:
        raise ArtifactValidationError("inconsistent offsets")

    records_limit = len(data)
    next_record_offset = records_offset
    for ordinal in range(question_count):
        entry_offset = index_offset + ordinal * QUIZ_INDEX_ENTRY_BYTES
        record_offset = le32(data, entry_offset)
        record_bytes = le32(data, entry_offset + 4)
        if record_offset != next_record_offset or record_offset + record_bytes > records_limit:
            raise ArtifactValidationError("invalid index entry")
        validate_quiz_record(data[record_offset : record_offset + record_bytes])
        next_record_offset = record_offset + record_bytes

    if next_record_offset != records_limit:
        raise ArtifactValidationError("record area mismatch")


def validate_quiz_record(record: bytes) -> None:
    if len(record) < QUIZ_RECORD_HEADER_BYTES:
        raise ArtifactValidationError("truncated question record")
    record_header_size = le16(record, 0)
    choice_count = record[2]
    correct = record[3]
    prompt_length = le16(record, 4)
    explanation_length = le16(record, 6)
    choice_lengths = [le16(record, 8 + index * 2) for index in range(QUIZ_MAX_CHOICES)]
    payload_length = le32(record, 20)

    if record_header_size != QUIZ_RECORD_HEADER_BYTES:
        raise ArtifactValidationError("invalid record header size")
    if choice_count < QUIZ_MIN_CHOICES or choice_count > QUIZ_MAX_CHOICES:
        raise ArtifactValidationError("choice count out of bounds")
    if correct >= choice_count:
        raise ArtifactValidationError("correct choice out of bounds")
    if prompt_length < 1 or prompt_length > QUIZ_MAX_PROMPT_BYTES:
        raise ArtifactValidationError("prompt out of bounds")
    if explanation_length > QUIZ_MAX_EXPLANATION_BYTES:
        raise ArtifactValidationError("explanation out of bounds")

    expected_payload = prompt_length + explanation_length
    for index, choice_length in enumerate(choice_lengths):
        if index < choice_count:
            if choice_length < 1 or choice_length > QUIZ_MAX_CHOICE_BYTES:
                raise ArtifactValidationError("choice out of bounds")
            expected_payload += choice_length
        elif choice_length != 0:
            raise ArtifactValidationError("inactive choice length must be zero")

    if payload_length != expected_payload or len(record) != QUIZ_RECORD_HEADER_BYTES + payload_length:
        raise ArtifactValidationError("payload length mismatch")

    cursor = QUIZ_RECORD_HEADER_BYTES
    decode_artifact_text(record[cursor : cursor + prompt_length])
    cursor += prompt_length
    for choice_length in choice_lengths[:choice_count]:
        decode_artifact_text(record[cursor : cursor + choice_length])
        cursor += choice_length
    decode_artifact_text(record[cursor : cursor + explanation_length])


def decode_artifact_text(raw: bytes) -> str:
    if b"\x00" in raw:
        raise ArtifactValidationError("embedded NUL in text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactValidationError(f"invalid UTF-8: {exc.reason}") from exc


def le16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def le32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def write_artifact(output_path: Path, artifact: bytes) -> list[Diagnostic]:
    temp_path = output_path.with_name(f"{output_path.name}.tmp")
    created_temp = False
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        created_temp = True
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(artifact)
                handle.flush()
                os.fsync(handle.fileno())
            read_back = temp_path.read_bytes()
            if read_back != artifact:
                raise ArtifactValidationError("read-back bytes differ from the generated artifact")
            validate_quiz_artifact_bytes(read_back)
            os.replace(temp_path, output_path)
            return []
        except Exception:
            if created_temp and temp_path.exists():
                temp_path.unlink()
            raise
    except ArtifactValidationError as exc:
        return [error("artifact-validation-failed", "$", str(exc))]
    except OSError as exc:
        if created_temp and temp_path.exists():
            temp_path.unlink()
        return [error("write-failed", "$", f"failed to write output: {exc.strerror or str(exc)}")]


def emit_diagnostics(stream: TextIO, diagnostics: Iterable[Diagnostic]) -> None:
    for diagnostic in diagnostics:
        stream.write(diagnostic.to_json_line())
        stream.write("\n")


def convert(source_path: Path, output_path: Path) -> list[Diagnostic]:
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        return [error("read-failed", "$", f"failed to read source: {exc.strerror or str(exc)}")]

    source, diagnostics = validate_source_document(raw)
    if diagnostics:
        return diagnostics

    assert source is not None
    artifact, build_diagnostics = build_quiz_artifact(source)
    if build_diagnostics:
        return build_diagnostics
    assert artifact is not None
    return write_artifact(output_path, artifact)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert canonical quiz-source-v1 JSON into a .quiz artifact.")
    parser.add_argument("source", type=Path, help="input UTF-8 JSON file")
    parser.add_argument("output", type=Path, help="output .quiz path")
    return parser.parse_args(argv)


def exit_code_for_diagnostics(diagnostics: Iterable[Diagnostic]) -> int:
    publication_codes = {"read-failed", "write-failed", "artifact-validation-failed"}
    return 3 if any(diagnostic.code in publication_codes for diagnostic in diagnostics) else 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    diagnostics = convert(args.source, args.output)
    if diagnostics:
        emit_diagnostics(sys.stderr, diagnostics)
        return exit_code_for_diagnostics(diagnostics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
