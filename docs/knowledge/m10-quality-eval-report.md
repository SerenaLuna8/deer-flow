# M10 真实质量评测报告

- 运行时间：2026-08-31T16:47:36.352403+00:00
- 硬件：Darwin 25.6.0 arm64 arm
- 模型：主嵌入 `Qwen/Qwen3-Embedding-8B` / 副嵌入 `Qwen/Qwen3-Embedding-0.6B` / 重排 `Qwen/Qwen3-Reranker-8B`
- 语料：65 题（holdout 39，其中标识符 22），检索单元 10002
- 对照：semantic 视为 M9 等价路径（同模型、无词法）；hybrid 为 M10 路径。异构域另跑两模式。
- 调用：embed 195 条 / 约 4209 token，rerank 5960 篇 / 约 256329 token，旧费率估算 ¥0.0730（费率未经当前验证，不是实测账单）
- 质量门槛：通过；总门禁：未通过。
- 性能评审：待操作者显式复审：非 Provider P95 超过评测阈值，当前没有批准记录。测量本身不证明耗时增长的具体原因。

## 验收集汇总

```json
{
  "identifier": {
    "semantic": {
      "count": 22,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 0.82623465712856,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 133.66173675167374,
      "misses": []
    },
    "hybrid": {
      "count": 22,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 0.82623465712856,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 367.01442769735877,
      "misses": []
    }
  },
  "natural_language": {
    "semantic": {
      "count": 6,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 111.59482435105019,
      "misses": []
    },
    "hybrid": {
      "count": 6,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 566.63123354665,
      "misses": []
    }
  },
  "tail": {
    "semantic": {
      "count": 2,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 113.98680710390181,
      "misses": []
    },
    "hybrid": {
      "count": 2,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 682.6614838962996,
      "misses": []
    }
  },
  "parent_child": {
    "semantic": {
      "count": 2,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 2.0,
      "p95_non_provider_ms": 84.02981429881038,
      "misses": []
    },
    "hybrid": {
      "count": 2,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 2.0,
      "p95_non_provider_ms": 100.31560530187562,
      "misses": []
    }
  },
  "cross_base": {
    "semantic": {
      "count": 3,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 115.70389020707808,
      "misses": []
    },
    "hybrid": {
      "count": 3,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 52.0,
      "p95_non_provider_ms": 337.7263900038088,
      "misses": []
    }
  },
  "metadata": {
    "semantic": {
      "count": 2,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 2.0,
      "p95_non_provider_ms": 55.626906198085635,
      "misses": []
    },
    "hybrid": {
      "count": 2,
      "errors": 0,
      "recall_candidate": 1.0,
      "recall_at_10": 1.0,
      "ndcg_at_10": 1.0,
      "mean_candidates": 2.0,
      "p95_non_provider_ms": 112.59581785197952,
      "misses": []
    }
  },
  "no_answer": {
    "semantic": {
      "count": 2,
      "errors": 0,
      "false_recall": 0.0,
      "mean_returned": 0.0,
      "p95_non_provider_ms": 100.11126785138913
    },
    "hybrid": {
      "count": 2,
      "errors": 0,
      "false_recall": 0.0,
      "mean_returned": 0.0,
      "p95_non_provider_ms": 764.2018633978296
    }
  }
}
```

## 门槛判定

```json
{
  "quality_passed": true,
  "all_passed": false,
  "p95_review_recorded": false,
  "p95_review_pending": true,
  "checks": {
    "identifier_recall_candidate_hybrid": {
      "actual": 1.0,
      "min": 0.95,
      "passed": true
    },
    "identifier_recall_at_10_hybrid": {
      "actual": 1.0,
      "min": 0.95,
      "passed": true
    },
    "identifier_m9_semantic_recall_at_10": {
      "actual": 1.0,
      "note": "M9-equivalent path is retrieval_mode=semantic on the same frozen models"
    },
    "natural_language_recall_regression": {
      "baseline": 1.0,
      "actual": 1.0,
      "drop": 0.0,
      "max_drop": 0.02,
      "passed": true
    },
    "natural_language_ndcg_regression": {
      "baseline": 1.0,
      "actual": 1.0,
      "drop": 0.0,
      "max_drop": 0.02,
      "passed": true
    },
    "no_answer_false_recall": {
      "baseline": 0.0,
      "actual": 0.0,
      "passed": true
    },
    "tail_zero_miss": {
      "misses": [],
      "passed": true
    },
    "p95_non_provider": {
      "baseline_ms": 111.59482435105019,
      "actual_ms": 566.63123354665,
      "ratio": 5.077576284041327,
      "review_ratio": 1.5,
      "review_required": true,
      "review": null,
      "passed": false
    }
  }
}
```

## 非 Provider P95 产品预算复审

待操作者显式复审：非 Provider P95 超过评测阈值，当前没有批准记录。测量本身不证明耗时增长的具体原因。

## 部署确认

本评测使用随机隔离空库，不是操作者目标库。M10 Schema 仍是一次定义、无升级路径；未取得目标库/停服/旧数据处置确认前，部署保持阻塞。
