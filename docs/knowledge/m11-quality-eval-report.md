# M11 质量评测

状态：`failed_or_review_pending`

以下结果来自明确授权的真实 Provider 评测；费用仅在提供经确认的价格时估算。

语料问题数：85；问题式查询：{'dev': 10, 'holdout': 10}。

## 验收集

```json
{}
```

## 门禁

```json
{
  "quality_passed": false,
  "all_passed": false,
  "p95_review_pending": false,
  "checks": {
    "complete_evidence": {
      "passed": false,
      "errors": 0
    },
    "query_errors": {
      "passed": true,
      "actual": 0
    },
    "semantic_question_recall_candidate": {
      "baseline": null,
      "actual": null,
      "uplift_pp": null,
      "minimum_pp": 5,
      "passed": false
    },
    "semantic_question_recall_at_10": {
      "baseline": null,
      "actual": null,
      "uplift_pp": null,
      "minimum_pp": 5,
      "passed": false
    },
    "semantic_overall_ndcg": {
      "drop": null,
      "maximum_drop": 0.02,
      "passed": false
    },
    "semantic_no_answer": {
      "baseline": null,
      "m10_waterline": 0.0,
      "actual": null,
      "passed": false
    },
    "semantic_cross_base_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_cross_base_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_identifier_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_identifier_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_metadata_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_metadata_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_natural_language_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_natural_language_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_parent_child_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_parent_child_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_tail_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_tail_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "semantic_p95": {
      "baseline_ms": null,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_cross_base_m10_p95": {
      "baseline_ms": 115.70389020707808,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_identifier_m10_p95": {
      "baseline_ms": 133.66173675167374,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_metadata_m10_p95": {
      "baseline_ms": 55.626906198085635,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_natural_language_m10_p95": {
      "baseline_ms": 111.59482435105019,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_no_answer_m10_p95": {
      "baseline_ms": 100.11126785138913,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_parent_child_m10_p95": {
      "baseline_ms": 84.02981429881038,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "semantic_tail_m10_p95": {
      "baseline_ms": 113.98680710390181,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_question_recall_candidate": {
      "baseline": null,
      "actual": null,
      "uplift_pp": null,
      "minimum_pp": 5,
      "passed": false
    },
    "hybrid_question_recall_at_10": {
      "baseline": null,
      "actual": null,
      "uplift_pp": null,
      "minimum_pp": 5,
      "passed": false
    },
    "hybrid_overall_ndcg": {
      "drop": null,
      "maximum_drop": 0.02,
      "passed": false
    },
    "hybrid_no_answer": {
      "baseline": null,
      "m10_waterline": 0.0,
      "actual": null,
      "passed": false
    },
    "hybrid_cross_base_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_cross_base_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_identifier_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_identifier_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_metadata_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_metadata_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_natural_language_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_natural_language_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_parent_child_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_parent_child_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_tail_recall_candidate": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_tail_recall_at_10": {
      "baseline": null,
      "m10_waterline": 1.0,
      "actual": null,
      "passed": false
    },
    "hybrid_p95": {
      "baseline_ms": null,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_cross_base_m10_p95": {
      "baseline_ms": 337.7263900038088,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_identifier_m10_p95": {
      "baseline_ms": 367.01442769735877,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_metadata_m10_p95": {
      "baseline_ms": 112.59581785197952,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_natural_language_m10_p95": {
      "baseline_ms": 566.63123354665,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_no_answer_m10_p95": {
      "baseline_ms": 764.2018633978296,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_parent_child_m10_p95": {
      "baseline_ms": 100.31560530187562,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "hybrid_tail_m10_p95": {
      "baseline_ms": 682.6614838962996,
      "actual_ms": null,
      "ratio": null,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": false
    },
    "summary_generation": {
      "passed": false
    },
    "query_cache_pairs": {
      "passed": false
    }
  }
}
```

## 调用和性能

```json
{
  "embed_calls": 20,
  "embed_tokens_by_model": {
    "Qwen/Qwen3-Embedding-8B": 15121,
    "Qwen/Qwen3-Embedding-0.6B": 56
  },
  "rerank_calls": 0,
  "rerank_tokens": 0,
  "summary_calls": 24,
  "summary_call_budget": 24,
  "summary_input_tokens_estimated": 9511,
  "summary_output_tokens_estimated": 4193,
  "estimated_cost": null
}
```

本次仅使用随机隔离评测库。目标库部署、旧数据处置与重置仍需独立确认。
