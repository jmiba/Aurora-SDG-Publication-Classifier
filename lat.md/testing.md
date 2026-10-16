# Testing

Plain `unittest` suite discovered from the repo root, run in CI with `ruff` and `mypy`. No network access: external services are faked via `FakeSession` or `mock.patch`.

## Running

Standard commands to run the whole suite or a single module locally.

- `python -m unittest discover` — full suite.
- `python -m unittest test_publication_sources` — source adapters, normalization, and deduplication.
- `python -m unittest test_app` — app flow, preview, and fetch-job behavior.
- `python -m unittest test_request_utils` — retry/backoff policy.
- `python -m unittest test_dependency_profiles` — dependency profile boundaries (see below).
- `python -m unittest test_openalex_local_sdg` — local pipeline contract, cache isolation, CSV/XLSX, cancellation, and selector behavior without downloading ML weights.
- `python3 test_scholarly_freeproxies.py "query" --max-results 3` — manual, network-dependent check of the optional `scholarly` free-proxy path (not part of CI).

## Coverage map

- `test_publication_sources.py` — OAI-PMH harvest/resumption tokens and local date filtering (including the first-page-only `from` datestamp window and the `send_from` opt-out), HAL paging, DSpace pagination, OpenAlex abstract reconstruction, DOI/title deduplication with provenance, OA reconciliation, and source parsing validation.
- `test_app.py` — preview pagination and focus search, fetch job start/cancel/poll flow, stale-result invalidation, and Google Scholar status rendering.
- `test_request_utils.py` — `Retry-After` parsing (seconds and HTTP-date forms), capped exponential backoff with jitter, and retryable vs fatal status handling.
- `test_dependency_profiles.py` — asserts the base profile excludes the optional scholarly stack and that importing [[openalex_sdg.py]] never imports `scholarly` eagerly (the fallback is lazy by design).

Verification conventions for this repo are also listed in `AGENTS.md`.

## OpenAlex SDG identifier compatibility

Current UN metadata identifiers and legacy OpenAlex identifiers retain valid assignments, while malformed data stays unavailable and empty arrays remain successful empty results.

[[test_publication_sources.py#OpenAlexSdgIdentifierTests]] verifies both identifier formats, the 0.4 cutoff, invalid goals and scores, and empty versus absent metadata. The source merge and enrichment test also verifies current identifiers populate `openalex_sdgs` with status `classified`.

## OpenAlex authenticated requests

Authenticated pagination keeps the bearer key on every page and out of query URLs, preserves filters, uses the supported page size, and fails on HTTP 401 rather than returning a partial result.

[[test_publication_sources.py#OpenAlexAuthenticationTests]] also covers institution lookup headers and key forwarding through the pipeline without sending OpenAlex credentials to repository requests.

## OpenAlex authentication configuration

Configuration resolves secrets before the environment, requires a key only for OpenAlex runs, hides the key from result identities, and gives useful authentication guidance before or after a request fails.

[[test_app.py#OpenAlexConfigurationTests]] covers key resolution, worker and lineage forwarding, host-scoped HTTP 401/403 notices, and the SWPS source selection showing the missing-key error with the fetch button disabled.
