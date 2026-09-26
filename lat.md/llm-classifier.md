# Independent LLM SDG classifier

Publication SDG classifier that reads title and abstract directly and makes its own zero, one, or multiple SDG decisions. It does not use Aurora predictions or scores.

This is a separate classification method, not an Aurora quality-control pass. The September 2026 Aurora QC prompt and workbook are useful for failure modes and review examples, but their machine-generated decisions are not verified reference labels.

## Goals and boundaries

Give users a selectable, open-weights LLM comparison alongside the primary Aurora classification.

- Classify each canonical publication from its title and available abstract, without using Aurora or `x_sdgs` results as LLM input or decision evidence.
- Permit an empty SDG set when the supplied text does not support a substantive SDG dimension.
- Preserve the validated decision, evidence excerpts, and model and prompt identity separately from Aurora results so comparisons remain possible.
- Keep primary `sdg_*` fields based on positive OpenAlex Aurora tags, the empty-list Aurora recheck, or the missing-field fallback; export LLM decisions only in `llm_*` columns.
- Do not use the QC workbook as ground truth or fine-tune a model in the first implementation.

## Model selection

Use a hosted inference endpoint for an open-weights model. Choose the production model by task-specific evaluation, not parameter count or general benchmark position.

- Initial configured model: `Qwen/Qwen3.8-27B`, subject to availability of the exact checkpoint at a provider with structured output, suitable data handling, and acceptable cost and latency.
- Challenger: `Qwen/Qwen3.5-397B-A17B`, a larger mixture-of-experts model with 397B total and 17B active parameters. Compare it on the same reviewed publications before paying for it on every row.
- Pin the provider, endpoint model identifier, checkpoint or revision where exposed, inference parameters, and prompt version for each run. A provider alias alone is insufficient for reproducibility.
- Keep the provider behind a small HTTP adapter so the classification rules and result schema do not depend on one host.

## Evidence and decision contract

The classifier assesses the 17 SDGs from publication text, without a pre-ranked candidate list.

- Input is the title and abstract after the existing source and enrichment steps; mark explicitly when only a title is available.
- For every assigned SDG, require a brief, exact excerpt from the supplied title or abstract. Do not infer content from authors, venue, affiliation, publication type, or an Aurora score.
- Accept zero, one, or multiple SDGs. Require each assigned SDG to have its own substantive content dimension; a general keyword, setting, method, or incidental mention is insufficient.
- Review title-only decisions with extra care. Generic software releases, incidental locations, and passing mentions do not qualify. Ambiguous cases may be flagged for manual review.
- Assess substantive findings about unequal opportunity, employment, civic participation, or renewable-energy policy even when the title is indirect. These are candidate dimensions, not automatic keyword matches.
- Treat any model-generated confidence as an uncalibrated explanation aid, not an Aurora probability or a threshold for automatic acceptance. Omit confidence from the first decision schema unless calibrated on reviewed labels.

## Output and validation

Validate a structured response before it enters the cache or export path.

- Return an explicit status (`classified`, `no_sdg`, or `manual_review`), SDG codes in 1–17, and evidence for each assignment. `no_sdg` is a successful decision with an empty SDG list; malformed output and provider failure are separate errors. [[llm_sdg.py#validate_decision]] enforces the contract.
- Reject duplicate or out-of-range codes, contradictory status and SDG combinations, unsupported evidence fields, and responses that do not satisfy the schema. Do not silently clamp invented scores into apparently valid results.
- Store a hash of title, abstract, provider endpoint, model identifier, effective thinking mode, and prompt, plus the validated response. [[llm_sdg.py#LlmConfig#identity]] and [[llm_sdg.py#input_hash]] change cache identity when these change; hosted providers may not expose an exact checkpoint revision.
- Adapt the shared UI and export boundary to display a valid empty decision and evidence. The current Aurora `predictions` envelope and `[[openalex_sdg.py#format_sdg_predictions]]` are presentation contracts for Aurora scores, not the independent classifier's truth model.

## Integration

Reuse publication retrieval and abstract enrichment; run the LLM comparison only when selected.

- The selector at [[app.py#render_model_selector]] offers the optional independent LLM comparison. [[openalex_sdg.py#_enrich_and_classify_publication]] keeps Aurora primary and dispatches the LLM independently of its predictions; the Aurora fallback may run in the same fetch if OpenAlex tags are unavailable.
- Keep provider credentials in the local secrets configuration documented through `.streamlit/secrets.sample.toml`. Never put keys, source text, or raw provider failures in routine logs.
- The LLM method uses a two-worker limit, request spacing, timeout, and transient retries. It adapts request spacing to a provider's advertised minute limit and honors `ratelimit-reset` on HTTP 429. Qwen3.8 uses direct nonthinking answers by default to reduce latency; a secret can restore reasoning and changes the cache identity. Other models keep their provider default. A failed row remains distinguishable from `no_sdg` and does not stop other rows.
- Keep Aurora and LLM cache entries separate and expose the LLM provider, model, prompt, and cache identity in its comparison columns. [[cache#SDG results]] stores LLM entries under a prompt/provider/model identity distinct from Aurora models.

## Qualification before rollout

Use reviewed publication examples to test whether the model makes defensible, reproducible assignments.

- Build a stratified, human-reviewed set across languages, disciplines, publication types, missing abstracts, no-SDG cases, and difficult overlaps such as SDG 10/16 and 13/15. Reviewers see source text, not model output, before labeling.
- Measure per-SDG precision and recall, no-SDG false positives and false negatives, agreement between reviewers, structured-output failure rate, latency, and cost per publication. Review disagreements, including low-score Aurora cases, without treating Aurora or the QC workbook as the answer key.
- Run the 27B candidate and the 397B challenger on the same set with a fixed prompt and comparable provider settings. Promote the larger model only if its improvement on the SDG task justifies its cost and latency.
- Verify the selected provider's exact model identity, data retention and location terms, structured-output support, throughput, and failure behavior before sending a full corpus.
