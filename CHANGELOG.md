# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.1.0...HEAD
[1.1.0]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/jmiba/Aurora-SDG-Publication-Classifier/releases/tag/1.0.0
