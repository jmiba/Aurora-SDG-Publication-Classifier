# Local OpenAlex comparison

The released local OpenAlex pipeline is the default primary classifier for enriched publication text. Legacy Aurora and the hosted evidence-producing LLM remain explicitly selectable alternatives.

## Inference and isolation

The pinned Qwen embedding model and released v2 head run locally on CPU, with one shared model and serialized inference across sessions.

[[openalex_local_sdg.py#LocalClassifier#classify]] loads the model only when requested, using a bounded Streamlit resource cache. A model-load failure is remembered for that fetch; subsequent fetches can retry. Cancellation is checked while waiting and before cache writes. Inference in progress cannot be interrupted mid-encode.

Install `requirements-openalex-local.txt` alongside base requirements to enable the option in [[app.py#render_model_selector]]. The base dependency profile remains unchanged. The first run downloads the pinned Qwen checkpoint; publication text stays on the app host for this comparison. Abstract-enrichment network behavior still applies. This mode never calls Aurora and needs no Aurora URL.

The tested float32 weights require roughly 2.38 GB before runtime overhead. Standard Community Cloud execution is not qualified; this integration targets a local or sufficiently provisioned server. It does not implement a remote embedding service or reduced-precision variant.

## Input and scoring contract

Title and enriched abstract use the released first-2,000-character formatter, with venue omitted consistently because canonical publications do not contain it.

[[openalex_local_sdg.py#Head]] validates 1,024-dimensional finite, normalized vectors and reproduces the released float32 logistic arithmetic with float64 isotonic interpolation. Membership is selected before rounding using the released float32 0.4 threshold. This differs slightly from the published production UDF near the cutoff; see [[openalex-evaluation#Numerical and local inference checks]].

The unmodified head is vendored with its upstream licence and provenance. The embedding model revision, head checksum, CPU/float32 policy, normalization, truncation, venue omission, and score contract are included in [[openalex_local_sdg.py#CACHE_MODEL]]. Changing the contract or full source text invalidates reuse through [[openalex_local_sdg.py#input_hash]].

## Results and cache

All 17 unrounded scores, thresholded goals, and method identity are cached and exported separately, without invented quotations or review decisions.

[[openalex_sdg.py#_enrich_and_classify_publication]] runs local inference after enrichment and uses it for primary SDG fields and charts. Valid results are `classified` or `below_threshold`; unsuccessful inference is `failed`, and other modes report `not_run`. An empty set means no goal passed the cutoff, not verified absence of SDG relevance.

[[openalex_local_sdg.py#validate_result]] rejects incomplete, nonfinite, out-of-range, and wrong-identity cache entries and rebuilds membership from scores. Both successful statuses use [[cache#SDG results]]; failures are not cached. `local_sdg_response` contains scores and full provenance; `local_sdgs`, `local_sdg_status`, `local_sdg_note`, and `local_sdg_classifier_version` are separate preview/export fields. Notes identify missing abstracts, truncation, and omitted venue. Primary charts use local assignments in the default mode. Primary `sdg_response` retains all 17 scores and provenance, `sdg_source` is `openalex_local`, and `sdg_classifier_version` identifies the pinned contract. Existing local cache entries remain reusable. Old comparison-shaped session results are invalidated by schema version 8.

## Verification

Focused tests cover invalid outputs, cutoff behavior, input identity, failure isolation, cache reuse and invalidation, cancellation, exports, and the Streamlit dependency notice.

[[test_openalex_local_sdg.py#LocalContractTests#test_rejects_incomplete_nonfinite_and_wrong_identity_results]] and the adjacent integration tests use synthetic text and mocked inference. Real embedding and released-vector replay are separate runtime checks; neither establishes accuracy on a human-reviewed ERUA set. Qualification limits remain in [[openalex-evaluation]].

On 8 October 2026, CPU inference on synthetic publication text succeeded with all 17 scores in the isolated evaluation runtime. Adapter replay of all 2,598 released committee vectors matched the stored v2 scores exactly at five decimals. Browser verification confirmed the selector and missing-dependency guidance. Cloud deployment and a live full-corpus fetch were not tested.
