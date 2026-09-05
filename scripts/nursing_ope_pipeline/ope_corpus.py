#!/usr/bin/env python3
"""Build a structured nursing OPE exam corpus from official PDFs.

This host-side tool intentionally keeps network/PDF parsing out of firmware. It downloads
known official PDFs, extracts text when a local extractor is available, and converts reviewed
exam text plus official answer keys into a canonical JSON source that can later be converted
into `.quiz` artifacts.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = SCRIPT_DIR / "manifest.json"
DEFAULT_WORKDIR = Path(".cache/nursing_ope")
SCHEMA = "reader-quiz.exam-source.v1"
NORMALIZED_SCHEMA = "reader-quiz.exam-normalized.v1"
REVIEW_QUEUE_SCHEMA = "reader-quiz.exam-review-queue.v1"
DEFAULT_NORMALIZED_DIRNAME = "normalized"
EXCLUDED_ADJACENT_CORPUS = "adjacent_enfermeria_excluded_by_default"
CORPUS_POLICIES = {
    "baleares_official_primary": {
        "source_kind": "official_primary",
        "include_by_default": True,
    },
    "fuden_2022_2024_checklist": {
        "source_kind": "official_checklist",
        "include_by_default": True,
    },
    "official_general_nursing_recency_candidate": {
        "source_kind": "official_recency_candidate",
        "include_by_default": True,
    },
    "official_general_nursing_additional_source": {
        "source_kind": "official_additional_source",
        "include_by_default": True,
    },
    EXCLUDED_ADJACENT_CORPUS: {
        "source_kind": "adjacent_official",
        "include_by_default": False,
    },
}


@dataclass(frozen=True)
class ManifestEntry:
    id: str
    jurisdiction: str
    category: str
    year: int
    label: str
    official_urls: dict[str, str] | None
    corpus: str

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ManifestEntry:
        return cls(
            id=raw["id"],
            jurisdiction=raw["jurisdiction"],
            category=raw["category"],
            year=int(raw["year"]),
            label=raw["label"],
            official_urls=raw.get("official_urls"),
            corpus=raw["corpus"],
        )


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_entries(
    manifest: dict[str, Any],
    *,
    include_candidates: bool = False,
    include_additional: bool = False,
) -> Iterable[ManifestEntry]:
    for raw in manifest.get("entries", []):
        yield ManifestEntry.from_json(raw)
    if include_candidates:
        for raw in manifest.get("candidate_2025_or_later", []):
            if raw.get("official_urls"):
                yield ManifestEntry.from_json(raw)
    if include_additional:
        for raw in manifest.get("additional_official_sources", []):
            if raw.get("official_urls"):
                yield ManifestEntry.from_json(raw)


def build_entry_index(manifest: dict[str, Any]) -> dict[str, ManifestEntry]:
    return {
        entry.id: entry
        for entry in iter_entries(
            manifest, include_candidates=True, include_additional=True
        )
    }


def corpus_policy(corpus: str) -> dict[str, Any]:
    policy = CORPUS_POLICIES.get(corpus)
    if policy is None:
        raise RuntimeError(f"unsupported corpus policy for {corpus}")
    return policy


def is_corpus_selected(entry: ManifestEntry, selected_corpora: set[str] | None) -> bool:
    return not selected_corpora or entry.corpus in selected_corpora


def load_parsed_exam_summaries(
    workdir: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    json_dir = workdir / "json"
    paths = sorted(json_dir.glob("*.json"))
    summaries: list[dict[str, Any]] = []
    entry_index = build_entry_index(manifest)
    for path in paths:
        summary = summarize_exam_json(path)
        entry = entry_index.get(summary["id"])
        policy = corpus_policy(entry.corpus) if entry is not None else None
        summary["manifest"] = {
            "id": entry.id if entry else summary["id"],
            "jurisdiction": entry.jurisdiction if entry else None,
            "category": entry.category if entry else None,
            "year": entry.year if entry else None,
            "label": entry.label if entry else None,
            "corpus": entry.corpus if entry else None,
            "official_urls": entry.official_urls if entry else None,
            "source_kind": policy["source_kind"] if policy else None,
            "include_by_default": policy["include_by_default"] if policy else None,
        }
        summaries.append(summary)
    return summaries


def aggregate_by_corpus(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        corpus = summary.get("manifest", {}).get("corpus") or "untracked"
        bucket = grouped.setdefault(
            corpus,
            {
                "corpus": corpus,
                "exams": 0,
                "questions": 0,
                "choices_ok": 0,
                "choices_four": 0,
                "answers_or_anulada": 0,
                "anulada": 0,
                "answer_missing": 0,
                "noted": 0,
            },
        )
        bucket["exams"] += 1
        for key in (
            "questions",
            "choices_ok",
            "choices_four",
            "answers_or_anulada",
            "anulada",
            "answer_missing",
            "noted",
        ):
            bucket[key] += int(summary.get(key, 0))
    return [grouped[key] for key in sorted(grouped)]


def cmd_status(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    entries = list(iter_entries(manifest))
    candidates = [
        ManifestEntry.from_json(raw)
        for raw in manifest.get("candidate_2025_or_later", [])
    ]
    additional = [
        ManifestEntry.from_json(raw)
        for raw in manifest.get("additional_official_sources", [])
    ]
    with_sources = [e for e in entries if e.official_urls]
    missing = [e for e in entries if not e.official_urls]
    candidates_with_sources = [e for e in candidates if e.official_urls]
    additional_with_sources = [e for e in additional if e.official_urls]

    print(f"Manifest: {args.manifest}")
    print(f"Checklist entries: {len(entries)}")
    print(f"Official sources known: {len(with_sources)}")
    print(f"Official sources missing: {len(missing)}")
    print(f"2025-or-later candidates with sources: {len(candidates_with_sources)}")
    print(f"Additional official sources with sources: {len(additional_with_sources)}")
    print()
    for entry in entries:
        marker = "ready" if entry.official_urls else "missing-source"
        print(
            f"{marker:14} {entry.id:48} {entry.corpus:40} {entry.jurisdiction} — {entry.label}"
        )

    if candidates:
        print("\n2025-or-later candidates for recency curation:")
        for entry in candidates:
            marker = "ready-candidate" if entry.official_urls else "candidate-missing"
            print(
                f"{marker:17} {entry.id:45} {entry.corpus:40} {entry.jurisdiction} — {entry.label}"
            )
    if additional:
        print("\nAdditional official sources kept separate from the checklist:")
        for entry in additional:
            marker = "ready-extra" if entry.official_urls else "extra-missing"
            print(
                f"{marker:17} {entry.id:45} {entry.corpus:40} {entry.jurisdiction} — {entry.label}"
            )
    return 0


def download_url(url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "reader-quiz-corpus/0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"download failed for {url}: {exc}") from exc

    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(out)


def cmd_download(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    raw_dir = args.workdir / "raw"
    count = 0
    for entry in iter_entries(
        manifest,
        include_candidates=args.include_candidates,
        include_additional=args.include_additional,
    ):
        if not entry.official_urls:
            continue
        for role in ("exam", "answers"):
            url = entry.official_urls.get(role)
            if not url:
                continue
            out = raw_dir / f"{entry.id}.{role}.pdf"
            if out.exists() and not args.force:
                print(f"skip existing {out}")
                continue
            print(f"download {entry.id} {role}: {url}")
            download_url(url, out)
            count += 1
    print(f"Downloaded {count} file(s) into {raw_dir}")
    return 0


def extract_with_pypdf(pdf: Path) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:  # noqa: BLE001 - preserve broad optional pypdf fallback
        return None

    reader = PdfReader(str(pdf))
    pages: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"\n\n<!-- Page {index} -->\n\n{text}")
    return "".join(pages).strip() + "\n"


def extract_with_pdftotext(pdf: Path) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    proc = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed for {pdf}: {proc.stderr.strip()}")
    return proc.stdout


def extract_with_ocr(pdf: Path) -> str | None:
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return None

    with tempfile.TemporaryDirectory(prefix="reader-quiz-ocr-") as tmp:
        prefix = Path(tmp) / "page"
        render = subprocess.run(
            ["pdftoppm", "-r", "220", "-png", str(pdf), str(prefix)],
            check=False,
            text=True,
            capture_output=True,
        )
        if render.returncode != 0:
            raise RuntimeError(
                f"pdftoppm OCR render failed for {pdf}: {render.stderr.strip()}"
            )

        pages = sorted(Path(tmp).glob("page-*.png"))
        if not pages:
            raise RuntimeError(f"pdftoppm produced no pages for {pdf}")

        parts: list[str] = []
        for index, page in enumerate(pages, start=1):
            ocr = subprocess.run(
                ["tesseract", str(page), "stdout", "-l", "spa+eng", "--psm", "6"],
                check=False,
                text=True,
                capture_output=True,
            )
            if ocr.returncode != 0:
                raise RuntimeError(
                    f"tesseract OCR failed for {pdf} page {index}: {ocr.stderr.strip()}"
                )
            parts.append(f"\n\n<!-- OCR Page {index} -->\n\n{ocr.stdout}")
        return "".join(parts).strip() + "\n"


def extract_pdf_text(pdf: Path, *, use_ocr: bool = True) -> str:
    best_short_text: str | None = None
    for extractor in (extract_with_pypdf, extract_with_pdftotext):
        text = extractor(pdf)
        if text is None:
            continue
        if len(text.strip()) >= 200:
            return text
        best_short_text = text

    if use_ocr:
        ocr_text = extract_with_ocr(pdf)
        if ocr_text is not None:
            return ocr_text

    if best_short_text is not None:
        if not use_ocr:
            return best_short_text
        raise RuntimeError(
            f"native PDF extraction for {pdf} produced only {len(best_short_text.strip())} characters; "
            "install OCR support and re-run extract --force. On Arch Linux: "
            "sudo pacman -S tesseract tesseract-data-spa poppler"
        )
    raise RuntimeError(
        "no PDF extractor available; install pypdf, pdftotext, or OCR support. "
        "On Arch Linux: sudo pacman -S poppler (pdftotext) and optionally "
        "sudo pacman -S tesseract tesseract-data-spa for scanned PDFs"
    )


def cmd_extract(args: argparse.Namespace) -> int:
    raw_dir = args.workdir / "raw"
    text_dir = args.workdir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(raw_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {raw_dir}. Run download first.", file=sys.stderr)
        return 1

    count = 0
    for pdf in pdfs:
        out = text_dir / f"{pdf.stem}.txt"
        if out.exists() and not args.force:
            print(f"skip existing {out}")
            continue
        print(f"extract {pdf}")
        text = extract_pdf_text(pdf, use_ocr=not args.no_ocr)
        out.write_text(text, encoding="utf-8")
        count += 1
    print(f"Extracted {count} file(s) into {text_dir}")
    return 0


QUESTION_START = re.compile(r"(?m)^\s*(\d{1,3})(?:\.-|[.)-])?\s+(?=\S)")
CHOICE_START = re.compile(r"(?m)^\s*([A-Da-d])(?:[.)])?\s+")
ANSWER_TOKEN = re.compile(r"(?i)\b(\d{1,3})\s*(?:[-.:)]\s*)?(A|B|C|D|ANULADA)\b")
INLINE_ANSWER_TOKEN = re.compile(
    r"(?is)\b(\d{1,3})(?:\.-|[.)-])?\s+.*?respuesta\s+correcta\s*:\s*([A-D])\b"
)
ANSWER_LINE = re.compile(r"(?i)\s*Respuesta\s+Correcta\s*:\s*[A-D]\b\s*")


def normalize_ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_answer_key(text: str) -> dict[int, int | None]:
    answers: dict[int, int | None] = {}
    for number, letter in ANSWER_TOKEN.findall(text):
        answers[int(number)] = (
            None if letter.upper() == "ANULADA" else ord(letter.upper()) - ord("A")
        )
    for number, letter in INLINE_ANSWER_TOKEN.findall(text):
        answers[int(number)] = ord(letter.upper()) - ord("A")
    return answers


def split_questions(text: str) -> list[tuple[int, str]]:
    matches = list(QUESTION_START.finditer(text))

    first_seen: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for match in matches:
        number = int(match.group(1))
        if number in seen:
            continue
        first_seen.append((number, match.end(), match.start()))
        seen.add(number)

    sequential: list[tuple[int, int, int]] = []
    expected = 1
    for match in matches:
        number = int(match.group(1))
        if number != expected:
            continue
        sequential.append((number, match.end(), match.start()))
        expected += 1

    accepted = (
        sequential
        if len(sequential) >= max(20, int(len(first_seen) * 0.7))
        else first_seen
    )

    chunks: list[tuple[int, str]] = []
    for idx, (number, start, _match_start) in enumerate(accepted):
        end = accepted[idx + 1][2] if idx + 1 < len(accepted) else len(text)
        chunks.append((number, text[start:end].strip()))
    return chunks


def parse_question(
    number: int, chunk: str, answers: dict[int, int | None]
) -> dict[str, Any]:
    chunk = ANSWER_LINE.sub("\n", chunk)
    choice_matches = list(CHOICE_START.finditer(chunk))
    notes: list[str] = []
    if len(choice_matches) < 2:
        notes.append("Could not detect at least two choices")
        prompt = normalize_ws(chunk)
        choices: list[str] = []
    else:
        prompt = normalize_ws(chunk[: choice_matches[0].start()])
        choices = []
        for idx, match in enumerate(choice_matches):
            start = match.end()
            end = (
                choice_matches[idx + 1].start()
                if idx + 1 < len(choice_matches)
                else len(chunk)
            )
            choices.append(normalize_ws(chunk[start:end]))

    correct_choice = answers.get(number)
    if number not in answers:
        notes.append("No official answer detected")
    elif correct_choice is None:
        notes.append("Official answer marks this question as anulada")
    elif correct_choice >= len(choices):
        notes.append("Official answer points outside parsed choices")

    return {
        "ordinal": number,
        "prompt": prompt,
        "choices": choices,
        "correct_choice": correct_choice,
        "explanation": None,
        "source_page": None,
        "review": {"status": "needs_review", "notes": notes},
    }


def find_entry(manifest: dict[str, Any], exam_id: str) -> ManifestEntry | None:
    return build_entry_index(manifest).get(exam_id)


def build_exam_json(
    manifest: dict[str, Any], exam_id: str, exam_text_path: Path, answer_text_path: Path
) -> dict[str, Any]:
    entry = find_entry(manifest, exam_id)
    if entry is None:
        raise RuntimeError(f"unknown exam id: {exam_id}")

    exam_text = exam_text_path.read_text(encoding="utf-8")
    answer_text = answer_text_path.read_text(encoding="utf-8")
    answers = parse_answer_key(answer_text)
    questions = [
        parse_question(number, chunk, answers)
        for number, chunk in split_questions(exam_text)
    ]

    source_urls = []
    if entry.official_urls:
        source_urls = [
            url
            for key, url in entry.official_urls.items()
            if key in {"exam", "answers", "procedure"}
        ]

    deck = {
        "id": entry.id,
        "title": f"{entry.jurisdiction} — {entry.category} — {entry.label}",
        "jurisdiction": entry.jurisdiction,
        "category": entry.category,
        "year": entry.year,
        "source_urls": source_urls,
    }
    return {"schema": SCHEMA, "deck": deck, "questions": questions}


def write_exam_json(output: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def cmd_parse(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    output = build_exam_json(manifest, args.exam_id, args.exam_text, args.answer_text)
    write_exam_json(output, args.out)
    print(f"Wrote {len(output['questions'])} parsed question(s) to {args.out}")
    print(
        "Review status: all parser output is marked needs_review before .quiz conversion."
    )
    return 0


def summarize_exam_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions", [])
    choices_ok = sum(1 for q in questions if len(q.get("choices", [])) >= 2)
    choices_four = sum(1 for q in questions if len(q.get("choices", [])) == 4)
    answer_ok = sum(
        1
        for q in questions
        if "No official answer detected" not in q.get("review", {}).get("notes", [])
    )
    anulada = sum(
        1
        for q in questions
        if "Official answer marks this question as anulada"
        in q.get("review", {}).get("notes", [])
    )
    answer_missing = sum(
        1
        for q in questions
        if "No official answer detected" in q.get("review", {}).get("notes", [])
    )
    noted = sum(1 for q in questions if q.get("review", {}).get("notes"))
    return {
        "id": data.get("deck", {}).get("id", path.stem),
        "questions": len(questions),
        "choices_ok": choices_ok,
        "choices_four": choices_four,
        "answers_or_anulada": answer_ok,
        "anulada": anulada,
        "answer_missing": answer_missing,
        "noted": noted,
        "path": str(path),
    }


def cmd_parse_all(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    text_dir = args.workdir / "text"
    json_dir = args.workdir / "json"
    exam_texts = sorted(text_dir.glob("*.exam.txt"))
    if not exam_texts:
        print(
            f"No extracted exam text files found in {text_dir}. Run extract first.",
            file=sys.stderr,
        )
        return 1

    failures = 0
    for exam_text in exam_texts:
        exam_id = exam_text.name.removesuffix(".exam.txt")
        answer_text = text_dir / f"{exam_id}.answers.txt"
        if not answer_text.exists():
            print(f"missing answer text for {exam_id}: {answer_text}", file=sys.stderr)
            failures += 1
            continue
        try:
            output = build_exam_json(manifest, exam_id, exam_text, answer_text)
        except RuntimeError as exc:
            print(f"skip {exam_id}: {exc}", file=sys.stderr)
            failures += 1
            continue
        out = json_dir / f"{exam_id}.json"
        write_exam_json(output, out)
        summary = summarize_exam_json(out)
        print(
            f"{summary['id']}: questions={summary['questions']} "
            f"choices_ok={summary['choices_ok']} choices_four={summary['choices_four']} "
            f"answers_or_anulada={summary['answers_or_anulada']} noted={summary['noted']}"
        )
    return 1 if failures else 0


def render_report_payload(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    summaries = load_parsed_exam_summaries(args.workdir, manifest)
    if not summaries:
        raise RuntimeError(
            f"No parsed JSON files found in {args.workdir / 'json'}. Run parse-all first."
        )
    payload: dict[str, Any] = {
        "workdir": str(args.workdir),
        "parsed_exams": len(summaries),
        "exams": summaries,
    }
    if args.by_corpus:
        payload["corpora"] = aggregate_by_corpus(summaries)
    return payload


def cmd_report(args: argparse.Namespace) -> int:
    payload = render_report_payload(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.by_corpus:
        for corpus in payload.get("corpora", []):
            print(
                f"{corpus['corpus']}: exams={corpus['exams']} questions={corpus['questions']} "
                f"choices_ok={corpus['choices_ok']} choices_four={corpus['choices_four']} "
                f"answers_or_anulada={corpus['answers_or_anulada']} anulada={corpus['anulada']} "
                f"answer_missing={corpus['answer_missing']} noted={corpus['noted']}"
            )
        print()

    for summary in payload["exams"]:
        manifest_meta = summary.get("manifest", {})
        corpus = manifest_meta.get("corpus") or "untracked"
        print(
            f"{summary['id']} [{corpus}]: questions={summary['questions']} "
            f"choices_ok={summary['choices_ok']} choices_four={summary['choices_four']} "
            f"answers_or_anulada={summary['answers_or_anulada']} anulada={summary['anulada']} "
            f"answer_missing={summary['answer_missing']} noted={summary['noted']}"
        )
    return 0


def note_to_quality_flags(notes: list[str], choice_count: int) -> list[str]:
    flags: list[str] = []
    if choice_count < 2:
        flags.append("choices_lt_2")
    if choice_count != 4:
        flags.append("choices_not_4")
    if choice_count > 4:
        flags.append("choices_gt_4_truncated")
    for note in notes:
        if note == "Could not detect at least two choices":
            continue
        if note == "No official answer detected":
            flags.append("missing_official_answer")
        elif note == "Official answer marks this question as anulada":
            flags.append("answer_annulled")
        elif note == "Official answer points outside parsed choices":
            flags.append("answer_outside_choice_range")
        else:
            flags.append(f"parser_note:{note}")
    return flags


def normalize_question(question: dict[str, Any], source_path: Path) -> dict[str, Any]:
    raw_choices = question.get("choices", [])
    choices: list[dict[str, Any]] = []
    for index, text in enumerate(raw_choices[:4]):
        choices.append({"id": chr(ord("A") + index), "text": text})

    notes = list(question.get("review", {}).get("notes", []))
    correct_choice = question.get("correct_choice")
    if correct_choice is None:
        answer_status = (
            "annulled"
            if "Official answer marks this question as anulada" in notes
            else "missing"
        )
        choice_id = None
    elif isinstance(correct_choice, int) and 0 <= correct_choice < len(choices):
        answer_status = "official"
        choice_id = chr(ord("A") + correct_choice)
    else:
        answer_status = "out_of_range"
        choice_id = None

    return {
        "ordinal": question.get("ordinal"),
        "prompt": question.get("prompt"),
        "choices": choices,
        "answer": {
            "choice_id": choice_id,
            "status": answer_status,
            "source": "official_answer_key",
        },
        "quality_flags": note_to_quality_flags(notes, len(raw_choices)),
        "raw_refs": {
            "parsed_json_path": str(source_path),
            "source_question_ordinal": question.get("ordinal"),
            "source_prompt": question.get("prompt"),
            "source_choices": raw_choices,
            "source_correct_choice": correct_choice,
            "source_explanation": question.get("explanation"),
            "source_page": question.get("source_page"),
            "review": question.get("review", {}),
        },
    }


def normalize_exam(entry: ManifestEntry, source_path: Path) -> dict[str, Any]:
    policy = corpus_policy(entry.corpus)
    parsed = json.loads(source_path.read_text(encoding="utf-8"))
    questions = [
        normalize_question(question, source_path)
        for question in parsed.get("questions", [])
    ]
    parse_report = summarize_exam_json(source_path)
    parse_report["source_schema"] = parsed.get("schema")

    return {
        "schema": NORMALIZED_SCHEMA,
        "exam": {
            "id": entry.id,
            "title": parsed.get("deck", {}).get("title")
            or f"{entry.jurisdiction} — {entry.category} — {entry.label}",
            "jurisdiction": entry.jurisdiction,
            "category": entry.category,
            "year": entry.year,
            "label": entry.label,
            "corpus": entry.corpus,
            "source_kind": policy["source_kind"],
            "include_by_default": policy["include_by_default"],
            "official_urls": entry.official_urls or {},
        },
        "questions": questions,
        "parse_report": parse_report,
    }


def cmd_normalize(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    entry_index = build_entry_index(manifest)
    json_dir = args.workdir / "json"
    outdir = args.outdir or (args.workdir / DEFAULT_NORMALIZED_DIRNAME)
    paths = sorted(json_dir.glob("*.json"))
    if not paths:
        print(
            f"No parsed JSON files found in {json_dir}. Run parse-all first.",
            file=sys.stderr,
        )
        return 1

    selected_corpora = set(args.corpus) if args.corpus else None
    count = 0
    skipped = 0
    for path in paths:
        exam_id = path.stem
        entry = entry_index.get(exam_id)
        if entry is None:
            print(f"skip {exam_id}: unknown exam id in manifest", file=sys.stderr)
            skipped += 1
            continue
        if not is_corpus_selected(entry, selected_corpora):
            continue
        if (
            entry.corpus == EXCLUDED_ADJACENT_CORPUS
            and not args.include_adjacent_excluded
        ):
            skipped += 1
            continue
        normalized = normalize_exam(entry, path)
        out = outdir / f"{exam_id}.json"
        write_exam_json(normalized, out)
        print(f"normalized {exam_id} -> {out}")
        count += 1

    print(f"Wrote {count} normalized exam(s) into {outdir}")
    if skipped:
        print(f"Skipped {skipped} exam(s)")
    return 0


def question_review_status(question: dict[str, Any]) -> str:
    flags = set(question.get("quality_flags", []))
    answer_status = question.get("answer", {}).get("status")
    choice_count = len(question.get("choices", []))
    if answer_status == "annulled":
        return "excluded_annulled"
    if choice_count == 4 and answer_status == "official" and not flags:
        return "ready"
    return "needs_review"


def summarize_normalized_exam(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    questions = data.get("questions", [])
    status_counts = {"ready": 0, "needs_review": 0, "excluded_annulled": 0}
    for question in questions:
        status_counts[question_review_status(question)] += 1

    if not questions:
        exam_status = "blocked_empty"
    elif status_counts["needs_review"] == 0:
        exam_status = "ready"
    else:
        exam_status = "needs_review"

    exam = data.get("exam", {})
    return {
        "id": exam.get("id", path.stem),
        "path": str(path),
        "corpus": exam.get("corpus"),
        "source_kind": exam.get("source_kind"),
        "include_by_default": exam.get("include_by_default"),
        "status": exam_status,
        "questions": len(questions),
        "ready": status_counts["ready"],
        "needs_review": status_counts["needs_review"],
        "excluded_annulled": status_counts["excluded_annulled"],
    }


def build_review_item(
    exam_summary: dict[str, Any], question: dict[str, Any]
) -> dict[str, Any]:
    prompt = question.get("prompt") or ""
    return {
        "exam_id": exam_summary["id"],
        "corpus": exam_summary.get("corpus"),
        "ordinal": question.get("ordinal"),
        "status": question_review_status(question),
        "quality_flags": question.get("quality_flags", []),
        "answer_status": question.get("answer", {}).get("status"),
        "choice_count": len(question.get("choices", [])),
        "prompt_preview": normalize_ws(prompt)[:180],
        "normalized_path": exam_summary["path"],
    }


def aggregate_review_queue_by_corpus(
    exams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for exam in exams:
        corpus = exam.get("corpus") or "untracked"
        bucket = grouped.setdefault(
            corpus,
            {
                "corpus": corpus,
                "exams": 0,
                "ready_exams": 0,
                "needs_review_exams": 0,
                "blocked_empty_exams": 0,
                "questions": 0,
                "ready_questions": 0,
                "needs_review_questions": 0,
                "excluded_annulled_questions": 0,
            },
        )
        bucket["exams"] += 1
        bucket[f"{exam['status']}_exams"] += 1
        bucket["questions"] += exam["questions"]
        bucket["ready_questions"] += exam["ready"]
        bucket["needs_review_questions"] += exam["needs_review"]
        bucket["excluded_annulled_questions"] += exam["excluded_annulled"]
    return [grouped[key] for key in sorted(grouped)]


def build_review_queue(args: argparse.Namespace) -> dict[str, Any]:
    normalized_dir = args.normalized_dir or (args.workdir / DEFAULT_NORMALIZED_DIRNAME)
    paths = sorted(normalized_dir.glob("*.json"))
    if not paths:
        raise RuntimeError(
            f"No normalized JSON files found in {normalized_dir}. Run normalize first."
        )

    selected_corpora = set(args.corpus) if args.corpus else None
    exams: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        exam = data.get("exam", {})
        corpus = exam.get("corpus")
        if selected_corpora and corpus not in selected_corpora:
            continue
        if corpus == EXCLUDED_ADJACENT_CORPUS and not args.include_adjacent_excluded:
            continue

        exam_summary = summarize_normalized_exam(path, data)
        exams.append(exam_summary)
        for question in data.get("questions", []):
            item = build_review_item(exam_summary, question)
            if args.only_needs_review and item["status"] == "ready":
                continue
            review_items.append(item)

    return {
        "schema": REVIEW_QUEUE_SCHEMA,
        "normalized_dir": str(normalized_dir),
        "exam_count": len(exams),
        "review_item_count": len(review_items),
        "corpora": aggregate_review_queue_by_corpus(exams),
        "exams": exams,
        "review_items": review_items,
    }


def cmd_review_queue(args: argparse.Namespace) -> int:
    queue = build_review_queue(args)
    out = args.out or (args.workdir / "review_queue.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote review queue with {queue['review_item_count']} item(s) from {queue['exam_count']} exam(s) to {out}"
    )
    for corpus in queue["corpora"]:
        print(
            f"{corpus['corpus']}: exams={corpus['exams']} ready_questions={corpus['ready_questions']} "
            f"needs_review_questions={corpus['needs_review_questions']} annulled={corpus['excluded_annulled_questions']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(func=lambda _args: 1)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)

    sub = parser.add_subparsers(required=True)

    status = sub.add_parser("status", help="show manifest coverage")
    status.set_defaults(func=cmd_status)

    download = sub.add_parser("download", help="download official PDFs with known URLs")
    download.add_argument("--force", action="store_true")
    download.add_argument(
        "--include-candidates",
        action="store_true",
        help="also download verified 2025-or-later candidate exams",
    )
    download.add_argument(
        "--include-additional",
        action="store_true",
        help="also download additional official sources kept separate from the checklist",
    )
    download.set_defaults(func=cmd_download)

    extract = sub.add_parser("extract", help="extract text from downloaded PDFs")
    extract.add_argument("--force", action="store_true")
    extract.add_argument(
        "--no-ocr",
        action="store_true",
        help="disable OCR fallback for image-only PDFs; useful to keep batch extraction fast",
    )
    extract.set_defaults(func=cmd_extract)

    parse = sub.add_parser(
        "parse", help="parse one exam text and answer key into canonical JSON"
    )
    parse.add_argument("--exam-id", required=True)
    parse.add_argument("--exam-text", type=Path, required=True)
    parse.add_argument("--answer-text", type=Path, required=True)
    parse.add_argument("--out", type=Path, required=True)
    parse.set_defaults(func=cmd_parse)

    parse_all = sub.add_parser(
        "parse-all", help="parse every extracted exam/answer text pair into JSON"
    )
    parse_all.set_defaults(func=cmd_parse_all)

    report = sub.add_parser("report", help="summarize parsed JSON coverage")
    report.add_argument(
        "--by-corpus",
        action="store_true",
        help="aggregate parsed quality metrics by manifest corpus",
    )
    report.add_argument(
        "--json", action="store_true", help="emit report output as JSON"
    )
    report.set_defaults(func=cmd_report)

    normalize = sub.add_parser(
        "normalize", help="normalize parsed JSON into reader-quiz.exam-normalized.v1"
    )
    normalize.add_argument(
        "--outdir", type=Path, help="output directory (default: <workdir>/normalized)"
    )
    normalize.add_argument(
        "--include-adjacent-excluded",
        action="store_true",
        help="include adjacent_enfermeria_excluded_by_default corpus in normalized output",
    )
    normalize.add_argument(
        "--corpus",
        action="append",
        help="restrict normalization to one corpus; repeat to include multiple corpora",
    )
    normalize.set_defaults(func=cmd_normalize)

    review_queue = sub.add_parser(
        "review-queue", help="build a review queue from normalized JSON files"
    )
    review_queue.add_argument(
        "--normalized-dir",
        type=Path,
        help="input directory (default: <workdir>/normalized)",
    )
    review_queue.add_argument(
        "--out",
        type=Path,
        help="output JSON file (default: <workdir>/review_queue.json)",
    )
    review_queue.add_argument(
        "--corpus",
        action="append",
        help="restrict review queue to one corpus; repeat to include multiple corpora",
    )
    review_queue.add_argument(
        "--include-adjacent-excluded",
        action="store_true",
        help="include adjacent_enfermeria_excluded_by_default corpus in the review queue",
    )
    review_queue.add_argument(
        "--only-needs-review",
        action="store_true",
        help="omit ready questions from review_items while preserving exam summaries",
    )
    review_queue.set_defaults(func=cmd_review_queue)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
