# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.8] - 2026-09-26

### Added

- Export the experimental OpenAlex `x_sdgs` field and optional independent hosted LLM decisions in separate comparison columns.
- Preserve the original OpenAlex Aurora response and mark local rechecks of empty OpenAlex SDG lists in the export.

### Changed

- Prefer positive OpenAlex Aurora assignments. Recheck empty or unavailable OpenAlex SDG lists with `aurora-sdg-multi` using the title and available abstract, retaining scores at or above 0.4.
- Explain that the 0.4 cutoff and classifier limitations can leave publications without an assigned SDG; document the current fixed threshold configuration.

## [1.1.7] - 2026-09-22

### Removed

- The `aurora-sdg` (single-label mBERT) and `osdg` classifier models, because
  the public Aurora service now returns HTTP 500 for both endpoints and no
  longer advertises them. The two working multi-label mBERT models
  (`aurora-sdg-multi`, `elsevier-sdg-multi`) remain the default options.

## [1.1.6] - 2026-09-21

### Changed

- Generalized the publication-period pruning from the HAL Search API source to
  all generic OAI-PMH sources: the first `ListRecords` request now sends the
  selected period start as the OAI-PMH `from` datestamp parameter, skipping
  old, unchanged records server-side (e.g. AccedaCRIS harvest shrinks from
  ~133k to ~37k records for a 2025+ window). Local publication-date filtering
  stays authoritative; `until` is never sent, and sources can opt out with
  `send_from = false` in `oai_sources.toml`.

## [1.1.5] - 2026-09-20

### Added

