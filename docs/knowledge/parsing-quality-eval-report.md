# RAG parsing quality evaluation

Date: 2026-09-01

This report compares the fixed pre-refactor parser at commit
`b96581974b057c0ae4d853815130d99c0ed23823` with the current parser over the
same six source files and source hashes. The corpus covers missing CSV header
cells, leading-zero identifiers, a long table, Word heading/step structure,
Markdown generic literals, and an image-only document. Each category has three
queries; the image-only queries are reported separately and are excluded from
answer-query averages.

## Deterministic replay result

- Execution mode: replay; external Provider calls: 0.
- Search path: production `KnowledgeSearchService`, hybrid retrieval, `top_k=5`.
- Search invocations: 30 across 15 answer queries and both corpora.
- Baseline source labels matched: 9 of 15; 6 labels lacked an exact baseline
  source mapping and were not assigned fabricated relevance.
- Current parser source labels matched: 15 of 15.
- Image-only source: 0 indexed text segments in both corpora; no OCR or image
  semantic-retrieval claim is made.

Replay validates corpus construction, source mapping, model-binding parity, and
the production retrieval workflow. It does not establish Hit@5 or MRR@5 quality
improvement. The machine-readable evidence is in
[`parsing-quality-eval-report.json`](parsing-quality-eval-report.json).

## Real-model boundary

The opt-in real Embedding/Reranker gate was not run because this task did not
authorize external Provider calls. Therefore the quality conclusion remains
`not_evaluated`; A27 is unverified. When explicitly authorized, run the
`provider_integration` case with
`ACT_WEAVE_KNOWLEDGE_PARSING_QUALITY_EVAL=1`. Enabling the gate without an
available configured credential fails instead of skipping.

## Verification evidence

- Parsing-quality non-Provider gate: 6 passed, 1 Provider case deselected.
- Existing M10 metric regression: 12 passed, 1 Provider case deselected.
- Fixed baseline: 28 segments; current parser: 27 segments.
- Random PostgreSQL databases used by the replay were deleted after execution.

No production database, deployment, or external model was modified or called.
