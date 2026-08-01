import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type { SkillBuilderSession } from "@/core/skill-builder";

import { mockLangGraphAPI } from "./utils/mock-api";

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SESSION_ID = "20000000-0000-4000-8000-000000000001";
const THREAD_ID = "30000000-0000-4000-8000-000000000001";
const NOW = "2026-08-01T08:00:00Z";
const SKILL_CONTENT = `---
name: code-writer
description: Turn Chinese meeting notes into an owned action list.
---

# Workflow

1. Read \`references/extraction-rules.md\`.
2. Normalize with \`scripts/normalize_actions.py\`.
3. Render with \`templates/action-report.md\`.
`;
const REFERENCE_CONTENT = `# 提取规则

负责人或截止日期缺失时写“待确认”，不要臆测。
`;
const SCRIPT_CONTENT = `def normalize_actions(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]
`;
const TEMPLATE_CONTENT = `# 行动清单

| 行动项 | 负责人 | 截止日期 | 状态 |
| --- | --- | --- | --- |
| {{action}} | {{owner}} | {{due_date}} | {{status}} |
`;

const project: Project = {
  id: PROJECT_ID,
  slug: "research-lab",
  display_name: "Research Lab",
  description: "Shared research",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "project.pin",
    "shared_assets.read",
    "shared_assets.execute",
    "shared_assets.edit",
    "shared_assets.manage_bindings",
    "private_work.create",
    "private_work.read_own",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-project",
};

function skillSession(
  overrides: Partial<SkillBuilderSession> = {},
): SkillBuilderSession {
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: "default",
    thread_id: THREAD_ID,
    slug: "code-writer",
    display_name: "code-writer",
    status: "interviewing",
    revision: 1,
    messages: [],
    active_clarification: null,
    progress: [
      { id: "interview", label: "确认需求", status: "running" },
      { id: "draft", label: "生成候选文件", status: "pending" },
      { id: "validate", label: "检查 Skill", status: "pending" },
    ],
    files: [],
    draft_checksum: null,
    validation: null,
    error_code: null,
    error_message: null,
    created_skill_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function deferredGate() {
  let release!: () => void;
  let markStarted!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  return { promise, release, started, markStarted };
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function captureSkillBuilderEvidence(page: Page, filename: string) {
  const directory = process.env.CAPTURE_SKILL_BUILDER_EVIDENCE_DIR?.trim();
  if (!directory) return;
  await page.screenshot({
    path: `${directory}/${filename}.png`,
    fullPage: false,
    animations: "disabled",
  });
}

async function mockSkillBuilder(
  page: Page,
  options: { failTurn?: boolean } = {},
) {
  mockLangGraphAPI(page);
  const turnGate = deferredGate();
  const turnRequests: Array<Record<string, unknown>> = [];
  let currentSession = skillSession();
  const builderBase = `/api/projects/${PROJECT_ID}/skill-builder/sessions`;

  await page.route(/\/api\/projects(?:\/.*)?(?:\?.*)?$/u, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === "/api/projects" && method === "GET") {
      await json(route, { items: [project], next_cursor: null });
      return;
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      await json(route, project);
      return;
    }
    if (path === builderBase && method === "GET") {
      await json(route, {
        data: [
          {
            id: currentSession.id,
            slug: currentSession.slug,
            display_name: currentSession.display_name,
            status: currentSession.status,
            updated_at: currentSession.updated_at,
          },
        ],
        request_id: "request-builder-list",
      });
      return;
    }
    if (path === `${builderBase}/${SESSION_ID}` && method === "GET") {
      await json(route, {
        data: currentSession,
        request_id: "request-builder-get",
      });
      return;
    }
    if (path === `${builderBase}/${SESSION_ID}/turns` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      turnRequests.push(body);
      turnGate.markStarted();
      await turnGate.promise;
      if (options.failTurn) {
        await json(
          route,
          {
            detail: {
              code: "asset_storage_unavailable",
              message: "Asset storage unavailable",
              request_id: "request-builder-turn-failed",
            },
          },
          503,
        );
        return;
      }
      const input = body.input as { kind?: string; message?: string };
      currentSession = skillSession({
        status: "draft_ready",
        revision: 3,
        messages: [
          {
            id: "message-user-1",
            role: "user",
            content: input.message ?? "",
            created_at: NOW,
          },
          {
            id: "message-assistant-1",
            role: "assistant",
            content: "候选代码 Skill 已生成。",
            created_at: NOW,
          },
        ],
        progress: [
          { id: "interview", label: "确认需求", status: "completed" },
          { id: "draft", label: "生成候选文件", status: "completed" },
          { id: "validate", label: "检查 Skill", status: "pending" },
        ],
        files: [
          {
            path: "SKILL.md",
            media_type: "text/markdown",
            size_bytes: Buffer.byteLength(SKILL_CONTENT, "utf8"),
            sha256: "a".repeat(64),
            encoding: "utf-8",
            content: SKILL_CONTENT,
          },
          {
            path: "references/extraction-rules.md",
            media_type: "text/markdown",
            size_bytes: Buffer.byteLength(REFERENCE_CONTENT, "utf8"),
            sha256: "b".repeat(64),
            encoding: "utf-8",
            content: REFERENCE_CONTENT,
          },
          {
            path: "scripts/normalize_actions.py",
            media_type: "text/x-python",
            size_bytes: Buffer.byteLength(SCRIPT_CONTENT, "utf8"),
            sha256: "c".repeat(64),
            encoding: "utf-8",
            content: SCRIPT_CONTENT,
          },
          {
            path: "templates/action-report.md",
            media_type: "text/markdown",
            size_bytes: Buffer.byteLength(TEMPLATE_CONTENT, "utf8"),
            sha256: "d".repeat(64),
            encoding: "utf-8",
            content: TEMPLATE_CONTENT,
          },
        ],
        draft_checksum: "c".repeat(64),
      });
      await json(route, {
        data: currentSession,
        request_id: "request-builder-turn",
      });
      return;
    }
    if (
      path === `/api/projects/${PROJECT_ID}/default-agent` &&
      method === "GET"
    ) {
      await json(route, {
        agent_asset_id: null,
        revision: 0,
        request_id: "request-default-agent",
      });
      return;
    }

    await route.fallback();
  });

  return { turnGate, turnRequests };
}

