# Cache

Local SQLite cache (WAL mode, one process-wide connection under a re-entrant lock) storing canonical publications, source records, and SDG results for reuse across fetches.

The schema is created and migrated in [[cache_db.py#_init_schema]]; the single connection is managed by [[cache_db.py#_get_conn]] and closed by [[cache_db.py#close_connection]] (used by tests for clean shutdown).

## Schema and migration

Tables are created idempotently on first connection; one-time data repairs run only once, guarded by `cache_meta` markers.

- [[cache_db.py#_init_schema]] — creates `works` (legacy), `sdg_results` (legacy), `canonical_works`, `source_records`, `sdg_results_v2`, and `cache_meta`; runs the one-time legacy import and OA-consistency repair, each guarded by a `cache_meta` marker.
- [[cache_db.py#_migrate_legacy_rows]] — copies legacy OpenAlex `works` rows into `canonical_works`/`source_records` without altering legacy data.
- [[cache_db.py#_repair_oa_consistency]] — repairs contradictory `is_oa`/`oa_status` pairs written by earlier versions.

## Canonical publications

A canonical publication merges the richest known metadata for a deduplicated work without reducing cached fields on later runs.

- [[cache_db.py#upsert_work]] — persists one canonical work and its source records in a single transaction, then syncs provenance via [[cache_db.py#_sync_canonical_provenance]].
- [[cache_db.py#_canonical_payload]] — builds the canonical row, preferring longer titles/abstracts/authors and merging semicolon lists and affiliation/provenance JSON ([[cache_db.py#_merge_semicolon]], [[cache_db.py#_merge_json_records]]).
- [[cache_db.py#_execute_upsert_publication]] — the `INSERT ... ON CONFLICT DO UPDATE` for `canonical_works`.
- [[cache_db.py#get_cached_publication]] (alias [[cache_db.py#get_cached_work]]) — read back one canonical publication by key.

## Source records and provenance

Each fetched source record is stored verbatim (with its raw JSON) and linked to its canonical publication; the canonical row's source summary is recomputed from these rows.

- [[cache_db.py#_source_record_payload]] — builds a `source_records` row from a source record, storing the raw record JSON.
- [[cache_db.py#_execute_upsert_source_record]] — upserts one source record.
- [[cache_db.py#_sync_canonical_provenance]] — recomputes the canonical source summary (sources, ids, URLs, count, provenance JSON) from all persisted source records.

## SDG results

Self-run classifications are cached by publication, method identity, and input hash.

The Aurora fallback and empty-list recheck share a cache identity for their title-plus-abstract input and 0.4 cutoff; export provenance distinguishes them. LLM identities include provider endpoint, model, and prompt. OpenAlex Aurora and `x_sdgs` come from the current fetched source record.

- [[cache_db.py#upsert_sdg_result]] — persists a classification keyed by (publication key, model) with the input `text_hash`; foreign-keyed to `canonical_works`.
- [[cache_db.py#get_cached_sdg_result]] — reads the cached classification for reuse decisions in [[enrichment#Classification]].
