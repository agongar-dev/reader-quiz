# Quiz host tools

The firmware only reads binary `.quiz` artifacts from the SD card. It does **not** parse CSV or JSON on-device.

## Convert canonical JSON to `.quiz`

```bash
python3 tools/quiz/convert_quiz.py source.json output.quiz
```

Canonical UTF-8 JSON input:

```json
{
  "format": "quiz-source-v1",
  "deck": {
    "id": "deck-id",
    "title": "Deck title"
  },
  "questions": [
    {
      "prompt": "Question text",
      "choices": ["A", "B", "C"],
      "correct": 1,
      "explanation": "Optional"
    }
  ]
}
```

## Limits

- `deck.id`: 1-64 UTF-8 bytes
- `deck.title`: 1-96 UTF-8 bytes
- `questions`: 1-10,000 items
- `prompt`: 1-1,024 UTF-8 bytes
- `choices`: 2-6 items, each 1-384 UTF-8 bytes
- `explanation`: 0-1,024 UTF-8 bytes
- no embedded NUL bytes
- duplicate JSON object keys, UTF-8 BOM, unknown fields, wrong types, and invalid `correct` indexes are rejected

## Determinism and identities

Identical validated input produces byte-identical output.

- `deck_identity = sha256("quiz-deck-id-v1\0" + deck.id UTF-8 bytes)[:16]`
- `revision_identity = sha256("quiz-revision-v1\0" + deck_identity + question_count:u32le + canonical question sequence)[:16]`
- canonical question sequence: `prompt:u16le+bytes`, `choice_count:u8`, each `choice:u16le+bytes`, `correct:u8`, `explanation:u16le+bytes`

The converter writes a sibling temp file, flushes, `fsync`s, validates the produced artifact, and only then replaces the requested output path.

## Diagnostics

Failures are emitted as JSON lines on stderr. `question_ordinal` is one-based when present, while `path` remains the zero-based JSON path:

```json
{"severity":"error","code":"unknown-field","path":"$.questions[0].extra","question_ordinal":1,"message":"unknown field 'extra'"}
```

Exit codes:
- `0`: success
- `2`: source/schema validation diagnostics
- `3`: read/write/artifact publication diagnostics

## Deploy to firmware

Copy the resulting artifact to the SD card as `/quiz/<name>.quiz`. Firmware discovery is limited to root-level lowercase `.quiz` files under `/quiz/`.

## Run tests

```bash
python3 -m unittest tools.quiz.tests.test_convert_quiz
cmake -S test -B build/test -G Ninja
ctest --test-dir build/test --output-on-failure -R 'QuizConverterPythonTest|QuizFormatReader'
```
