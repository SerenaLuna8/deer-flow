# M11 质量评测

状态：`failed_or_review_pending`

以下结果来自明确授权的真实 Provider 评测；费用仅在提供经确认的价格时估算。

语料问题数：85；问题式查询：{'dev': 10, 'holdout': 10}。

## 验收集

```json
{
  "identifier": {
    "semantic": {
      "off": {
        "count": 22,
        "query_ids": [
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22"
        ],
        "errors": 0,
        "p95_non_provider_ms": 95.20306069935032,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 22,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.82623465712856
      },
      "on": {
        "count": 22,
        "query_ids": [
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22"
        ],
        "errors": 0,
        "p95_non_provider_ms": 101.89166510317591,
        "query_embedding_cache_hits": 22,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.82623465712856
      }
    },
    "hybrid": {
      "off": {
        "count": 22,
        "query_ids": [
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22"
        ],
        "errors": 0,
        "p95_non_provider_ms": 191.59795619925717,
        "query_embedding_cache_hits": 22,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.82623465712856
      },
      "on": {
        "count": 22,
        "query_ids": [
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22"
        ],
        "errors": 0,
        "p95_non_provider_ms": 193.71868299786001,
        "query_embedding_cache_hits": 22,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.82623465712856
      }
    }
  },
  "overall": {
    "semantic": {
      "off": {
        "count": 49,
        "query_ids": [
          "h-he-01",
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22",
          "h-meta-01",
          "h-meta-02",
          "h-na-01",
          "h-na-02",
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06",
          "h-pc-01",
          "h-pc-02",
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting",
          "h-tail-01",
          "h-tail-02",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 105.05408350218204,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 50,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.8594365927006042
      },
      "on": {
        "count": 49,
        "query_ids": [
          "h-he-01",
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22",
          "h-meta-01",
          "h-meta-02",
          "h-na-01",
          "h-na-02",
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06",
          "h-pc-01",
          "h-pc-02",
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting",
          "h-tail-01",
          "h-tail-02",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 101.86152100141044,
        "query_embedding_cache_hits": 50,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.8594365927006042
      }
    },
    "hybrid": {
      "off": {
        "count": 49,
        "query_ids": [
          "h-he-01",
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22",
          "h-meta-01",
          "h-meta-02",
          "h-na-01",
          "h-na-02",
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06",
          "h-pc-01",
          "h-pc-02",
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting",
          "h-tail-01",
          "h-tail-02",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 308.9231039994047,
        "query_embedding_cache_hits": 50,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.8594365927006042
      },
      "on": {
        "count": 49,
        "query_ids": [
          "h-he-01",
          "h-id-01",
          "h-id-02",
          "h-id-03",
          "h-id-04",
          "h-id-05",
          "h-id-06",
          "h-id-07",
          "h-id-08",
          "h-id-09",
          "h-id-10",
          "h-id-11",
          "h-id-12",
          "h-id-13",
          "h-id-14",
          "h-id-15",
          "h-id-16",
          "h-id-17",
          "h-id-18",
          "h-id-19",
          "h-id-20",
          "h-id-21",
          "h-id-22",
          "h-meta-01",
          "h-meta-02",
          "h-na-01",
          "h-na-02",
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06",
          "h-pc-01",
          "h-pc-02",
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting",
          "h-tail-01",
          "h-tail-02",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 311.6362284999923,
        "query_embedding_cache_hits": 50,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.8594365927006042
      }
    }
  },
  "natural_language": {
    "semantic": {
      "off": {
        "count": 6,
        "query_ids": [
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06"
        ],
        "errors": 0,
        "p95_non_provider_ms": 131.37779779899574,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 6,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 6,
        "query_ids": [
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06"
        ],
        "errors": 0,
        "p95_non_provider_ms": 103.56981360109785,
        "query_embedding_cache_hits": 6,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    },
    "hybrid": {
      "off": {
        "count": 6,
        "query_ids": [
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06"
        ],
        "errors": 0,
        "p95_non_provider_ms": 248.56789604564256,
        "query_embedding_cache_hits": 6,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 6,
        "query_ids": [
          "h-nl-01",
          "h-nl-02",
          "h-nl-03",
          "h-nl-04",
          "h-nl-05",
          "h-nl-06"
        ],
        "errors": 0,
        "p95_non_provider_ms": 259.2033368477132,
        "query_embedding_cache_hits": 6,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    }
  },
  "tail": {
    "semantic": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-tail-01",
          "h-tail-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 96.70860175137932,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 2,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-tail-01",
          "h-tail-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 96.6114312512218,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    },
    "hybrid": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-tail-01",
          "h-tail-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 286.88620269713283,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-tail-01",
          "h-tail-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 316.5874874091969,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    }
  },
  "parent_child": {
    "semantic": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-pc-01",
          "h-pc-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 65.09691779428977,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 2,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-pc-01",
          "h-pc-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 39.946176744160766,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    },
    "hybrid": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-pc-01",
          "h-pc-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 52.810011554720404,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-pc-01",
          "h-pc-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 127.46428295304213,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    }
  },
  "cross_base": {
    "semantic": {
      "off": {
        "count": 3,
        "query_ids": [
          "h-he-01",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 106.12974199320888,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 4,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 3,
        "query_ids": [
          "h-he-01",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 97.77223160563153,
        "query_embedding_cache_hits": 4,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    },
    "hybrid": {
      "off": {
        "count": 3,
        "query_ids": [
          "h-he-01",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 157.85794160037767,
        "query_embedding_cache_hits": 4,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 3,
        "query_ids": [
          "h-he-01",
          "h-xb-01",
          "h-xb-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 235.12088520583347,
        "query_embedding_cache_hits": 4,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    }
  },
  "metadata": {
    "semantic": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-meta-01",
          "h-meta-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 53.656996350036934,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 2,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-meta-01",
          "h-meta-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 29.979800054206862,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    },
    "hybrid": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-meta-01",
          "h-meta-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 60.25800544593949,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-meta-01",
          "h-meta-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 63.66669286162505,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 1.0
      }
    }
  },
  "no_answer": {
    "semantic": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-na-01",
          "h-na-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 84.82365900108562,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 2,
        "false_recall": 0.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-na-01",
          "h-na-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 98.10834819454612,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "false_recall": 0.0
      }
    },
    "hybrid": {
      "off": {
        "count": 2,
        "query_ids": [
          "h-na-01",
          "h-na-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 335.8251454983474,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "false_recall": 0.0
      },
      "on": {
        "count": 2,
        "query_ids": [
          "h-na-01",
          "h-na-02"
        ],
        "errors": 0,
        "p95_non_provider_ms": 354.9294499993266,
        "query_embedding_cache_hits": 2,
        "query_embedding_cache_misses": 0,
        "false_recall": 0.0
      }
    }
  },
  "question_style": {
    "semantic": {
      "off": {
        "count": 10,
        "query_ids": [
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting"
        ],
        "errors": 0,
        "p95_non_provider_ms": 110.0142607529051,
        "query_embedding_cache_hits": 0,
        "query_embedding_cache_misses": 10,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.721635740010008
      },
      "on": {
        "count": 10,
        "query_ids": [
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting"
        ],
        "errors": 0,
        "p95_non_provider_ms": 103.19414360601513,
        "query_embedding_cache_hits": 10,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.721635740010008
      }
    },
    "hybrid": {
      "off": {
        "count": 10,
        "query_ids": [
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting"
        ],
        "errors": 0,
        "p95_non_provider_ms": 321.2063438995756,
        "query_embedding_cache_hits": 10,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.721635740010008
      },
      "on": {
        "count": 10,
        "query_ids": [
          "h-question-backup-rehearsal",
          "h-question-battery-shock",
          "h-question-cable-sampling",
          "h-question-dataset-amendment",
          "h-question-meter-seal",
          "h-question-parking-quota",
          "h-question-prototype-reservation",
          "h-question-resin-changeover",
          "h-question-shared-terminal",
          "h-question-tunnel-lighting"
        ],
        "errors": 0,
        "p95_non_provider_ms": 345.58053779492184,
        "query_embedding_cache_hits": 10,
        "query_embedding_cache_misses": 0,
        "recall_candidate": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.721635740010008
      }
    }
  }
}
```

