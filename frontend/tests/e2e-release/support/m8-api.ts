import {
  expect,
  type APIResponse,
  type BrowserContext,
} from "@playwright/test";
import { z } from "zod";

const BASE_URL = "http://127.0.0.1:2026";
const ORIGIN_HEADERS = { Origin: BASE_URL } as const;

const projectSchema = z
  .object({
    id: z.string().uuid(),
    slug: z.string().min(1),
    role: z.enum(["admin", "editor", "runner", "viewer"]),
    capabilities: z.array(z.string()),
  })
  .passthrough();

const projectPageSchema = z
  .object({ items: z.array(projectSchema), next_cursor: z.string().nullable() })
  .strict();

const invitationSchema = z
  .object({ invite_url_fragment: z.string().startsWith("/invite#token=") })
  .passthrough();

const membershipSchema = z
  .object({
    membership_id: z.string().uuid(),
    account_email: z.string().email(),
    role: z.enum(["admin", "editor", "runner", "viewer"]),
    version: z.number().int().positive(),
  })
  .passthrough();

const assetSchema = z
  .object({
    id: z.string().uuid(),
    scope: z.enum(["system", "project"]),
    current_published_version_id: z.string().uuid().nullable(),
    status: z.string().min(1),
    capabilities: z.array(z.string()),
    binding: z
      .object({
        version_id: z.string().uuid(),
        enabled: z.boolean(),
        version: z.number().int().positive(),
      })
      .passthrough()
      .nullable(),
  })
  .passthrough();

const assetPageSchema = z
  .object({
    system_items: z.array(assetSchema),
    project_items: z.array(assetSchema),
  })
  .passthrough();

const threadSchema = z
  .object({
    thread_id: z.string().uuid(),
    version: z.number().int().positive(),
  })
  .passthrough();

const fileSchema = z
  .object({ id: z.string().uuid(), status: z.literal("ready") })
  .passthrough();

const memorySchema = z
  .object({
    version: z.number().int().nonnegative(),
    memory: z
      .object({
        facts: z.array(z.object({ id: z.string().min(1) }).passthrough()),
      })
      .passthrough(),
  })
  .passthrough();

const automationSchema = z
  .object({ id: z.string().min(1), version: z.number().int().positive() })
  .passthrough();

export type ProjectRef = z.infer<typeof projectSchema>;

export interface AccountFixture {
  email: string;
  password: string;
}

export interface PrivateFixture {
  threadId: string;
  threadVersion: number;
  fileId: string;
  factId: string;
  memoryVersion: number;
  automationId: string;
}

export function syntheticAccount(label: string): AccountFixture {
  const nonce = crypto.randomUUID().replaceAll("-", "");
  return {
    email: `m8-${label}-${nonce}@example.com`,
    password: `M8-${label}-${nonce}-Aa9!`,
  };
}

async function expectStatus(
  response: APIResponse,
  status: number,
): Promise<void> {
  expect(response.status()).toBe(status);
}

function expectNoForbiddenKeys(
  value: unknown,
  forbidden: ReadonlySet<string>,
): void {
  if (Array.isArray(value)) {
    value.forEach((item) => expectNoForbiddenKeys(item, forbidden));
    return;
  }
  if (value === null || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    expect(forbidden.has(key.toLowerCase())).toBe(false);
    expectNoForbiddenKeys(item, forbidden);
  }
}

async function csrfHeaders(
  context: BrowserContext,
): Promise<Record<string, string>> {
  const cookies = await context.cookies(BASE_URL);
  const csrf = cookies.find(({ name }) => name === "csrf_token")?.value;
  expect(csrf).toBeTruthy();
  return { ...ORIGIN_HEADERS, "x-csrf-token": csrf! };
}

export async function initializeAdmin(
  context: BrowserContext,
): Promise<AccountFixture> {
  const credentials = syntheticAccount("system-admin");
  const response = await context.request.post("/api/v1/auth/initialize", {
    data: credentials,
    headers: ORIGIN_HEADERS,
  });
  await expectStatus(response, 201);
  return credentials;
}

export async function registerAccount(
  context: BrowserContext,
  label: string,
): Promise<AccountFixture> {
  const credentials = syntheticAccount(label);
  const response = await context.request.post("/api/v1/auth/register", {
    data: credentials,
    headers: ORIGIN_HEADERS,
  });
  await expectStatus(response, 201);
  return credentials;
}