- A `lat.md/` concept directory (managed by [lat.md](https://www.npmjs.com/package/lat.md)) anchoring the architecture, source adapters, enrichment pipeline, cache, fetch jobs, exports, and test map to the source code.

### Changed

- Refactored the optional `scholarly` Google Scholar fallback into a single-attempt helper plus a thin retry loop, removing a redundant in-loop failure return without changing retry behavior.
- Persist SDG classifications through the same `with conn:` transaction pattern as the other cache upserts instead of an explicit commit.
- Stop reading institution-search results from session state twice per rerun in the institution selector.
- Drop a redundant byte copy when decoding the CSV fallback in completed-result payloads.

### Fixed

- Report the source's returned record count as the OAI-PMH total when the server omits `completeListSize`, instead of the locally filtered and limit-trimmed result count, matching the DSpace and HAL fetchers.

### Removed

- Dead code: the unused `BASE_WORKS` and `PER_PAGE` constants in `openalex_sdg.py`, the `openalex_sdg.abbreviate_authors` duplicate (the app uses its own copy), and the unused public cache functions `upsert_publication` and `upsert_source_record`.

## [1.1.4] - 2026-09-08

### Changed

- Run publication fetches in a background worker and poll their locked progress
  from a Streamlit fragment, so the Cancel button can signal a running fetch.
- Limit the publication-focus selector to the current preview page or at most
  100 title, author, and DOI search matches instead of building one option for
  every result row.
- Treat OAI-PMH protocol errors from one endpoint like retryable HTTP source
  failures: report the unavailable source and continue with the remaining
  selected sources. Local source configuration errors still stop the run.
- Reserve Aurora request slots while locked but sleep outside the rate-limiter
  lock, allowing enrichment workers to wait independently.
- Split the DSpace row limit across the selected entity types so one source no
  longer over-fetches by the number of entity types.
- Require Streamlit 1.49 or newer, the minimum version that supports the
  scoped rerun used by the live fetch progress panel.

### Fixed

- Guard the one-time Open Access consistency repair with a cache metadata
  marker and serialize first-time SQLite connection initialization.
- Keep the background fetch job registry in a dedicated imported module so
  Streamlit's per-rerun script re-execution no longer orphans a running
  fetch with a "background fetch state is unavailable" error.
- Reach 100% progress when a completed source returns fewer rows than the
  requested limit, URL-encode Semantic Scholar DOI path values, and distinguish
  invalid Aurora JSON from HTTP errors.

### Removed

- Removed unused authorship, author-token, single-origin network, and defensive
  OpenAlex artistic-work code paths.

## [1.1.3] - 2026-09-08

### Changed

- Capped export filenames at `MAX_EXPORT_FILENAME_LENGTH` (150 chars) so they
  can be opened in Excel despite OS path limits. Overlong source and
  publication-type lists are truncated by dropping whole ids from the end
  (noted with a `+N` suffix), while the institution, model, date, and limit
  segments are preserved.

## [1.1.2] - 2026-08-24

### Fixed

- Transient OAI-PMH server failures now skip the unavailable source after
  retries, continue fetching other selected sources, and report the failure in
  the fetch summary.
- Scoped the Paris 8 HAL OAI-PMH source to its institution-specific open-access
  collection, avoiding an unbounded harvest of the full HAL index.
- Use HAL's Search API for Paris 8 queries so publication-date filters are
  applied server-side and short periods do not require harvesting every OAI-PMH
  page.
- Normalized mixed timezone-aware and timezone-naive publication dates before
  applying chart filters, preventing HAL Paris book-chapter queries from
  failing while rendering the co-affiliation graph.

## [1.1.1] - 2026-08-22

### Added

- Added MRU CRIS, the NBU Scholar Electronic Repository, and AccedaCRIS as
  configured publication sources with verified OpenAlex and ROR identifiers.

### Changed

- Sorted configured repository sources alphabetically in the source selector,
  while keeping OpenAlex first.

### Fixed

- Corrected the HAL OAI-PMH endpoint and switched AccedaCRIS from its
  incompatible legacy DSpace REST entry to its OAI-PMH endpoint. The legacy
  AccedaCRIS entry remains disabled because the service rejects automated
  requests with HTTP 403.

## [1.1.0] - 2026-08-21

### Added

- Generic public OAI-PMH harvesting with Dublin Core normalization, safe XML
  parsing, resumption-token pagination, local publication-date filtering, and
  the Europa-Universität Viadrina OPUS repository registry entry.

### Fixed

- Co-affiliation networks now use the full combined result instead of a focused
  preview row, retain edges for every selected repository institution instead
  of filtering around only the first one, and report when records such as
  OAI-PMH Dublin Core entries lack affiliation pairs.
- Corrected the README workflow diagram syntax and abstract-fallback routing so
  the complete flow renders reliably on GitHub.

## [1.0.0] - 2026-08-21

### Added

- Multi-source publication retrieval from OpenAlex and configurable public
  DSpace repositories, including the SWPS SHARE registry entry.
- Source-aware normalization, deterministic deduplication, canonical
  provenance, and local SQLite caching.
- OpenAlex institution search, ROR support, institution lineage expansion,
  multi-institution queries, and publication-type filtering including software.
- Missing-abstract enrichment through Semantic Scholar and optional Google
  Scholar providers, with user-visible authentication and fallback status.
- Aurora SDG classification with cache reuse, concurrent processing, request
  rate limiting, cancellation, and title-only low-confidence annotations.
- CSV and XLSX exports with source provenance and SDG results.
- Publication, open-access, author, SDG, and interactive 3D co-affiliation
  visualizations, including label filtering and optional secondary edges.
- Ruff, mypy, and unit-test checks in GitHub Actions.

### Changed

- Consolidated HTTP retries into a shared exponential-backoff policy with
  jitter and `Retry-After` support.
- Made the less-reliable `scholarly` and free-proxy integration an optional
  dependency profile while keeping SerpApi as the preferred Google Scholar
  provider.
- Replaced the handwritten OOXML export implementation with pandas and
  openpyxl.
- Avoided redundant cache writes for unchanged publications and preserved
  richer abstracts and accumulated source-record provenance.
- Decomposed Streamlit query, fetch, cancellation, invalidation, chart, and
  export orchestration into testable helpers.

### Fixed

- Corrected final-attempt HTTP 429 reporting and Semantic Scholar invalid-key
  warnings without exposing credentials.
- Prevented duplicate classified-record writes and unnecessary cache timestamp
  churn on stable reruns.
- Improved graph edge visibility, zoom-consistent node boundaries, smooth
  sphere rendering, foreground labels, adaptive light/dark label styling, and
  stable empty-filter canvas behavior.
- Corrected model selection when multiple models share the same description.

### Security

- Kept local Streamlit credentials and runtime SQLite cache files out of the
  tracked source tree.
- Required a non-placeholder contact address before OpenAlex queries can run.

[Unreleased]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.8...HEAD
[1.1.8]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.7...1.1.8
[1.1.7]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.6...1.1.7
[1.1.6]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.5...1.1.6
[1.1.5]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.4...1.1.5
[1.1.4]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.3...1.1.4
[1.1.3]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.2...1.1.3
[1.1.2]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.1...1.1.2
[1.1.1]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.0...1.1.1
[1.1.0]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/releases/tag/1.0.0
