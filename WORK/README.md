# WORK

Put files for the next conversion here.

Expected input pair:

1. Clean original file: `.pdf` or `.epub`
2. Matching reMarkable export PDF: `.pdf`

Codex should only look for conversion input files in this directory. Files in
this directory are ignored by Git so private documents, generated annotations,
extract JSON, review JSON, and notes do not get committed.

Suggested naming:

- `Original - Book Title.epub`
- `reMarkable - Book Title.pdf`

After a conversion, generated outputs will also appear here unless a different
output path is requested.
