import { expect, test, type Page, type Route } from "@playwright/test";

import type { Capability, Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const FACT_ID = "20000000-0000-4000-8000-000000000001";
const REVISION_ID = "30000000-0000-4000-8000-000000000001";
const CANDIDATE_ACCEPT_ID = "40000000-0000-4000-8000-000000000001";
const CANDIDATE_REJECT_ID = "40000000-0000-4000-8000-000000000002";
const EVIDENCE_ID = "50000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-05T00:00:00Z";
const capabilities: Capability[] = [
  "project.read",
  "project.enter",
  "private_work.read_own",
  "private_work.create",
];
const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Core project route",
  icon: "folder",
  role: "admin",
  capabilities,
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 1,
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
  request_id: "request-alpha",
};

function revision(content = "Prefers executable implementation plans") {
  return {
    id: REVISION_ID,
    factId: FACT_ID,
    revisionNumber: 1,
    revisionSequence: 1,
    content,
    contentDigest: "a".repeat(64),
    category: "preference",
    confidence: 0.92,
    validFrom: TIMESTAMP,
    validTo: null,
    lastConfirmedAt: TIMESTAMP,
    changedBy: "user",
    sourceCandidateId: CANDIDATE_ACCEPT_ID,
    supersedesRevisionId: null,
    changeReason: "User confirmed",
    contentErasedAt: null,
    createdAt: TIMESTAMP,
  };
}

function memoryFact() {
  return {
    id: FACT_ID,
    factKind: "preference",
    status: "active" as "active" | "disabled",
    version: 1,
    disabledAt: null as string | null,
    supersededAt: null,
    deletedAt: null,
    createdAt: TIMESTAMP,
    updatedAt: TIMESTAMP,
    currentRevision: revision(),
  };
}

