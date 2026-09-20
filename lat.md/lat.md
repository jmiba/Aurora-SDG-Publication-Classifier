This directory defines the high-level concepts, business logic, and architecture of the Aurora SDG Publication Classifier using markdown. It is managed by [lat.md](https://www.npmjs.com/package/lat.md) — a tool that anchors source code to these definitions. Run `lat check` after editing these files.

- [[architecture]] — Streamlit app shell, secrets, query selection, and the main render/fetch flow.
- [[sources]] — Publication source adapters: OpenAlex, DSpace, OAI-PMH, HAL; normalization and deduplication.
- [[enrichment]] — Abstract enrichment and Aurora SDG classification with caching and rate limiting.
- [[cache]] — SQLite cache schema, canonical publications, provenance, and legacy migration.
- [[fetch-jobs]] — Background fetch jobs, progress publication, and cancellation.
- [[exports]] — Result preview, charts, and CSV/XLSX export.
- [[testing]] — Test map and how to run the verification suite.
