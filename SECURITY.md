# Security Policy

## Supported Version

The `main` branch is the supported development line.

## Reporting A Vulnerability

Please do not publish sensitive vulnerability details in a public issue.

If GitHub private vulnerability reporting is available for this repository, use
that. Otherwise, open a minimal public issue that says you want to report a
security issue, without exploit details or private files.

Useful report details:

- Affected command or workflow
- File type involved (`pdf`, `epub`, or GitHub workflow)
- Whether untrusted input is required
- Minimal synthetic reproduction steps

## Security Expectations

- The CLI must not upload documents or call external APIs during conversion.
- Source documents and generated artifacts must stay out of Git.
- EPUB ZIP member names and manifest paths are treated as untrusted input.
- GitHub Actions should use the minimum permissions needed.
- Dependency updates are monitored through Dependabot.
