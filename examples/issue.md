# Task: add a greeting function

## Goal

Add `greeting()` in `src/example.py`. It returns `Hello from the example project`.

## Allowed paths

src/example.py

## Acceptance

- Positive case: importing `greeting()` returns the specified string.
- Nearest negative case: the configured check fails if the file is missing, invalid Python, or returns the wrong string.
- Evidence required: the configured behavioral check passes and the diff contains only the allowed file.

## Checks

The configured check imports the file and asserts the exact return value. The controller runs it before publication and the maintainer repeats it during review.

## Dependencies and limits

This is a disposable demonstration. It needs no service or private data. Keep credentials and private data out of the issue.
