# Sources

Publication-source adapters fetch and normalize records from OpenAlex, DSpace, and OAI-PMH (including the HAL Search API) into one shared publication contract, then merge cross-source duplicates.

Each adapter returns a list of normalized records plus a total. All adapters share [[request_utils.py#request_with_backoff]] for retry/backoff and raise [[publication_sources.py#SourceFetchCancelled]] when the caller requests cancellation. Normalized records carry a `source_record_key`, a `publication_key`, provenance fields, and a `_raw_record` payload for the cache.

## Source configuration parsing

Source entries come from the tracked TOML registries (`dspace_sources.toml`, `oai_sources.toml`) and optional secrets-based additions. Parsing validates identifiers and URL shapes so only well-formed public endpoints are used.

- [[publication_sources.py#parse_dspace_sources]] — parses and validates `DSpaceSource` entries (id, label, base_url, scope, entity types, OpenAlex/ROR ids).
- [[publication_sources.py#parse_oai_sources]] — parses and validates `OaiPmhSource` entries (id, label, base_url, metadata prefix, set, optional HAL `search_api_url`, publication types).
- [[publication_sources.py#DSpaceSource]] and [[publication_sources.py#OaiPmhSource]] — frozen config dataclasses; both expose an `openalex_query_id` property for the linked institution.

## Normalization to the shared contract

Each adapter maps its native shape into the same keys so downstream enrichment and export are source-agnostic.

- [[publication_sources.py#normalize_openalex_work]] — OpenAlex work to contract; reconstructs the abstract from `abstract_inverted_index` via [[publication_sources.py#_reconstruct_openalex_abstract]].
- [[publication_sources.py#normalize_dspace_object]] — DSpace search object to contract; abstract via [[publication_sources.py#_abstract_from_metadata]], OA via [[publication_sources.py#_dspace_oa]].
- [[publication_sources.py#normalize_oai_record]] — OAI-PMH Dublin Core record to contract; type via [[publication_sources.py#_oai_type]], OA via [[publication_sources.py#_oai_oa]], record URL via [[publication_sources.py#_oai_record_url]].
- [[publication_sources.py#normalize_hal_document]] — HAL Search API document to contract; type via [[publication_sources.py#_hal_type]].
- [[publication_sources.py#normalize_doi]] — canonical DOI token used for deduplication.

## Fetching

Each adapter paginates its native API, applies local type/date filtering where the API cannot, and respects cancellation and row limits.

- [[publication_sources.py#fetch_openalex_records]] — cursor-paginated OpenAlex works for a filter.
- [[publication_sources.py#fetch_dspace_records]] — paginated DSpace REST search per entity type.
- [[publication_sources.py#fetch_oai_records]] — OAI-PMH `ListRecords` with resumption-token paging and a local publication-date filter; XML is parsed safely via [[publication_sources.py#_request_xml]] (size cap and DOCTYPE/ENTITY guard).
- [[publication_sources.py#fetch_hal_records]] — HAL Search API paging with Solr date-range filtering.

## Deduplication

Cross-source duplicates are merged by DOI first, then by a stable title/year/first-author hash, preserving provenance from every source.

- [[publication_sources.py#publication_deduplication_key]] — DOI or metadata-based dedup key.
- [[publication_sources.py#deduplicate_publications]] — groups records by key, merges via [[publication_sources.py#_merge_publication]], and records per-source provenance.
- [[publication_sources.py#reconcile_oa_pair]] — keeps the `is_oa` boolean and `oa_status` string internally consistent across merges.
