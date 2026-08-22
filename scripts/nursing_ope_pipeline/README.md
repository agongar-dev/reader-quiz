# Nursing OPE corpus pipeline

Host-side tooling for building a structured quiz corpus from official nursing OPE exam PDFs.

The firmware must not parse PDFs or general JSON on-device. This pipeline keeps network, PDF extraction,
manual correction, and source normalization on the development machine and emits a canonical JSON source that
can later be converted into the firmware `.quiz` artifact.

## Corpus separation

Do not mix sources silently. Every manifest entry has a `corpus` value:

- `baleares_official_primary`: same administration/category target; this is the highest-value corpus.
- `official_general_nursing_recency_candidate`: official general nursing exams from other administrations, used only
  when the Baleares corpus is too small or when curating the latest 37 official exams.
- `official_general_nursing_additional_source`: official general nursing exams discovered while researching, kept
  separate from the FUDEN checklist because they are extra calls, postponed calls, or otherwise non-checklist items.
- `fuden_2022_2024_checklist`: a checklist derived from FUDEN's public index; it is not content provenance and each
  entry still needs an official exam/answer source before use.
- `adjacent_enfermeria_excluded_by_default`: official nursing-adjacent material that is useful for analysis but must
  not be merged into the general Enfermero/a deck unless explicitly approved.

## Source priority

1. Official administration PDFs: exam booklet plus answer key.
2. Official college/union mirrors only when they link or mirror the original administration PDF.
3. Commercial indexes, such as FUDEN's 2022-2024 recopilatorio index, only as a checklist for which official
   PDFs still need to be located.

## Workflow

```bash
# Show corpus coverage and missing official URLs
python3 scripts/nursing_ope_pipeline/ope_corpus.py status

# Download all 37-checklist entries that already have official URLs
python3 scripts/nursing_ope_pipeline/ope_corpus.py download

# Also download verified 2025-or-later candidates for replacing older entries during recency curation
python3 scripts/nursing_ope_pipeline/ope_corpus.py download --include-candidates

# Also download additional official sources that are deliberately kept outside the 37-checklist
python3 scripts/nursing_ope_pipeline/ope_corpus.py download --include-additional

# Extract text from downloaded PDFs when a local extractor is available
python3 scripts/nursing_ope_pipeline/ope_corpus.py extract

# Parse every extracted exam/answer pair into canonical JSON
python3 scripts/nursing_ope_pipeline/ope_corpus.py parse-all

# Summarize parsed coverage joined with manifest metadata
python3 scripts/nursing_ope_pipeline/ope_corpus.py report

# Aggregate parsed quality metrics by corpus
python3 scripts/nursing_ope_pipeline/ope_corpus.py report --by-corpus

# Emit the same report as JSON for downstream tooling
python3 scripts/nursing_ope_pipeline/ope_corpus.py report --by-corpus --json

# Normalize parsed JSON into reader-quiz.exam-normalized.v1 files
python3 scripts/nursing_ope_pipeline/ope_corpus.py normalize

# Include the adjacent corpus only when explicitly requested
python3 scripts/nursing_ope_pipeline/ope_corpus.py normalize --include-adjacent-excluded

# Restrict normalization to one or more corpora
python3 scripts/nursing_ope_pipeline/ope_corpus.py normalize \
  --corpus baleares_official_primary \
  --corpus official_general_nursing_recency_candidate

# Build a review queue from normalized files
python3 scripts/nursing_ope_pipeline/ope_corpus.py review-queue --only-needs-review

# Restrict the review queue to the primary Baleares corpus
python3 scripts/nursing_ope_pipeline/ope_corpus.py review-queue \
  --corpus baleares_official_primary \
  --only-needs-review

# Or parse one exam text plus one answer-key text into canonical JSON
python3 scripts/nursing_ope_pipeline/ope_corpus.py parse \
  --exam-id baleares-2023-primer-llamamiento \
  --exam-text .cache/nursing_ope/text/baleares-2023-primer-llamamiento.exam.txt \
  --answer-text .cache/nursing_ope/text/baleares-2023-primer-llamamiento.answers.txt \
  --out .cache/nursing_ope/json/baleares-2023-primer-llamamiento.json
```

Parsed JSON stays in `.cache/nursing_ope/json`; normalized JSON stays in `.cache/nursing_ope/normalized` by default.
The review queue is written to `.cache/nursing_ope/review_queue.json` by default. These cache outputs are host-side
working artifacts and should not be committed.


## Current curation status

After the last source sweep, the manifest contains:

- 37 checklist entries from the FUDEN public index.
- 13 checklist entries with official source URLs.
- 24 checklist entries still missing official sources.
- 7 verified 2025-or-later official recency candidates.
- 3 additional official sources kept separate from the checklist.

The current normalized/review workflow intentionally stops before `.quiz` conversion. The next human-owned step is
reviewing `.cache/nursing_ope/review_queue.json`, fixing or excluding low-quality items, and only then feeding reviewed
normalized JSON into a future `.quiz` converter.

## Review queue shape

`review-queue` writes `reader-quiz.exam-review-queue.v1`:

