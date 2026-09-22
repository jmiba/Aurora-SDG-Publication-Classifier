# LLM-based SDG classifier (planned)

Design for an optional LLM-based SDG classifier that plugs into the existing enrichment pipeline as just another model option. Proposed, not yet implemented.

The public Aurora service retired the single-label mBERT (`aurora-sdg`) and `osdg` endpoints (both now return HTTP 500), leaving only the two multi-label mBERT backends. An LLM classifier restores model diversity and removes the dependency on the hosted VU service.

## Goals and non-goals

Ship a first-class "model" the user can pick in the same model selector, producing the same `predictions`-style result the rest of the pipeline already consumes.

- Goal: multi-label SDG classification with per-goal confidence, plus an optional single-label (primary SDG) mode, driven by a configurable LLM provider.
- Goal: zero changes to downstream consumers — results flow through the same normalization as [[enrichment#Classification]] and the same SQLite cache.
- Non-goal: replacing the working mBERT models; the LLM model is additive.
- Non-goal: fine-tuning or training our own classifier in this phase.

## Integration points

The classifier call is already isolated, so the LLM model reuses the existing seams.

- [[openalex_sdg.py#classify_text_aurora]] is the single call site for hosted models; an LLM model branches before it (same `model` string from [[openalex_sdg.py#AURORA_MODELS]]).
- [[openalex_sdg.py#format_sdg_predictions]] consumes the Aurora `predictions` envelope; the LLM path should emit that same envelope shape so the UI and exports stay untouched.
- [[openalex_sdg.py#_hash_classification_text]] already keys the [[cache#SDG results]] cache by model + text hash, so LLM results cache independently of mBERT results.
- [[openalex_sdg.py#_RateLimiter]] and `AURORA_MIN_INTERVAL_SECONDS` currently space Aurora calls; an LLM provider needs its own limiter tuned to its rate limits (LLM latency is far higher than mBERT's, so the shared 0.12 s floor is not appropriate).
- The model selector is [[app.py#render_model_selector]] and the selection carries the model id in [[app.py#QuerySelection]]; adding an entry to `AURORA_MODELS` is the only app-side change.

## Model and prompt design

One classification call per canonical publication, temperature 0, with a strict system prompt.

- System prompt: the classifier's role, the 17 SDGs with their UN names (code 1–17), the output contract, and the rule that only goals clearly supported by the text may be returned.
- User prompt: the publication title, then the enriched abstract (the same `text_for_sdg` assembly used for the hosted models), and the instruction to emit JSON only.
- Single-label mode: instruct the model to return exactly the one most-supported SDG (restoring the retired `aurora-sdg` behavior); multi-label mode returns every applicable SDG.
- Pin the model identifier (provider + version) in configuration so results are reproducible across runs; a model upgrade is a configuration change, not a code change.

## Response schema and validation

Force structured output and normalize into the Aurora envelope before anything downstream sees it.

- Require a JSON array of `{"sdg": <int 1–17>, "confidence": <float 0–1>}`; request JSON mode / function calling where the provider supports it.
- Validate and repair: drop unknown codes, clamp scores to 0–1, drop duplicates (keep max score), reject empty results as a classification failure (matching how `invalid json` is noted today).
- Map survivors into the documented `{"predictions": [{"sdg": {"code", "name"}, "prediction": score}]}` envelope so [[openalex_sdg.py#format_sdg_predictions]] renders them unchanged.
- On a malformed or empty response, record a per-row note (e.g. `llm_invalid_json`) instead of failing the whole fetch, mirroring how hosted-model failures are currently reported.

## Provider abstraction

Support at least two interchangeable backends behind one small interface.

- API provider (e.g. an OpenAI-compatible chat-completions endpoint) configured by base URL, model id, and an API key from [[architecture#Secrets and configuration]].
- Local provider (Ollama/vLLM-style HTTP endpoint) for zero per-call cost and full data privacy; same interface, key optional.
- Keep the interface minimal — a `classify(title, abstract, model_id) -> envelope` call — so swapping providers is a secrets change, not a code change.

## Configuration

New optional secrets, documented in `.streamlit/secrets.sample.toml` alongside `aurora_base_url`.

- `llm_classifier_enabled` (bool), `llm_provider_base_url`, `llm_provider_api_key`, `llm_provider_model`, and a label for the selector.
- The LLM model(s) appear in `AURORA_MODELS` only when enabled, so existing deployments see no behavior change.

## Caching, rate limiting, and cost

LLM calls are more expensive and slower than mBERT calls; the plan must account for that before enabling the model.

- Cache reuse is automatic via [[cache#SDG results]]; re-runs over the same corpus stay free.
- Give the LLM path its own `_RateLimiter` (configurable minimum interval) and consider lowering `ENRICHMENT_MAX_WORKERS` for LLM runs, since per-call latency dominates.
- Cost estimate: tokens scale with abstract length × number of classified publications; the 17-SDG system prompt is a fixed per-call overhead.

## Risks and mitigations

The main risks are nondeterminism, provider availability, and quality drift, each with a concrete mitigation.

- Non-determinism: mitigated by temperature 0, pinned model version, and strict JSON validation.
- Provider outage or rate limits: per-row notes instead of hard failures; the mBERT models remain available as fallback.
- Quality drift between models: keep per-model cached results separate (already the case) so users can compare `aurora-sdg-multi` and the LLM model on the same publication set.

## Acceptance criteria

Definition of done before the LLM model is added to `AURORA_MODELS`.

- Unit tests covering envelope normalization, code/score validation, malformed-response notes, and cache-key independence from the mBERT models.
- `lat check` passes and the [[testing]] map covers the new normalization code.
- A small manual comparison run (same publications, mBERT vs LLM) documenting agreement and notable differences.
