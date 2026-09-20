# Testing

Plain `unittest` suite discovered from the repo root, run in CI with `ruff` and `mypy`. No network access: external services are faked via `FakeSession` or `mock.patch`.

## Running

Standard commands to run the whole suite or a single module locally.

- `python -m unittest discover` — full suite.
- `python -m unittest test_publication_sources` — source adapters, normalization, and deduplication.
- `python -m unittest test_app` — app flow, preview, and fetch-job behavior.
- `python -m unittest test_request_utils` — retry/backoff policy.
- `python -m unittest test_dependency_profiles` — dependency profile boundaries (see below).
- `python3 test_scholarly_freeproxies.py "query" --max-results 3` — manual, network-dependent check of the optional `scholarly` free-proxy path (not part of CI).

## Coverage map

- `test_publication_sources.py` — OAI-PMH harvest/resumption tokens and local date filtering, HAL paging, DSpace pagination, OpenAlex abstract reconstruction, DOI/title deduplication with provenance, OA reconciliation, and source parsing validation.
- `test_app.py` — preview pagination and focus search, fetch job start/cancel/poll flow, stale-result invalidation, and Google Scholar status rendering.
- `test_request_utils.py` — `Retry-After` parsing (seconds and HTTP-date forms), capped exponential backoff with jitter, and retryable vs fatal status handling.
- `test_dependency_profiles.py` — asserts the base profile excludes the optional scholarly stack and that importing [[openalex_sdg.py]] never imports `scholarly` eagerly (the fallback is lazy by design).

Verification conventions for this repo are also listed in `AGENTS.md`.