export async function createProject(
  context: BrowserContext,
  label: string,
): Promise<ProjectRef> {
  const slug = `m8-${label}-${crypto.randomUUID().slice(0, 12)}`;
  const response = await context.request.post("/api/projects", {
    data: {
      slug,
      display_name: `M8 ${label}`,
      description: "M8 synthetic release project",
      icon: "folder",
    },
    headers: await csrfHeaders(context),
  });
  await expectStatus(response, 201);
  return projectSchema.parse(await response.json());
}

export async function listProjects(
  context: BrowserContext,
): Promise<ProjectRef[]> {
  const response = await context.request.get("/api/projects?limit=100");
  await expectStatus(response, 200);
  return projectPageSchema.parse(await response.json()).items;
}

export async function expectProjectNotFound(
  context: BrowserContext,
  projectId: string,
): Promise<void> {
  const response = await context.request.get(`/api/projects/${projectId}`);
  await expectStatus(response, 404);
}

export async function inviteRole(
  admin: BrowserContext,
  invited: BrowserContext,
  project: ProjectRef,
  role: "editor" | "runner" | "viewer",
  label: string,
): Promise<AccountFixture> {
  const credentials = await registerAccount(invited, label);
  const created = await admin.request.post(
    `/api/projects/${project.id}/invitations`,
    {
      data: { email: credentials.email, role },
      headers: await csrfHeaders(admin),
    },
  );
  await expectStatus(created, 201);
  const fragment = invitationSchema.parse(
    await created.json(),
  ).invite_url_fragment;
  const token = fragment.slice(fragment.indexOf("#token=") + 7);
  const claim = await invited.request.post("/api/project-invitations/claim", {
    data: { token },
    headers: ORIGIN_HEADERS,
  });
  await expectStatus(claim, 200);
  const redeemed = await invited.request.post(
    "/api/project-invitations/redeem",
    { headers: await csrfHeaders(invited) },
  );
  await expectStatus(redeemed, 200);
  return credentials;
}

export async function expectRole(
  context: BrowserContext,
  projectId: string,
  role: ProjectRef["role"],
): Promise<void> {
  const projects = await listProjects(context);
  expect(projects.find(({ id }) => id === projectId)?.role).toBe(role);
}

export async function expectCapabilities(
  context: BrowserContext,
  projectId: string,
  expected: readonly string[],
  forbidden: readonly string[],
): Promise<void> {
  const projects = await listProjects(context);
  const project = projects.find(({ id }) => id === projectId);
  expect(project).toBeDefined();
  expect(project?.capabilities).toEqual(expect.arrayContaining([...expected]));
  for (const capability of forbidden) {
    expect(project?.capabilities).not.toContain(capability);
  }
}

