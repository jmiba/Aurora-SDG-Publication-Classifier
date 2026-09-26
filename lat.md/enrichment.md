# Enrichment

Per-publication abstract enrichment, OpenAlex-first Aurora classification, and optional independent LLM comparison, with caching and bounded concurrency.

The entry point is the [[enrichment#Fetch pipeline]]; per-record work is [[enrichment#Per-publication enrichment]].

## Fetch pipeline

The top-level orchestrator fetches all selected sources, deduplicates, then enriches and classifies concurrently in a bounded thread pool.

- [[openalex_sdg.py#fetch_publications_with_sdg]] — fetches OpenAlex/DSpace/OAI sources, deduplicates via [[sources#Deduplication]], sorts by recency ([[openalex_sdg.py#_publication_recency_key]]), and runs [[openalex_sdg.py#_enrich_and_classify_publication]] across a `ThreadPoolExecutor` of at most `ENRICHMENT_MAX_WORKERS`. Each worker owns a thread-local session. Cancellation is signalled via a `threading.Event`; progress is reported through the `progress_callback`.
- [[openalex_sdg.py#fetch_works_with_sdg]] — backward-compatible OpenAlex-only wrapper.
- Source failures that are retryable HTTP errors or OAI protocol errors are recorded in `FetchStats.source_failures` and the remaining sources continue.

## Per-publication enrichment

One worker enriches and classifies a single publication, preferring cached data and only calling external services when needed.

- [[openalex_sdg.py#_enrich_and_classify_publication]] — enriches abstracts, uses positive OpenAlex Aurora tags, and reuses or calls Aurora for empty or missing lists. It preserves the original OpenAlex response, exports `x_sdgs`, and optionally runs an independent LLM comparison; see [[cache#Canonical publications]] and [[cache#SDG results]].
- [[openalex_sdg.py#_work_cache_changed]] — returns true when persistence would add provenance or a longer abstract.
- [[openalex_sdg.py#_source_record_key_set]] — the set of source-record keys a publication represents, used to detect new provenance.

## Abstract enrichment

Abstracts are retrieved in a fixed fallback order: cached, then Semantic Scholar by DOI, then Google Scholar (SerpApi, else `scholarly`).

- [[openalex_sdg.py#get_abstract_from_semantic_scholar]] — DOI lookup; a 401/403 disables further attempts for the run via [[openalex_sdg.py#_SemanticScholarRunState]].
- [[openalex_sdg.py#get_abstract_from_serpapi_google_scholar]] — SerpApi Google Scholar lookup; matches candidates by title, DOI, and year ([[openalex_sdg.py#_title_matches]], [[openalex_sdg.py#_serpapi_result_doi]], [[openalex_sdg.py#_serpapi_result_year]]).
- [[openalex_sdg.py#get_abstract_from_scholarly]] — optional `scholarly` free-proxy fallback, serialized behind `_SCHOLARLY_LOCK`; one search attempt is [[openalex_sdg.py#_scholarly_search_once]].
- [[openalex_sdg.py#scholarly_fallback_available]] — whether the optional `scholarly` package is installed.
- [[openalex_sdg.py#clean_html_fragment]] — strips HTML tags/entities and normalizes whitespace for stored text.

## Classification

Positive OpenAlex Aurora tags are primary; an empty list triggers a local Aurora recheck.

Missing or malformed OpenAlex fields also trigger the Aurora fallback. Both calls use title plus enriched abstract and a 0.4 cutoff. `sdg_source` distinguishes `aurora_recheck_openalex_empty` from `aurora_fallback`; `openalex_aurora_response` retains the original `[]`. Experimental `x_sdgs` and the selected [[llm-classifier|independent LLM comparison]] stay separate.

No goal at or above the cutoff produces a successful `no_sdg` result with an empty assignment. This is distinct from `failed`, which means no valid classifier response was obtained. The cutoff is fixed in code and must stay aligned with the chart filter and Aurora cache identity.

- [[openalex_sdg.py#openalex_sdg_predictions]] — validates and converts OpenAlex SDG arrays into the presentation envelope; keeps an empty array distinct from a missing field.
- [[openalex_sdg.py#select_aurora_predictions]] — validates self-run Aurora scores and keeps those at or above 0.4.
- [[openalex_sdg.py#classify_text_aurora]] — POSTs the text to the Aurora endpoint for a model, returning `(json, note)`.
- [[llm_sdg.py#classify_publication]] — sends title and abstract to a configured hosted model with structured output, validates evidence excerpts, and returns a decision or a fixed failure category.
- [[openalex_sdg.py#_RateLimiter]] — spaces request starts across workers without serializing response waits; the shared Aurora limiter uses `AURORA_MIN_INTERVAL_SECONDS`, while the LLM limiter adapts to advertised minute quotas.
- [[openalex_sdg.py#format_sdg_predictions]] — renders the Aurora `predictions` envelope into ordered `NN% SDG N (Name)` lines.
- [[openalex_sdg.py#_hash_classification_text]] — SHA-256 of the classified text, stored as `text_hash` to decide cache reuse.
- [[openalex_sdg.py#too_short_for_model]] — skips models that require a minimum word count, configured via `MIN_WORDS_BY_MODEL` (currently empty: the word-count-requiring OSDG model was removed from the public Aurora service).
- [[openalex_sdg.py#FetchStats]] — per-run counters for abstracts, SDG reuse, source failures, and the Semantic Scholar auth status.