```json
{
  "schema": "reader-quiz.exam-review-queue.v1",
  "normalized_dir": ".cache/nursing_ope/normalized",
  "exam_count": 22,
  "review_item_count": 549,
  "corpora": [
    {
      "corpus": "baleares_official_primary",
      "exams": 2,
      "ready_questions": 80,
      "needs_review_questions": 44,
      "excluded_annulled_questions": 0
    }
  ],
  "exams": [
    {
      "id": "baleares-2024-segundo-llamamiento",
      "corpus": "baleares_official_primary",
      "status": "needs_review",
      "questions": 72,
      "ready": 69,
      "needs_review": 3
    }
  ],
  "review_items": [
    {
      "exam_id": "baleares-2024-segundo-llamamiento",
      "ordinal": 38,
      "status": "needs_review",
      "quality_flags": ["missing_official_answer"],
      "answer_status": "missing",
      "choice_count": 4,
      "prompt_preview": "...",
      "normalized_path": ".cache/nursing_ope/normalized/baleares-2024-segundo-llamamiento.json"
    }
  ]
}
```

## PDF extraction dependencies

The script tries these extractors, in order:

1. `pypdf` Python package.
2. `pdftotext` command-line tool.

On Arch Linux, prefer system packages instead of `pip`:

```bash
# Recommended baseline: provides pdftotext and pdftoppm
sudo pacman -S poppler

# Needed for scanned/image-only PDFs or PDFs whose text layer extracts empty
sudo pacman -S tesseract tesseract-data-spa

# Alternative: makes the pypdf Python import available to system Python
sudo pacman -S python-pypdf
```

`pipx` is useful for Python applications, but it does not make library imports like `pypdf` available to
`python3 scripts/nursing_ope_pipeline/ope_corpus.py`. Use `poppler`/`pdftotext`, `python-pypdf`, or a local
virtualenv if you want Python-package isolation.

The extractor automatically falls back to `pdftoppm + tesseract` OCR when native text extraction returns less
than 200 characters. Re-run with `--force` after installing OCR support to replace short/empty text outputs:

```bash
python3 scripts/nursing_ope_pipeline/ope_corpus.py extract --force
```

Some scanned official PDFs are large and OCR can be slow. To keep a batch moving and leave image-only PDFs as
short/empty text for later manual OCR, disable OCR fallback:

```bash
python3 scripts/nursing_ope_pipeline/ope_corpus.py extract --no-ocr
```

If no extractor is installed, `download` still works and `extract` fails with an actionable message.

## Canonical parsed JSON shape

The parser emits a host-side editable source format, not the final firmware artifact:

```json
{
  "schema": "reader-quiz.exam-source.v1",
  "deck": {
    "id": "baleares-2023-primer-llamamiento",
    "title": "Baleares — Enfermero/enfermera — Examen 2023. Primer llamamiento",
    "jurisdiction": "Baleares",
    "category": "Enfermero/enfermera",
    "year": 2023,
    "source_urls": []
  },
  "questions": [
    {
      "ordinal": 1,
      "prompt": "...",
      "choices": ["...", "...", "...", "..."],
      "correct_choice": 0,
      "explanation": null,
      "source_page": null,
      "review": { "status": "needs_review", "notes": [] }
    }
  ]
}
```

All parser output starts as `needs_review`. Official PDFs differ enough that a human review pass is expected
before converting to `.quiz`.

## Normalized JSON shape

`normalize` does not create `.quiz` files. It reshapes parsed JSON into a corpus-aware, host-side normalized form:

```json
{
  "schema": "reader-quiz.exam-normalized.v1",
  "exam": {
    "id": "baleares-2023-primer-llamamiento",
    "title": "Baleares — Enfermero/enfermera — Examen 2023. Primer llamamiento",
    "jurisdiction": "Baleares",
    "category": "Enfermero/enfermera",
    "year": 2023,
    "label": "Examen 2023. Primer llamamiento",
    "corpus": "baleares_official_primary",
    "source_kind": "official_primary",
    "include_by_default": true,
    "official_urls": {
      "exam": "https://...",
      "answers": "https://...",
      "procedure": "https://..."
    }
  },
  "questions": [
    {
      "ordinal": 1,
      "prompt": "...",
      "choices": [
        { "id": "A", "text": "..." },
        { "id": "B", "text": "..." },
        { "id": "C", "text": "..." },
        { "id": "D", "text": "..." }
      ],
      "answer": {
        "choice_id": "A",
        "status": "official",
        "source": "official_answer_key"
      },
      "quality_flags": [],
      "raw_refs": {
        "parsed_json_path": ".cache/nursing_ope/json/baleares-2023-primer-llamamiento.json",
        "source_question_ordinal": 1,
        "source_prompt": "...",
        "source_choices": ["...", "...", "...", "..."],
        "source_correct_choice": 0,
        "source_explanation": null,
        "source_page": null,
        "review": { "status": "needs_review", "notes": [] }
      }
    }
  ],
  "parse_report": {
    "id": "baleares-2023-primer-llamamiento",
    "questions": 60,
    "choices_ok": 60,
    "choices_four": 60,
    "answers_or_anulada": 60,
    "anulada": 0,
    "answer_missing": 0,
    "noted": 0,
    "path": ".cache/nursing_ope/json/baleares-2023-primer-llamamiento.json",
    "source_schema": "reader-quiz.exam-source.v1"
  }
}
```

By default, normalization excludes `adjacent_enfermeria_excluded_by_default`. Pass
`--include-adjacent-excluded` only when that corpus is intentionally being studied as adjacent material.