export async function enterProject(
  context: BrowserContext,
  projectId: string,
): Promise<void> {
  const response = await context.request.post(
    `/api/projects/${projectId}/enter`,
    {
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(response, 200);
}

export async function bindExecutableSystemAgent(
  context: BrowserContext,
  projectId: string,
): Promise<string> {
  for (const [segment, bindingKind] of [
    ["skills", "skill"],
    ["mcp-servers", "mcp"],
  ] as const) {
    const dependencies = await context.request.get(
      `/api/projects/${projectId}/${segment}`,
    );
    await expectStatus(dependencies, 200);
    const dependencyPage = assetPageSchema.parse(await dependencies.json());
    for (const dependency of dependencyPage.system_items) {
      const versionId = dependency.current_published_version_id;
      if (
        versionId === null ||
        (dependency.binding?.enabled === true &&
          dependency.binding.version_id === versionId)
      ) {
        continue;
      }
      const bound = await context.request.post(
        `/api/projects/${projectId}/system-${bindingKind}-bindings`,
        {
          data: {
            asset_id: dependency.id,
            version_id: versionId,
            ...(dependency.binding === null
              ? {}
              : { expected_binding_version: dependency.binding.version }),
          },
          headers: await csrfHeaders(context),
        },
      );
      await expectStatus(bound, 201);
    }
  }
  const listed = await context.request.get(`/api/projects/${projectId}/agents`);
  await expectStatus(listed, 200);
  const page = assetPageSchema.parse(await listed.json());
  const selected = page.system_items.find(
    ({ current_published_version_id: versionId }) => versionId !== null,
  );
  expect(selected).toBeDefined();
  const versionId = selected?.current_published_version_id;
  expect(versionId).toBeTruthy();
  const bound = await context.request.post(
    `/api/projects/${projectId}/system-agent-bindings`,
    {
      data: { asset_id: selected?.id, version_id: versionId },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(bound, 201);
  return selected!.id;
}

export async function assertSharedAssetsAreSafe(
  context: BrowserContext,
  projectId: string,
): Promise<void> {
  for (const segment of ["agents", "skills", "mcp-servers"] as const) {
    const response = await context.request.get(
      `/api/projects/${projectId}/${segment}`,
    );
    await expectStatus(response, 200);
    const raw: unknown = await response.json();
    const page = assetPageSchema.parse(raw);
    expect(page.system_items.length).toBeGreaterThan(0);
    expectNoForbiddenKeys(
      raw,
      new Set([
        "prompt",
        "content",
        "archive",
        "ciphertext",
        "nonce",
        "secret",
        "api_key",
      ]),
    );
  }
}

export async function changeMemberRole(
  admin: BrowserContext,
  projectId: string,
  email: string,
  role: "editor" | "runner" | "viewer",
): Promise<void> {
  const listed = await admin.request.get(`/api/projects/${projectId}/members`);
  await expectStatus(listed, 200);
  const members = z.array(membershipSchema).parse(await listed.json());
  const member = members.find(
    ({ account_email: accountEmail }) => accountEmail === email,
  );
  expect(member).toBeDefined();
  const response = await admin.request.patch(
    `/api/projects/${projectId}/members/${member?.membership_id}`,
    {
      data: { role, version: member?.version },
      headers: await csrfHeaders(admin),
    },
  );
  await expectStatus(response, 200);
}

function syntheticMemory(): Record<string, unknown> {
  const timestamp = new Date().toISOString();
  const emptySummary = { summary: "", updatedAt: "" };
  return {
    version: "1.0",
    lastUpdated: timestamp,
    user: {
      workContext: {
        summary: "M8 synthetic work context",
        updatedAt: timestamp,
      },
      personalContext: emptySummary,
      topOfMind: emptySummary,
    },
    history: {
      recentMonths: emptySummary,
      earlierContext: emptySummary,
      longTermBackground: emptySummary,
    },
    facts: [
      {
        content: "M8 synthetic private fact",
        category: "context",
        confidence: 0.9,
        source: "manual",
      },
    ],
  };
}

export async function createPrivateFixture(
  context: BrowserContext,
  projectId: string,
  agentId: string,
): Promise<PrivateFixture> {
  const threadId = crypto.randomUUID();
  const threadResponse = await context.request.post(
    `/api/projects/${projectId}/private-work/threads`,
    {
      data: {
        thread_id: threadId,
        agent_asset_id: agentId,
        agent_scope: "system",
        display_name: "M8 synthetic private thread",
        metadata: {},
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(threadResponse, 201);
  const thread = threadSchema.parse(await threadResponse.json());

  const upload = await context.request.post(
    `/api/projects/${projectId}/private-work/threads/${threadId}/uploads`,
    {
      multipart: {
        file: {
          name: "m8-synthetic.txt",
          mimeType: "text/plain",
          buffer: Buffer.from("M8 synthetic private file\n", "utf8"),
        },
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(upload, 201);
  const file = fileSchema.parse(await upload.json());

  const initialMemory = await context.request.get(
    `/api/projects/${projectId}/memory`,
  );
  await expectStatus(initialMemory, 200);
  const initial = memorySchema.parse(await initialMemory.json());
  const importedMemory = await context.request.post(
    `/api/projects/${projectId}/memory/import`,
    {
      data: { expected_version: initial.version, memory: syntheticMemory() },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(importedMemory, 200);
  const memory = memorySchema.parse(await importedMemory.json());

  const automationResponse = await context.request.post(
    `/api/projects/${projectId}/automations`,
    {
      data: {
        title: "M8 synthetic automation",
        prompt: "M8 synthetic prompt without private data",
        context_mode: "reuse_thread",
        thread_id: threadId,
        agent_asset_id: agentId,
        agent_scope: "system",
        schedule_type: "cron",
        schedule_spec: { cron: "0 0 1 1 *" },
        timezone: "UTC",
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(automationResponse, 201);
  const automation = automationSchema.parse(await automationResponse.json());
  return {
    threadId,
    threadVersion: thread.version,
    fileId: file.id,
    factId: memory.memory.facts[0]!.id,
    memoryVersion: memory.version,
    automationId: automation.id,
  };
}

export async function assertCredentialResponsesAreSafe(
  context: BrowserContext,
  projectId: string,
): Promise<void> {
  const response = await context.request.get(
    `/api/projects/${projectId}/credentials`,
  );
  await expectStatus(response, 200);
  expectNoForbiddenKeys(
    await response.json(),
    new Set([
      "ciphertext",
      "nonce",
      "storage_locator",
      "private_key",
      "access_token",
    ]),
  );
}

export async function assertViewerBoundaries(
  context: BrowserContext,
  projectId: string,
  fixture: PrivateFixture,
): Promise<void> {
  const search = await context.request.post(
    `/api/projects/${projectId}/private-work/threads/search`,
    { data: { limit: 10, offset: 0 }, headers: await csrfHeaders(context) },
  );
  await expectStatus(search, 200);
  expect(JSON.stringify(await search.json())).toContain(fixture.threadId);

  const files = await context.request.get(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}/uploads`,
  );
  await expectStatus(files, 200);
  expect(JSON.stringify(await files.json())).toContain(fixture.fileId);

  const downloaded = await context.request.get(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}/files/${fixture.fileId}`,
  );
  await expectStatus(downloaded, 200);

  const memory = await context.request.get(
    `/api/projects/${projectId}/memory/export`,
  );
  await expectStatus(memory, 200);
  expect(JSON.stringify(await memory.json())).toContain(fixture.factId);

  const automations = await context.request.get(
    `/api/projects/${projectId}/automations?limit=50&offset=0`,
  );
  await expectStatus(automations, 200);
  expect(JSON.stringify(await automations.json())).toContain(
    fixture.automationId,
  );

  const denied = await context.request.post(
    `/api/projects/${projectId}/memory/import`,
    {
      data: { expected_version: 0, memory: {} },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(denied, 403);

  const createDenied = await context.request.post(
    `/api/projects/${projectId}/private-work/threads`,
    {
      data: {
        thread_id: crypto.randomUUID(),
        agent_asset_id: crypto.randomUUID(),
        agent_scope: "system",
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(createDenied, 403);

  const runDenied = await context.request.post(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}/runs`,
    {
      data: { input: "M8 viewer denial probe" },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(runDenied, 403);

  const automationDenied = await context.request.post(
    `/api/projects/${projectId}/automations/${fixture.automationId}/trigger`,
    {
      headers: {
        ...(await csrfHeaders(context)),
        "Idempotency-Key": crypto.randomUUID(),
      },
    },
  );
  await expectStatus(automationDenied, 403);

  const deleteFile = await context.request.delete(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}/uploads?file_id=${fixture.fileId}`,
    { headers: await csrfHeaders(context) },
  );
  await expectStatus(deleteFile, 200);

  const deleteFact = await context.request.delete(
    `/api/projects/${projectId}/memory/facts/${fixture.factId}`,
    {
      data: { expected_version: fixture.memoryVersion },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(deleteFact, 200);

  const deleteThread = await context.request.delete(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}?expected_version=${fixture.threadVersion}`,
    { headers: await csrfHeaders(context) },
  );
  await expectStatus(deleteThread, 200);
}

export async function assertPrivateFixtureHidden(
  context: BrowserContext,
  projectId: string,
  fixture: PrivateFixture,
): Promise<void> {
  const thread = await context.request.get(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}`,
  );
  await expectStatus(thread, 404);
  const file = await context.request.get(
    `/api/projects/${projectId}/private-work/threads/${fixture.threadId}/files/${fixture.fileId}`,
  );
  await expectStatus(file, 404);
  const memory = await context.request.get(
    `/api/projects/${projectId}/memory/export`,
  );
  await expectStatus(memory, 200);
  expect(JSON.stringify(await memory.json())).not.toContain(fixture.factId);
  const automations = await context.request.get(
    `/api/projects/${projectId}/automations?limit=50&offset=0`,
  );
  await expectStatus(automations, 200);
  expect(JSON.stringify(await automations.json())).not.toContain(
    fixture.automationId,
  );
}
