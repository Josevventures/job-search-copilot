"""Deterministic job-application form extractor (docs/ARCHITECTURE.md, "Form extractor and prep packs").

Never types, uploads, submits or signs in. Run as `python -m prep_extract --url <posting> --out <path>` from the
repo root, with an interpreter that has Playwright and its Chromium installed. Importing this package must stay
free of Playwright/requests: the board imports prep_extract.schema for the stub file.
"""
