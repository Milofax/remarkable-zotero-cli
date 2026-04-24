# reMarkable Zotero CLI

[![CI](https://github.com/Milofax/remarkable-zotero-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Milofax/remarkable-zotero-cli/actions/workflows/ci.yml)

Convert highlights from a reMarkable PDF export back into the clean original
document, then import the result into Zotero.

The workflow expects two files:

1. The clean original document (`.pdf` or `.epub`)
2. The matching reMarkable export PDF that contains the visible highlights

It writes an annotated file in the original format:

- `*.annotated.pdf` for PDF originals
- `*.annotated.epub` plus `*.annotated.notes.md` for EPUB originals

## Why This Exists

reMarkable exports highlight strokes visually, but Zotero needs proper
annotations with clean text, colors, and stable positions. This project bridges
that gap by extracting the highlighted regions from the reMarkable PDF export
and placing equivalent annotations onto the clean source file.

The guiding rule is strict:

> If text A is highlighted in the reMarkable export, text A should be highlighted
> in the target file. Not more, not less.

## Features

- Transfers reMarkable highlights to clean PDF and EPUB originals.
- Preserves supported highlight colors: yellow, green, pink, orange, blue, and
  red fallbacks.
- Writes real PDF highlight annotations for text matches.
- Writes PDF rectangle annotations for non-text image or graphic regions.
- Reconstructs clean annotation text from the original document where possible.
- Supports EPUB visual spans and Calibre/Koreader bookmark metadata for Zotero's
  ebook annotation import path.
- Produces review artifacts so uncertain cases are explicit instead of guessed.

## Limits

- Scanned pages without a usable text layer need OCR first.
- EPUB image or graphic highlights cannot be guaranteed at the exact same visual
  page position because EPUB is reflowable.
- PDF image and table highlights are supported as fixed-page rectangles, but new
  document types should still be visually checked.
- Handwritten notes and typed note semantics are not fully reconstructed yet.
- This tool is local-only; it does not upload documents or call network APIs.

## Installation

Use a virtual environment instead of installing into the system Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Usage

Run the wrapper from the repository root:

```bash
./remarkable-zotero "<original.pdf|epub>" "<remarkable-export.pdf>"
```

Or call the script directly:

```bash
python rm-highlights-to-annotations.py "<original.pdf|epub>" "<remarkable-export.pdf>"
```

Useful review mode:

```bash
./remarkable-zotero "<original.pdf|epub>" "<remarkable-export.pdf>" \
  --extract-json highlights.extract.json
```

Replay reviewed highlights:

```bash
./remarkable-zotero "<original.pdf|epub>" ignored.pdf \
  --extract-in highlights.reviewed.extract.json
```

## Outputs

Each run writes the annotated file plus review metadata next to it:

- `*.annotated.pdf` or `*.annotated.epub`
- `*.annotated.review.json`
- `*.annotated.unmatched.json` if some highlights could not be placed
- `*.annotated.notes.md` for EPUB fallback notes

`*.annotated.review.json` is the quality gate:

- `status = final` means no technical unmatched cases remain.
- `status = needs_review` means the run is not finished.

For EPUB, `final` is not a replacement for visual/text-boundary review. For PDF,
new document families should also be sampled visually against the reMarkable
export, especially for short highlights, colors, tables, figures, and image
regions.

## Zotero Import

For PDF:

1. Add or reload the generated `*.annotated.pdf` in Zotero.
2. Import external annotations if Zotero prompts for it.
3. Use "Add Note from Annotations".

For EPUB:

1. Add the generated `*.annotated.epub` in Zotero.
2. Use "Import Ebook Annotations".
3. Use "Add Note from Annotations".
4. Keep the generated `*.annotated.notes.md` as a fallback audit trail.

## Security And Privacy

- Source documents, generated PDFs/EPUBs, extracts, and review JSON files are
  intentionally ignored by Git.
- Do not commit copyrighted books, personal notes, or generated annotation
  artifacts.
- The CLI reads and writes local files only.
- EPUB ZIP entries are validated before being copied into a new EPUB.
- Dependencies are bounded in `requirements.txt` and monitored through
  Dependabot.
- Security issues should be reported through the process in
  [SECURITY.md](SECURITY.md).

## Development

Install dependencies, then run:

```bash
python -m unittest discover -s tests -v
```

The CI workflow runs the same tests on supported Python versions.

## Project Files

- [rm-highlights-to-annotations.py](rm-highlights-to-annotations.py): main CLI
  implementation
- [remarkable-zotero](remarkable-zotero): small wrapper script
- [CODEX_REMARKABLE_ZOTERO.md](CODEX_REMARKABLE_ZOTERO.md): Codex workflow
  instructions for assisted conversion and review runs
- [CONTRIBUTING.md](CONTRIBUTING.md): contribution guidelines
- [SECURITY.md](SECURITY.md): vulnerability reporting and security policy

## License

MIT. See [LICENSE](LICENSE).
