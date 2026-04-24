# Contributing

Thanks for helping improve reMarkable Zotero CLI.

## Ground Rules

- Do not commit copyrighted books, personal PDFs, EPUBs, reMarkable exports, or
  generated annotation artifacts.
- Keep examples synthetic or public-domain.
- Preserve the core quality rule: target annotations must match the highlighted
  source text exactly whenever the source text is available.
- Prefer explicit `needs_review` output over guessing.
- Keep GitHub Actions and scripts least-privilege and local-first.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Test

```bash
python -m unittest discover -s tests -v
```

## Pull Requests

Before opening a pull request:

- Run the test suite.
- Add tests for matching, extraction, color, geometry, or security behavior when
  the change touches those areas.
- Update `README.md` or `CODEX_REMARKABLE_ZOTERO.md` if user-facing behavior
  changes.
- Keep review artifacts and generated PDFs/EPUBs out of Git.

## Reporting Bugs

Use the bug report template and include:

- Original file type (`pdf` or `epub`)
- Whether the source document has a real text layer
- The generated `*.annotated.review.json` status
- A small synthetic reproduction if possible

Do not attach copyrighted source documents to public issues.