## 门禁

```json
{
  "quality_passed": false,
  "all_passed": false,
  "p95_review_pending": true,
  "checks": {
    "complete_evidence": {
      "passed": true,
      "errors": 0
    },
    "query_errors": {
      "passed": true,
      "actual": 0
    },
    "semantic_question_recall_candidate": {
      "baseline": 1.0,
      "actual": 1.0,
      "uplift_pp": 0.0,
      "minimum_pp": 5,
      "passed": false
    },
    "semantic_question_recall_at_10": {
      "baseline": 1.0,
      "actual": 1.0,
      "uplift_pp": 0.0,
      "minimum_pp": 5,
      "passed": false
    },
    "semantic_overall_ndcg": {
      "drop": 0.0,
      "maximum_drop": 0.02,
      "passed": true
    },
    "semantic_no_answer": {
      "baseline": 0.0,
      "m10_waterline": 0.0,
      "actual": 0.0,
      "passed": true
    },
    "semantic_cross_base_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_cross_base_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_identifier_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_identifier_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_metadata_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_metadata_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_natural_language_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_natural_language_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_parent_child_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_parent_child_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_tail_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_tail_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "semantic_p95": {
      "baseline_ms": 105.05408350218204,
      "actual_ms": 101.86152100141044,
      "ratio": 0.9696102960081006,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_cross_base_m10_p95": {
      "baseline_ms": 115.70389020707808,
      "actual_ms": 97.77223160563153,
      "ratio": 0.8450211261751543,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_identifier_m10_p95": {
      "baseline_ms": 133.66173675167374,
      "actual_ms": 101.89166510317591,
      "ratio": 0.7623098994477191,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_metadata_m10_p95": {
      "baseline_ms": 55.626906198085635,
      "actual_ms": 29.979800054206862,
      "ratio": 0.5389442286696613,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_natural_language_m10_p95": {
      "baseline_ms": 111.59482435105019,
      "actual_ms": 103.56981360109785,
      "ratio": 0.9280879664749719,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_no_answer_m10_p95": {
      "baseline_ms": 100.11126785138913,
      "actual_ms": 98.10834819454612,
      "ratio": 0.9799930647185864,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_parent_child_m10_p95": {
      "baseline_ms": 84.02981429881038,
      "actual_ms": 39.946176744160766,
      "ratio": 0.47538099515621907,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "semantic_tail_m10_p95": {
      "baseline_ms": 113.98680710390181,
      "actual_ms": 96.6114312512218,
      "ratio": 0.8475667816816562,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_question_recall_candidate": {
      "baseline": 1.0,
      "actual": 1.0,
      "uplift_pp": 0.0,
      "minimum_pp": 5,
      "passed": false
    },
    "hybrid_question_recall_at_10": {
      "baseline": 1.0,
      "actual": 1.0,
      "uplift_pp": 0.0,
      "minimum_pp": 5,
      "passed": false
    },
    "hybrid_overall_ndcg": {
      "drop": 0.0,
      "maximum_drop": 0.02,
      "passed": true
    },
    "hybrid_no_answer": {
      "baseline": 0.0,
      "m10_waterline": 0.0,
      "actual": 0.0,
      "passed": true
    },
    "hybrid_cross_base_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_cross_base_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_identifier_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_identifier_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_metadata_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_metadata_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_natural_language_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_natural_language_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_parent_child_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_parent_child_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_tail_recall_candidate": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_tail_recall_at_10": {
      "baseline": 1.0,
      "m10_waterline": 1.0,
      "actual": 1.0,
      "passed": true
    },
    "hybrid_p95": {
      "baseline_ms": 308.9231039994047,
      "actual_ms": 311.6362284999923,
      "ratio": 1.0087825237590284,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_cross_base_m10_p95": {
      "baseline_ms": 337.7263900038088,
      "actual_ms": 235.12088520583347,
      "ratio": 0.6961874824267711,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_identifier_m10_p95": {
      "baseline_ms": 367.01442769735877,
      "actual_ms": 193.71868299786001,
      "ratio": 0.527823072823723,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_metadata_m10_p95": {
      "baseline_ms": 112.59581785197952,
      "actual_ms": 63.66669286162505,
      "ratio": 0.5654445615850708,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_natural_language_m10_p95": {
      "baseline_ms": 566.63123354665,
      "actual_ms": 259.2033368477132,
      "ratio": 0.4574462569338288,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_no_answer_m10_p95": {
      "baseline_ms": 764.2018633978296,
      "actual_ms": 354.9294499993266,
      "ratio": 0.46444462778620155,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "hybrid_parent_child_m10_p95": {
      "baseline_ms": 100.31560530187562,
      "actual_ms": 127.46428295304213,
      "ratio": 1.2706326455337542,
      "review_ratio": 1.2,
      "review_required": true,
      "operator_review": null,
      "passed": false
    },
    "hybrid_tail_m10_p95": {
      "baseline_ms": 682.6614838962996,
      "actual_ms": 316.5874874091969,
      "ratio": 0.4637547230616697,
      "review_ratio": 1.2,
      "review_required": false,
      "operator_review": null,
      "passed": true
    },
    "summary_generation": {
      "passed": true
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
  "embed_calls": 110,
  "embed_tokens_by_model": {
    "Qwen/Qwen3-Embedding-8B": 17281,
    "Qwen/Qwen3-Embedding-0.6B": 77
  },
  "rerank_calls": 696,
  "rerank_tokens": 2043368,
  "summary_calls": 24,
  "summary_call_budget": 36,
  "summary_input_tokens_estimated": 9451,
  "summary_output_tokens_estimated": 4895,
  "estimated_cost": null
}
```

