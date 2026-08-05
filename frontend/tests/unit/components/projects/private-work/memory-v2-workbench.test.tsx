import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  MemoryV2Workbench,
  type MemoryTab,
  type MemoryV2WorkbenchProps,
} from "@/components/projects/private-work/memory/memory-v2-workbench";
import { I18nProvider } from "@/core/i18n/context";
import {
  memoryV2CandidateSchema,
  memoryV2FactDetailSchema,
  memoryV2FactSchema,
} from "@/core/private-work/memory";

const FACT_ID = "33333333-3333-4333-8333-333333333333";
const REVISION_ID = "44444444-4444-4444-8444-444444444444";
const EVIDENCE_ID = "55555555-5555-4555-8555-555555555555";
const CANDIDATE_ID = "66666666-6666-4666-8666-666666666666";
const TIMESTAMP = "2026-08-05T00:00:00Z";

const revision = {
  id: REVISION_ID,
  factId: FACT_ID,
  revisionNumber: 1,
  revisionSequence: 1,
  content: "用户希望执行计划可直接落地",
  contentDigest: "a".repeat(64),
  category: "preference",
  confidence: 0.94,
  validFrom: TIMESTAMP,
  validTo: null,
  lastConfirmedAt: TIMESTAMP,
  changedBy: "user",
  sourceCandidateId: CANDIDATE_ID,
  supersedesRevisionId: null,
  changeReason: "用户确认",
  contentErasedAt: null,
  createdAt: TIMESTAMP,
};

const activeFact = memoryV2FactSchema.parse({
  id: FACT_ID,
  factKind: "preference",
  status: "active",
  version: 1,
  disabledAt: null,
  supersededAt: null,
  deletedAt: null,
  createdAt: TIMESTAMP,
  updatedAt: TIMESTAMP,
  currentRevision: revision,
});

const candidate = memoryV2CandidateSchema.parse({
  id: CANDIDATE_ID,
  candidateType: "preference",
  content: "用户希望执行计划包含验收命令",
  confidence: 0.88,
  retentionClass: "durable",
  sensitivity: "restricted",
  status: "pending",
  decisionReason: null,
  decidedAt: null,
  contentErasedAt: null,
  createdAt: TIMESTAMP,
  updatedAt: TIMESTAMP,
});

const detail = memoryV2FactDetailSchema.parse({
  namespace: "default",
  fact: activeFact,
  revisions: [revision],
  evidence: [
    {
      id: EVIDENCE_ID,
      factId: FACT_ID,
      revisionId: REVISION_ID,
      sourceCandidateId: CANDIDATE_ID,
      sourceItemId: null,
      threadId: "thread-1",
      runId: "run-1",
      runEventSequence: 9,
      evidenceExcerpt: "执行计划需要明确到文件和测试。",
      trustClass: "direct",
      sourceErasedAt: TIMESTAMP,
      createdAt: TIMESTAMP,
    },
  ],
});

function props(initialTab: MemoryTab): MemoryV2WorkbenchProps {
  return {
    initialTab,
    projectName: "Alpha Project",
    projectSlug: "alpha",
    facts: {
      items: [activeFact],
      page: 0,
      hasNext: true,
      isLoading: false,
      isFetching: false,
      error: null,
      retry: () => undefined,
      previous: () => undefined,
      next: () => undefined,
      query: "",
      category: "",
      status: "active",
      setQuery: () => undefined,
      setCategory: () => undefined,
      setStatus: () => undefined,
    },
    candidates: {
      items: [candidate],
      page: 0,
      hasNext: false,
      isLoading: false,
      isFetching: false,
      error: null,
      retry: () => undefined,
      previous: () => undefined,
      next: () => undefined,
    },
    detail: {
      selectedFactId: FACT_ID,
      data: detail,
      isLoading: false,
      error: null,
      retry: () => undefined,
      select: () => undefined,
    },
    status: {
      data: {
        enabled: true,
        pipelineMode: "v2",
        searchEnabled: true,
        injectionEnabled: true,
        consolidationIntervalMinutes: 120,
        candidateRetentionDays: 30,
      },
      isLoading: false,
      error: null,
      retry: () => undefined,
    },
    actions: {
      canManage: true,
      canHardForget: true,
      canExport: true,
      isExporting: false,
      busyCandidateIds: [],
      busyFactIds: [],
      exportMemory: async () => undefined,
      acceptCandidate: async () => undefined,
      rejectCandidate: async () => undefined,
      reviseFact: async () => undefined,
      disableFact: async () => undefined,
      restoreFact: async () => undefined,
      hardForgetFact: async () => undefined,
    },
  };
}

function render(
  tab: MemoryTab,
  transform?: (value: MemoryV2WorkbenchProps) => void,
) {
  const value = props(tab);
  transform?.(value);
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <MemoryV2Workbench {...value} />
    </I18nProvider>,
  );
}

describe("Memory v2 workbench", () => {
  test("renders the four regions and active fact controls", () => {
    const html = render("facts");

    for (const label of ["长期记忆", "待整理", "修改历史", "设置"]) {
      expect(html).toContain(label);
    }
    expect(html).toContain("用户希望执行计划可直接落地");
    expect(html).toContain("停止召回");
    expect(html).toContain("查看历史");
    expect(html).toContain("永久遗忘");
    expect(html).toContain("下一页");
  });

  test("renders filtered empty state separately from the first-use empty state", () => {
    const filtered = render("facts", (value) => {
      value.facts.items = [];
      value.facts.query = "不存在";
      value.facts.hasNext = false;
    });
    const firstUse = render("facts", (value) => {
      value.facts.items = [];
      value.facts.hasNext = false;
    });

    expect(filtered).toContain("没有匹配的事实");
    expect(firstUse).toContain("还没有长期事实");
  });

  test("keeps a return control when a later page becomes empty", () => {
    const facts = render("facts", (value) => {
      value.facts.items = [];
      value.facts.page = 1;
      value.facts.hasNext = false;
    });
    const candidates = render("candidates", (value) => {
      value.candidates.items = [];
      value.candidates.page = 1;
      value.candidates.hasNext = false;
    });

    expect(facts).toContain("上一页");
    expect(candidates).toContain("上一页");
  });

  test("hides export when the project lacks read permission", () => {
    const html = render("facts", (value) => {
      value.actions.canExport = false;
    });

    expect(html).not.toContain("导出 NDJSON");
  });

  test("blocks accepting a restricted candidate but keeps reject available", () => {
    const html = render("candidates");

    expect(html).toContain("受限");
    expect(html).toContain("不能接受为长期事实");
    expect(html).toMatch(/<button[^>]*disabled[^>]*>接受<\/button>/u);
    expect(html).toMatch(/<button[^>]*>拒绝<\/button>/u);
  });

  test("shows revision source erasure without linking the deleted thread", () => {
    const html = render("history");

    expect(html).toContain("修订 1");
    expect(html).toContain("用户确认");
    expect(html).toContain("来源已删除");
    expect(html).toContain("执行计划需要明确到文件和测试");
    expect(html).not.toContain("打开来源会话");
  });

  test("shows the current read-only pipeline values", () => {
    const html = render("settings");

    expect(html).toContain("Memory v2");
    expect(html).toContain("120 分钟");
    expect(html).toContain("30 天");
    expect(html).toContain("由系统管理员统一管理");
  });
});