function candidate(id: string, content: string) {
  return {
    id,
    candidateType: "preference",
    content,
    confidence: 0.86,
    retentionClass: "durable",
    sensitivity: "normal",
    status: "pending",
    decisionReason: null,
    decidedAt: null,
    contentErasedAt: null,
    createdAt: TIMESTAMP,
    updatedAt: TIMESTAMP,
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockProjectMemoryRoute(page: Page) {
  let fact = memoryFact();
  let facts = [fact];
  let candidates = [
    candidate(CANDIDATE_ACCEPT_ID, "Keep acceptance criteria explicit"),
    candidate(CANDIDATE_REJECT_ID, "Temporary conversational detail"),
  ];
  let reviseConflictPending = true;

  await page.route("**/api/**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status") {
      return json(route, {
        needs_setup: false,
        registration_enabled: true,
      });
    }
    if (path === "/api/projects" && request.method() === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/enter` &&
      request.method() === "POST"
    ) {
      return json(route, project);
    }

    const memoryBase = `/api/projects/${PROJECT_ID}/memory/v2`;
    if (path === `${memoryBase}/status` && request.method() === "GET") {
      return json(route, {
        enabled: true,
        pipelineMode: "v2",
        searchEnabled: true,
        injectionEnabled: true,
        consolidationIntervalMinutes: 120,
        candidateRetentionDays: 30,
      });
    }
    if (path === `${memoryBase}/facts` && request.method() === "GET") {
      const status = url.searchParams.get("status") ?? "active";
      const query = (url.searchParams.get("query") ?? "").toLowerCase();
      const category = url.searchParams.get("category") ?? "";
      const items = facts.filter(
        (item) =>
          (status === "all" || item.status === status) &&
          (!query ||
            item.currentRevision.content.toLowerCase().includes(query)) &&
          (!category || item.currentRevision.category === category),
      );
      return json(route, { namespace: "default", items });
    }
    if (path === `${memoryBase}/candidates` && request.method() === "GET") {
      return json(route, { namespace: "default", items: candidates });
    }
    if (
      path === `${memoryBase}/facts/${FACT_ID}` &&
      request.method() === "GET"
    ) {
      return json(route, {
        namespace: "default",
        fact,
        revisions: [fact.currentRevision],
        evidence: [
          {
            id: EVIDENCE_ID,
            factId: FACT_ID,
            revisionId: REVISION_ID,
            sourceCandidateId: CANDIDATE_ACCEPT_ID,
            sourceItemId: null,
            threadId: "source-thread",
            runId: "source-run",
            runEventSequence: 2,
            evidenceExcerpt: "The user requested executable plans.",
            trustClass: "direct",
            sourceErasedAt: null,
            createdAt: TIMESTAMP,
          },
        ],
      });
    }
    if (
      path === `${memoryBase}/facts/${FACT_ID}` &&
      request.method() === "PATCH"
    ) {
      const body = request.postDataJSON() as {
        content?: string;
        category?: string;
        confidence?: number;
      };
      if (reviseConflictPending) {
        reviseConflictPending = false;
        fact = {
          ...fact,
          version: fact.version + 1,
          currentRevision: {
            ...fact.currentRevision,
            revisionNumber: fact.currentRevision.revisionNumber + 1,
            revisionSequence: fact.currentRevision.revisionSequence + 1,
            content: "Updated in another session",
          },
        };
        facts = [fact];
        return json(
          route,
          {
            detail: {
              code: "PRIVATE_WORK_CONFLICT",
              message: "Memory changed while editing",
            },
          },
          409,
        );
      }
      fact = {
        ...fact,
        version: fact.version + 1,
        currentRevision: {
          ...fact.currentRevision,
          revisionNumber: fact.currentRevision.revisionNumber + 1,
          revisionSequence: fact.currentRevision.revisionSequence + 1,
          content: body.content ?? fact.currentRevision.content,
          category: body.category ?? fact.currentRevision.category,
          confidence: body.confidence ?? fact.currentRevision.confidence,
        },
      };
      facts = [fact];
      return json(route, fact);
    }
    if (
      path === `${memoryBase}/facts/${FACT_ID}/disable` &&
      request.method() === "POST"
    ) {
      fact = {
        ...fact,
        status: "disabled",
        version: fact.version + 1,
        disabledAt: TIMESTAMP,
      };
      facts = [fact];
      return json(route, fact);
    }
    if (
      path === `${memoryBase}/facts/${FACT_ID}/restore` &&
      request.method() === "POST"
    ) {
      fact = {
        ...fact,
        status: "active",
        version: fact.version + 1,
        disabledAt: null,
      };
      facts = [fact];
      return json(route, fact);
    }
    if (
      path === `${memoryBase}/facts/${FACT_ID}/hard-forget` &&
      request.method() === "POST"
    ) {
      facts = [];
      return json(route, {
        factId: FACT_ID,
        version: fact.version + 1,
        status: "deleted",
        erasedCandidates: 1,
        erasedRevisions: 2,
        erasedEvidence: 1,
        erasedSourceItems: 1,
      });
    }
    const candidateAcceptMatch = new RegExp(
      `^${memoryBase}/candidates/([^/]+)/accept$`,
      "u",
    ).exec(path);
    if (candidateAcceptMatch && request.method() === "POST") {
      candidates = candidates.filter(
        (item) => item.id !== candidateAcceptMatch[1],
      );
      return json(route, fact);
    }
    const candidateRejectMatch = new RegExp(
      `^${memoryBase}/candidates/([^/]+)/reject$`,
      "u",
    ).exec(path);
    if (candidateRejectMatch && request.method() === "POST") {
      const rejected = candidates.find(
        (item) => item.id === candidateRejectMatch[1],
      );
      candidates = candidates.filter(
        (item) => item.id !== candidateRejectMatch[1],
      );
      return json(route, { ...rejected, status: "rejected" });
    }
    return json(route, { detail: "not found" }, 404);
  });
}

test("the Memory v2 workbench completes its core management flow", async ({
  page,
}) => {
  await mockProjectMemoryRoute(page);

  await page.goto("/projects/alpha/memory");

  await expect(page).toHaveURL(/\/projects\/alpha\/memory$/u);
  await expect(page.getByTestId("project-shell")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Memory", exact: true }),
  ).toBeVisible();
  for (const tab of [
    "Long-term memory",
    "Pending review",
    "Change history",
    "Settings",
  ]) {
    await expect(page.getByRole("tab", { name: tab })).toBeVisible();
  }

  const factRow = page
    .locator("article")
    .filter({ hasText: "Prefers executable implementation plans" });
  await factRow.getByRole("button", { name: "Edit" }).click();
  await page
    .getByRole("dialog")
    .getByRole("textbox", { name: "Content", exact: true })
    .fill("Prefers executable plans with focused acceptance tests");
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText("Updated in another session")).toBeVisible();

  const refreshedFactRow = page
    .locator("article")
    .filter({ hasText: "Updated in another session" });
  await refreshedFactRow.getByRole("button", { name: "Edit" }).click();
  await page
    .getByRole("dialog")
    .getByRole("textbox", { name: "Content", exact: true })
    .fill("Prefers executable plans with focused acceptance tests");
  await page.getByRole("button", { name: "Save revision" }).click();
  await expect(
    page.getByText("Prefers executable plans with focused acceptance tests"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Stop recall" }).click();
  await expect(
    page.getByText("Prefers executable plans with focused acceptance tests"),
  ).toHaveCount(0);
  await page.getByRole("combobox").click();
  await page.getByRole("option", { name: "Disabled" }).click();
  await page.getByRole("button", { name: "Restore" }).click();
  await expect(page.getByText("No matching facts")).toBeVisible();
  await page.getByRole("combobox").click();
  await page.getByRole("option", { name: "Active" }).click();

  await page.getByRole("tab", { name: "Pending review" }).click();
  const acceptedCandidate = page
    .locator("article")
    .filter({ hasText: "Keep acceptance criteria explicit" });
  await acceptedCandidate.getByRole("button", { name: "Accept" }).click();
  await expect(page.getByText("Keep acceptance criteria explicit")).toHaveCount(
    0,
  );
  const rejectedCandidate = page
    .locator("article")
    .filter({ hasText: "Temporary conversational detail" });
  await rejectedCandidate.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByText("Nothing is waiting for review")).toBeVisible();

  await page.getByRole("tab", { name: "Long-term memory" }).click();
  await page.getByRole("button", { name: "View history" }).click();
  await expect(
    page.getByRole("heading", { name: "Change history" }),
  ).toBeVisible();
  await expect(
    page.getByText("The user requested executable plans."),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Long-term memory" }).click();

  await page.getByRole("button", { name: "Forget permanently" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Forget permanently" })
    .click();
  await expect(page.getByText("No long-term facts yet")).toBeVisible();

  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByText("Memory v2")).toBeVisible();
  await expect(page.getByText("120 minutes")).toBeVisible();
  await expect(page.getByText("30 days")).toBeVisible();
});