## 摘要调用诊断

仅含白名单类型、状态、长度和计数。调用序号表示获准的摘要调用尝试，不表示成功 HTTP 请求数。

```json
{
  "events": [
    {
      "event_index": 1,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 1,
      "elapsed_ms": 4073.799,
      "content_kind": "string",
      "content_length": 321,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 334,
        "output_tokens": 203,
        "total_tokens": 537
      }
    },
    {
      "event_index": 2,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 2,
      "elapsed_ms": 3334.712,
      "content_kind": "string",
      "content_length": 257,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 339,
        "output_tokens": 160,
        "total_tokens": 499
      }
    },
    {
      "event_index": 3,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 3,
      "elapsed_ms": 3942.725,
      "content_kind": "string",
      "content_length": 304,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 320,
        "output_tokens": 195,
        "total_tokens": 515
      }
    },
    {
      "event_index": 4,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 4,
      "elapsed_ms": 3129.628,
      "content_kind": "string",
      "content_length": 222,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 326,
        "output_tokens": 139,
        "total_tokens": 465
      }
    },
    {
      "event_index": 5,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 5,
      "elapsed_ms": 3952.085,
      "content_kind": "string",
      "content_length": 345,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 317,
        "output_tokens": 204,
        "total_tokens": 521
      }
    },
    {
      "event_index": 6,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 6,
      "elapsed_ms": 3480.668,
      "content_kind": "string",
      "content_length": 292,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 330,
        "output_tokens": 191,
        "total_tokens": 521
      }
    },
    {
      "event_index": 7,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 7,
      "elapsed_ms": 3801.136,
      "content_kind": "string",
      "content_length": 341,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 332,
        "output_tokens": 210,
        "total_tokens": 542
      }
    },
    {
      "event_index": 8,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 8,
      "elapsed_ms": 3477.537,
      "content_kind": "string",
      "content_length": 321,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 326,
        "output_tokens": 193,
        "total_tokens": 519
      }
    },
    {
      "event_index": 9,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 9,
      "elapsed_ms": 3833.861,
      "content_kind": "string",
      "content_length": 301,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 332,
        "output_tokens": 202,
        "total_tokens": 534
      }
    },
    {
      "event_index": 10,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 10,
      "elapsed_ms": 3760.724,
      "content_kind": "string",
      "content_length": 316,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 332,
        "output_tokens": 202,
        "total_tokens": 534
      }
    },
    {
      "event_index": 11,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 11,
      "elapsed_ms": 3762.44,
      "content_kind": "string",
      "content_length": 312,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 335,
        "output_tokens": 207,
        "total_tokens": 542
      }
    },
    {
      "event_index": 12,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 12,
      "elapsed_ms": 3886.936,
      "content_kind": "string",
      "content_length": 333,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 338,
        "output_tokens": 219,
        "total_tokens": 557
      }
    },
    {
      "event_index": 13,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 13,
      "elapsed_ms": 3706.837,
      "content_kind": "string",
      "content_length": 248,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 329,
        "output_tokens": 167,
        "total_tokens": 496
      }
    },
    {
      "event_index": 14,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 14,
      "elapsed_ms": 3736.778,
      "content_kind": "string",
      "content_length": 316,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 330,
        "output_tokens": 193,
        "total_tokens": 523
      }
    },
    {
      "event_index": 15,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 15,
      "elapsed_ms": 3199.102,
      "content_kind": "string",
      "content_length": 243,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 309,
        "output_tokens": 152,
        "total_tokens": 461
      }
    },
    {
      "event_index": 16,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 16,
      "elapsed_ms": 3752.316,
      "content_kind": "string",
      "content_length": 313,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 314,
        "output_tokens": 191,
        "total_tokens": 505
      }
    },
    {
      "event_index": 17,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 17,
      "elapsed_ms": 5075.959,
      "content_kind": "string",
      "content_length": 299,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 317,
        "output_tokens": 184,
        "total_tokens": 501
      }
    },
    {
      "event_index": 18,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 18,
      "elapsed_ms": 4063.514,
      "content_kind": "string",
      "content_length": 347,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 338,
        "output_tokens": 234,
        "total_tokens": 572
      }
    },
    {
      "event_index": 19,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 19,
      "elapsed_ms": 3311.998,
      "content_kind": "string",
      "content_length": 204,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 321,
        "output_tokens": 127,
        "total_tokens": 448
      }
    },
    {
      "event_index": 20,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 20,
      "elapsed_ms": 3365.984,
      "content_kind": "string",
      "content_length": 225,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 329,
        "output_tokens": 134,
        "total_tokens": 463
      }
    },
    {
      "event_index": 21,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 21,
      "elapsed_ms": 2483.344,
      "content_kind": "string",
      "content_length": 106,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 314,
        "output_tokens": 70,
        "total_tokens": 384
      }
    },
    {
      "event_index": 22,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 22,
      "elapsed_ms": 3153.644,
      "content_kind": "string",
      "content_length": 278,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 289,
        "output_tokens": 159,
        "total_tokens": 448
      }
    },
    {
      "event_index": 23,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 23,
      "elapsed_ms": 3011.918,
      "content_kind": "string",
      "content_length": 207,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 281,
        "output_tokens": 118,
        "total_tokens": 399
      }
    },
    {
      "event_index": 24,
      "event": "runtime_response",
      "task_id": null,
      "document_id": null,
      "task_attempt": 1,
      "call_index": 24,
      "elapsed_ms": 2877.83,
      "content_kind": "string",
      "content_length": 198,
      "content_empty": false,
      "finish_reason": "stop",
      "token_usage": {
        "input_tokens": 265,
        "output_tokens": 115,
        "total_tokens": 380
      }
    }
  ]
}
```

本次仅使用随机隔离评测库。目标库部署、旧数据处置与重置仍需独立确认。