test("Skill Builder shows a submitted user turn before generation completes", async ({
  page,
}) => {
  const mock = await mockSkillBuilder(page);
  await page.goto(`/projects/research-lab/skills/new/${SESSION_ID}`);

  const composer = page.getByLabel("描述想要的 Skill");
  await composer.fill("写代码");
  await page.getByRole("button", { name: "发送" }).click();
  await mock.turnGate.started;

  const userMessages = page.getByTestId("skill-builder-user-message");
  await expect(userMessages.getByText("写代码", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("");
  await expect(page.getByText(/skill-creator 正在生成候选文件/u)).toBeVisible();
  await captureSkillBuilderEvidence(page, "skill-builder-pending-user-turn");

  mock.turnGate.release();
  await expect(
    page.getByText("候选代码 Skill 已生成。", { exact: true }),
  ).toBeVisible();
  await expect(userMessages.getByText("写代码", { exact: true })).toHaveCount(
    1,
  );
  await captureSkillBuilderEvidence(page, "skill-builder-canonical-turn");

  expect(mock.turnRequests).toHaveLength(1);
  expect(mock.turnRequests[0]).toMatchObject({
    input: { kind: "message", message: "写代码" },
    expected_revision: 1,
  });
});

test("Skill Builder restores the draft and localizes a failed turn", async ({
  page,
}) => {
  const mock = await mockSkillBuilder(page, { failTurn: true });
  await page.goto(`/projects/research-lab/skills/new/${SESSION_ID}`);

  const composer = page.getByLabel("描述想要的 Skill");
  await composer.fill("写代码");
  await page.getByRole("button", { name: "发送" }).click();
  await mock.turnGate.started;
  const userMessages = page.getByTestId("skill-builder-user-message");
  await expect(userMessages.getByText("写代码", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("");

  mock.turnGate.release();
  await expect(composer).toHaveValue("写代码");
  await expect(
    page.getByText("Skill 设计服务暂时不可用，请稍后重试。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText("Asset storage unavailable")).toHaveCount(0);
  await expect(userMessages).toHaveCount(0);
});

test("Skill Builder reconstructs scripts references and templates directories", async ({
  page,
}) => {
  const mock = await mockSkillBuilder(page);
  await page.goto(`/projects/research-lab/skills/new/${SESSION_ID}`);

  await page.getByLabel("描述想要的 Skill").fill("创建会议行动项 Skill");
  await page.getByRole("button", { name: "发送" }).click();
  await mock.turnGate.started;
  mock.turnGate.release();

  await expect(page.getByText("4 个 UTF-8 文本文件")).toBeVisible();
  await expect(page.getByLabel("文件 SKILL.md")).toBeVisible();

  for (const directory of ["references", "scripts", "templates"]) {
    const folder = page.getByLabel(`文件夹 ${directory}`);
    await expect(folder).toBeVisible();
    await folder.click();
  }

  await expect(
    page.getByLabel("文件 references/extraction-rules.md"),
  ).toBeVisible();
  await expect(
    page.getByLabel("文件 scripts/normalize_actions.py"),
  ).toBeVisible();
  const template = page.getByLabel("文件 templates/action-report.md");
  await expect(template).toBeVisible();
  await template.click();
  await expect(page.getByLabel("编辑 templates/action-report.md")).toHaveValue(
    TEMPLATE_CONTENT,
  );
  await expect(page.getByRole("button", { name: "预览" })).toBeVisible();

  await captureSkillBuilderEvidence(
    page,
    "skill-builder-multi-directory-candidate",
  );
});
