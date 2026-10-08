# Released OpenAlex SDG pipeline evaluation

The 8 October 2026 evaluation reproduces the released classifier's benchmarks and tests local inference. It supports a local comparison trial, but does not qualify a replacement for the evidence-producing independent classifier.

## Decision

Use the released OpenAlex head as a candidate comparison model; retain the current independent classifier until reviewed ERUA examples establish the tradeoffs.

The released pipeline is runnable locally and its numerical benchmarks reproduce. Its target-oriented definition is useful, and its embedding-plus-head architecture avoids a hosted decision call for every publication. It returns 17 scores and a thresholded set of goals, without quotations, explicit uncertainty routing, or an explanation of an empty result. It therefore cannot directly satisfy [[llm-classifier#Evidence and decision contract]] or [[llm-classifier#Output and validation]].

No app code, classifier selection, project dependencies, secrets, or runtime cache contents were changed. The evaluation does not authorize automatic adoption. A future comparison must remain separate from Aurora and the fetched OpenAlex tags.

This statement describes the evaluation stage. The subsequently requested optional adapter is now documented in [[openalex-local]]; it retains the qualification boundary above.

## Release identity and sources

The inspected upstream checkout is release 1.0.0 at commit `36260360e35b31cb48f2b3a2374fc295ad57ba40`; the shipped model is `sdg-jev-head-v2`.

- [OpenAlex SDGs repository](https://github.com/ourresearch/openalex-sdgs)
- [Reproduction instructions](https://github.com/ourresearch/openalex-sdgs/blob/36260360e35b31cb48f2b3a2374fc295ad57ba40/REPRODUCE.md)
- [Embedding input and implementation](https://github.com/ourresearch/openalex-sdgs/blob/36260360e35b31cb48f2b3a2374fc295ad57ba40/classifier/embed.py)
- [Local classification head](https://github.com/ourresearch/openalex-sdgs/blob/36260360e35b31cb48f2b3a2374fc295ad57ba40/classifier/head.py)
- [Training and evaluation Jev requests](https://github.com/ourresearch/openalex-sdgs/blob/36260360e35b31cb48f2b3a2374fc295ad57ba40/classifier/jev_request.py)

The v2 head is 209,629 bytes with SHA-256 `0b1357dcb19215205faf7381c224159cdac6a733fd1d08725f2935d6b938f52a`. Both released heads matched their manifest hashes. The Qwen embedding checkpoint used locally is `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`.

Upstream README states MIT for code and CC0 for data and head weights, with CC BY 4.0 for external benchmark sources. However, its LICENSE footer describes a different student model, a multilingual-e5-small fine-tune released under MIT. That inconsistency should be clarified before redistributing the head. Qwen's embedding model has its own Apache 2.0 licence.

## Pipeline and input policy

Jev supplies training labels; local inference uses Qwen embeddings and 17 logistic-regression heads with isotonic calibration, without calling Jev.

The released embedding text concatenates `Title:`, optional `Abstract:`, and optional `Venue:`, then keeps the first 2,000 characters. Qwen3-Embedding-0.6B produces a normalized 1,024-dimensional vector without a query instruction prefix. The head applies a sigmoid and a per-goal isotonic map; goals scoring at least 0.4 are returned.

The training Jev request can include venue and up to 6,000 abstract characters. Its benchmarked direct-Jev request omits venue and limits the abstract to 3,000 characters. These are different input contracts. The embedding may lose late abstract findings and, for long abstracts, the venue entirely. Its scores are not validated probabilities of correctness against human judgments.

The current independent classifier reads title and enriched abstract, requires exact excerpts, and does not use venue as decision evidence. The canonical publication cache has no venue column. Reusing the head for repository-only records therefore needs an explicit missing-venue policy rather than substituting affiliation or repository names. Its CLI also requires numeric OpenAlex-shaped IDs; a project adapter must preserve canonical keys independently of those CLI IDs.

## Reproduced accuracy evidence

Replay verifies the published calculations from stored predictions and labels. It does not constitute a fresh model evaluation or a reviewed ERUA benchmark.

On the 2,000-work committee set, counting unresolved judgments as negative:

| Method | Precision | Recall | F1 |
| --- | ---: | ---: | ---: |
| Aurora served tags, threshold 0.4 | 0.2850 | 0.3052 | 0.2947 |
| Jev direct, threshold 0.5 | 0.7616 | 0.7180 | 0.7391 |
| Released head v2, threshold 0.4 | 0.6808 | 0.7234 | 0.7015 |

The paired bootstrap 95% interval for head-minus-Jev F1 is -0.0636 to -0.0114. The head retains recall but loses precision. Its gains over Aurora reproduce under both single judges. Training IDs number 200,000; none overlap the 2,000-work set. Stored judge rows have no parsing or committee integrity problems.

There are 171 unresolved goal judgments across 11 works. Counting them all as positive gives head F1 0.6409, Jev 0.6688, and Aurora 0.2720. The headline 0.7015 is one uncertainty bracket, not the only supported estimate. The reported 98.87% judge agreement includes many negative decisions; kappa is 0.7675. SDG 15 v2 was selected using one half of this evaluation set and confirmed on the other, so its final full-set estimate is not wholly untouched test evidence.

On 8,767 human-voted Aurora survey pairs, head v2 reproduces F1 75.5% (precision 77.9%, recall 73.3%) versus Aurora 63.6%. Under the alternate protocol that treats unasked goals as false positives, head micro-F1 is 55.5% versus Aurora 61.4%. Protocol choice matters: unasked goals are not verified negatives. On OSDG-CD, the released script evaluates Jev and Aurora, but not the embedding head because those excerpts do not have shipped work vectors.

The head's committee F1 is 0.7162 for English and 0.6681 for other/unknown languages. Title-only precision is 0.6460. SDG 9, 10, 12, 14, and 17 have F1 below 0.50; SDG 17 has only eight positive judgments. Global averages do not qualify every goal or our institutional language and discipline mix.

## Numerical and local inference checks

The shipped head arithmetic is reproducible, and the released embedding code runs on this Mac. Exact production equivalence still needs a defined numerical contract.

`python3 -m classifier.head --check` reproduced all stored scores within the check's five-decimal comparison: 2,598 committee and 8,889 survey works, for both v1 and v2. All four maximum reported differences were zero.

The published production UDF uses float32 isotonic breakpoints and rounds scores to four places before thresholding. The local scorer uses float64 interpolation and thresholds before output rounding. A NumPy emulation of the published production calculation produced 19 goal membership differences across 18 of the 2,598 committee works, despite a maximum score difference of only 0.0001012. Spark was not run. Matching a score file does not establish exact membership parity with production near 0.4.

A seed-1300 sample of 32 S2 works included eight in each English/non-English-or-unknown by abstract/title-only group. Public metadata was refetched, then embedded locally. Default released CLI inference took 6.185 seconds including imports and cached model loading; 31 of 32 tag sets matched shipped-vector inference. Two previously abstract-bearing records now returned no abstract, including the only changed tag set. This is a compatibility probe with current metadata, not an accuracy benchmark against historical text.

An explicit float32 repeat pinned the embedding revision. All 30 records with unchanged abstract presence retained their tag sets, with median embedding cosine 0.999916 and maximum score difference 0.039514. Unchanged abstract presence does not prove identical historical text. The full 32-work set again had one label change. Encoding took 2.971 seconds, excluding the 1.208-second model load and imports.

Default Mac GPU inference produced vector norms from 0.9982 to 1.0035 on the project sample. Explicit float32 brought norms within approximately 0.00000012 of one and changed no project tag sets. Precision should be pinned in an adapter rather than inherited silently from the embedding model and library defaults.

Head-only arithmetic on 2,598 shipped vectors took a median 0.775 milliseconds over 20 warmed repetitions. This excludes embedding, fetching, validation, cache operations, and UI work; it is not end-to-end per-publication latency.

## Cached project smoke test

Thirty-two cached publications completed offline embedding and head inference. The test establishes local compatibility, not classification accuracy.

The cache was opened read-only. At inspection it held 7,543 canonical rows, including 7,542 with a title and 5,436 with an abstract. From title-bearing records, the sample contained eight per English (`en`/`eng`) versus other/unknown language and abstract versus title-only group, using seed 1300. Venue was omitted because it is absent from the canonical schema. Synthetic numeric IDs were used only to satisfy the upstream CLI; they are not OpenAlex identifiers.

All 32 records produced finite scores in [0,1]. Fourteen received at least one goal, eighteen received none, and sixteen goal assignments were made. No sample text exceeded the released 2,000-character input limit. Explicit float32 embedding took 2.188 seconds, excluding model load and imports. The default CLI took 4.546 seconds including imports and cached model loading.

No reviewed gold labels were available in the inspected tracked project files. These counts therefore say nothing about precision, recall, evidence quality, or whether empty outputs are correct. No project publication text was sent to Jev, Qwen chat, Aurora, or another inference provider. Project text and vectors remain only in the temporary evaluation directory; the saved numerical artifact contains no project titles, abstracts, or canonical record keys.

## Integration conditions

A local comparison adapter should preserve provenance, separate failure from empty tags, and validate ERUA performance before promotion.

- Keep all 17 raw scores separately from goal membership and record head hash, embedding revision, precision, truncation rule, venue policy, and threshold policy in the cache identity; see [[cache#SDG results]].
- Validate shape, finite values, vector norms, and output completeness. The released CLI silently skips missing titles and error records; missing output must not become `no_sdg`.
- Represent an empty tag set as no goal above threshold. Do not invent evidence quotations or claim a manual-review judgment that the head never made.
- Resolve numerical behavior at 0.4 and define review routing using reviewed labels. Reusing the current Aurora cutoff is insufficient because these are different score distributions.
- Keep local inference dependencies isolated or optional, and load the embedding model once per worker lifecycle. The trial used Python 3.14, Torch 2.14.1, sentence-transformers 6.1.0, and transformers 5.19.0 in a temporary environment; project requirements were unchanged.
- Compare against the existing independent classifier on a fixed, human-reviewed ERUA set, stratified by language, discipline, title-only records, no-SDG cases, and the five weak goals. Measure per-goal precision/recall, false positives, review workload, and end-to-end latency.

## Reproduction and saved evidence

The numerical artifact captures release identities, reproduced benchmarks, comparisons, and timings without storing private project text.

Results: [numerical evaluation artifact](../evaluations/openalex-sdgs-2026-10-08.json). Scratch checkout: `/private/tmp/erua-openalex-sdgs-eval-20261008`; isolated runtime: `/private/tmp/erua-openalex-sdgs-runtime-20261008`. Temporary directories are not durable deliverables.

From the pinned upstream checkout, the successful baseline commands were:

```bash
python3 benchmarks/score_public.py
python3 benchmarks/score_committee.py --out evaluation
python3 -m classifier.head --check
python3 -m classifier.head --vectors evaluation/local_vectors.npz --out evaluation/local_scores.jsonl
python3 -m classifier.head --vectors evaluation/project_vectors.npz --out evaluation/project_scores.jsonl
```

Local embedding used the released `classifier.embed` with batch 8 and device `mps`, then repeated through `SentenceTransformer` with the pinned revision and `model_kwargs={"dtype": torch.float32}`. Public text fetching needed the isolated environment's verified CA bundle after system Python certificate validation failed. GPU detection needed execution outside the filesystem sandbox; inference itself ran with `HF_HUB_OFFLINE=1`.

No training, live Jev evaluation, fresh committee judging, Qwen-classifier comparison, full ERUA benchmark, production Spark run, app integration, or Streamlit UI verification was performed. `lat check` and diff hygiene are the relevant project checks for this evaluation-only change.
