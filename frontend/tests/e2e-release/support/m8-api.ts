import { spawn } from "node:child_process";
import { createConnection } from "node:net";

import {
  expect,
  type APIResponse,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";
import { z } from "zod";

import type { M8LiveBrowserResult, M8LiveModelSummary } from "./m8-result";

const BASE_URL = "http://127.0.0.1:2026";
const ORIGIN_HEADERS = { Origin: BASE_URL } as const;

const uuidSchema = z.string().uuid();

const accountResponseSchema = z
  .object({ id: uuidSchema, email: z.string().email() })
  .passthrough();

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

const assetMutationSchema = z
  .object({
    item: z
      .object({ id: uuidSchema, version: z.number().int().positive() })
      .passthrough(),
  })
  .passthrough();

const agentVersionMutationSchema = z
  .object({
    data: z
      .object({
        id: uuidSchema,
        workflow_status: z.enum(["draft", "published", "archived"]),
        model_ref: z.string(),
        tool_groups: z.array(z.string()),
      })
      .passthrough(),
  })
  .passthrough();

const liveProbeHandoffSchema = z
  .object({
    status: z.literal("passed"),
    frame_count: z.number().int().min(2),
    tool_call_count: z.number().int().min(1),
    terminal_count: z.literal(1),
    cursor_count: z.number().int().min(2),
    artifact_id: uuidSchema,
  })
  .strict();

export type ProjectRef = z.infer<typeof projectSchema>;

export interface AccountFixture {
  userId: string;
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

export interface LivePublicHandle {
  projectId: string;
  threadId: string;
  runId: string;
  artifactId: string;
}

export interface PinnedLiveAgent {
  ownerUserId: string;
  project: ProjectRef;
  agentId: string;
  threadId: string;
  outputPath: string;
  publicHandle: LivePublicHandle;
  frameIds: number[];
  summary: M8LiveModelSummary | null;
  replayPassed: boolean;
  privateDenials: number;
}

export interface RecoveryAuthorityHandoff {
  admin: { user_id: string; email: string; password: string };
  outsider: { user_id: string; email: string; password: string };
  purge_project: { project_id: string; slug: string };
  purge_thread_id: string;
  purge_file_id: string;
  live_project: { project_id: string; slug: string };
  live: LivePublicHandle;
}

export function syntheticAccount(label: string): AccountFixture {
  const nonce = crypto.randomUUID().replaceAll("-", "");
  return {
    userId: "",
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
  credentials.userId = accountResponseSchema.parse(await response.json()).id;
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
  credentials.userId = accountResponseSchema.parse(await response.json()).id;
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

export async function createPinnedLiveAgent(
  context: BrowserContext,
  project: ProjectRef,
  options: { modelRef: string; toolGroups: ["file:read", "file:write"] },
): Promise<PinnedLiveAgent> {
  expect(options.modelRef).toBeTruthy();
  expect(options.toolGroups).toEqual(["file:read", "file:write"]);
  const current = await context.request.get("/api/v1/auth/me");
  await expectStatus(current, 200);
  const owner = accountResponseSchema.parse(await current.json());
  const suffix = crypto.randomUUID().replaceAll("-", "");
  const created = await context.request.post(
    `/api/projects/${project.id}/agents`,
    {
      data: {
        slug: `m8-live-${suffix.slice(0, 16)}`,
        display_name: "M8 live synthetic Agent",
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(created, 201);
  const asset = assetMutationSchema.parse(await created.json()).item;
  const versionResponse = await context.request.post(
    `/api/projects/${project.id}/agents/${asset.id}/versions`,
    {
      data: {
        description: "M8 bounded live release Agent",
        soul: "Follow the synthetic request and use only the approved file tools.",
        model_ref: options.modelRef,
        tool_groups: options.toolGroups,
        skill_version_ids: [],
        mcp_version_ids: [],
        expected_asset_version: asset.version,
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(versionResponse, 201);
  const version = agentVersionMutationSchema.parse(
    await versionResponse.json(),
  ).data;
  expect(version.model_ref).toBe(options.modelRef);
  expect(version.tool_groups).toEqual(options.toolGroups);
  const publishedResponse = await context.request.post(
    `/api/projects/${project.id}/agents/${asset.id}/versions/${version.id}/publish`,
    {
      data: { expected_asset_version: asset.version + 1 },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(publishedResponse, 200);
  const published = agentVersionMutationSchema.parse(
    await publishedResponse.json(),
  ).data;
  expect(published.id).toBe(version.id);
  expect(published.workflow_status).toBe("published");

  const threadId = crypto.randomUUID();
  const threadResponse = await context.request.post(
    `/api/projects/${project.id}/private-work/threads`,
    {
      data: {
        thread_id: threadId,
        agent_asset_id: asset.id,
        agent_scope: "project",
        display_name: "M8 live synthetic thread",
        metadata: {},
      },
      headers: await csrfHeaders(context),
    },
  );
  await expectStatus(threadResponse, 201);
  threadSchema.parse(await threadResponse.json());
  return {
    ownerUserId: owner.id,
    project,
    agentId: asset.id,
    threadId,
    outputPath: `/mnt/user-data/outputs/m8-${suffix}.txt`,
    publicHandle: {
      projectId: project.id,
      threadId,
      runId: "",
      artifactId: "",
    },
    frameIds: [],
    summary: null,
    replayPassed: false,
    privateDenials: 0,
  };
}

interface ParsedSseFrame {
  id: number;
  event: string;
}

function parseBoundedSse(body: string): ParsedSseFrame[] {
  if (Buffer.byteLength(body, "utf8") > 512 * 1024) {
    throw new Error("M8_LIVE_STREAM_TOO_LARGE");
  }
  const frames: ParsedSseFrame[] = [];
  for (const block of body.replaceAll("\r\n", "\n").split("\n\n")) {
    let id: number | null = null;
    let event = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("id:")) {
        const value = line.slice(3).trim();
        if (/^[1-9][0-9]*$/u.test(value)) id = Number(value);
      } else if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      }
    }
    if (id !== null && Number.isSafeInteger(id) && event) {
      frames.push({ id, event });
    }
  }
  return frames;
}

async function runLiveDatabaseProbe(
  live: PinnedLiveAgent,
): Promise<z.infer<typeof liveProbeHandoffSchema>> {
  const python = process.env.M8_LIVE_PROBE_PYTHON;
  const databaseUrl = process.env.M8_LIVE_DATABASE_URL;
  const backendRoot = process.env.M8_LIVE_PROBE_CWD;
  if (!python || !databaseUrl || !backendRoot) {
    throw new Error("M8_LIVE_PROBE_CONFIG_REQUIRED");
  }
  const environment: NodeJS.ProcessEnv = {
    NODE_ENV: process.env.NODE_ENV ?? "production",
  };
  for (const name of ["HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "TZ"]) {
    if (process.env[name]) environment[name] = process.env[name];
  }
  environment.M8_LIVE_DATABASE_URL = databaseUrl;
  return await new Promise((resolve, reject) => {
    const child = spawn(
      python,
      ["-m", "scripts.release_acceptance.live_probe"],
      {
        cwd: backendRoot,
        env: environment,
        stdio: ["pipe", "pipe", "ignore"],
      },
    );
    const chunks: Buffer[] = [];
    let size = 0;
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("M8_LIVE_PROBE_TIMEOUT"));
    }, 30_000);
    child.on("error", () => {
      clearTimeout(timeout);
      reject(new Error("M8_LIVE_PROBE_FAILED"));
    });
    child.stdout.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > 4096) {
        child.kill("SIGTERM");
        return;
      }
      chunks.push(chunk);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code !== 0 || size > 4096) {
        reject(new Error("M8_LIVE_PROBE_FAILED"));
        return;
      }
      try {
        resolve(
          liveProbeHandoffSchema.parse(
            JSON.parse(Buffer.concat(chunks).toString("utf8")),
          ),
        );
      } catch {
        reject(new Error("M8_LIVE_PROBE_FAILED"));
      }
    });
    child.stdin.end(
      JSON.stringify({
        project_id: live.project.id,
        owner_user_id: live.ownerUserId,
        thread_id: live.threadId,
        run_id: live.publicHandle.runId,
      }),
    );
  });
}

export async function submitSyntheticToolPrompt(
  page: Page,
  live: PinnedLiveAgent,
): Promise<void> {
  const startedAt = performance.now();
  await page.goto(`/projects/${live.project.slug}/chats/${live.threadId}`);
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith(
        `/threads/${live.threadId}/runs/stream`,
      ),
    { timeout: 300_000 },
  );
  const prompt = [
    "First call write_file exactly once.",
    `Write the exact text M8 synthetic live proof to ${live.outputPath}.`,
    "Then call present_files exactly once for that same path.",
    "Do not call any other tool. After both tools succeed, finish with a short confirmation.",
  ].join(" ");
  const composer = page.getByPlaceholder(/how can i assist you/i);
  await composer.fill(prompt);
  await page.getByLabel("Submit").click();
  const response = await responsePromise;
  if (response.status() !== 200) {
    let publicCode = "UNKNOWN";
    try {
      const payload: unknown = await response.json();
      if (typeof payload === "object" && payload !== null) {
        const detail = Reflect.get(payload, "detail");
        if (typeof detail === "object" && detail !== null) {
          const code = Reflect.get(detail, "code");
          if (typeof code === "string" && /^[A-Z0-9_]+$/u.test(code)) {
            publicCode = code;
          }
        }
      }
    } catch {
      publicCode = "UNKNOWN";
    }
    throw new Error(`M8_LIVE_RUN_HTTP_${response.status()}_${publicCode}`);
  }
  const contentLocation = response.headers()["content-location"] ?? "";
  const runId = /\/runs\/([0-9a-f-]{36})$/iu.exec(contentLocation)?.[1];
  expect(runId).toBeTruthy();
  const frames = parseBoundedSse(await response.text());
  const frameIds = frames.map(({ id }) => id);
  expect(frameIds.length).toBeGreaterThan(1);
  expect(new Set(frameIds).size).toBe(frameIds.length);
  expect(frames.filter(({ event }) => event === "end")).toHaveLength(1);
  live.publicHandle.runId = uuidSchema.parse(runId);
  live.frameIds = frameIds;
  const probe = await runLiveDatabaseProbe(live);
  expect(probe.frame_count).toBeGreaterThan(1);
  expect(probe.terminal_count).toBe(1);
  live.publicHandle.artifactId = probe.artifact_id;
  live.summary = {
    provider: "deepseek",
    logical_model_name: process.env.M8_LOGICAL_MODEL_NAME!,
    provider_model_id: "deepseek-v4-pro",
    outcome: "completed",
    frame_count: probe.frame_count,
    tool_call_count: probe.tool_call_count,
    terminal_count: probe.terminal_count,
    cursor_count: probe.cursor_count,
    duration_ms: Math.max(0, Math.round(performance.now() - startedAt)),
  };
}

export async function expectRunTerminal(
  page: Page,
  live: PinnedLiveAgent,
): Promise<void> {
  expect(live.summary?.terminal_count).toBe(1);
  const response = await page
    .context()
    .request.get(
      `/api/projects/${live.project.id}/private-work/threads/${live.threadId}/runs/${live.publicHandle.runId}`,
    );
  await expectStatus(response, 200);
  expect(
    z
      .object({ status: z.literal("success") })
      .passthrough()
      .parse(await response.json()).status,
  ).toBe("success");
  await expect(page.getByPlaceholder(/how can i assist you/i)).toBeEnabled();
}

export async function expectToolResultVisible(
  page: Page,
  live: PinnedLiveAgent,
): Promise<void> {
  expect(live.summary?.tool_call_count).toBeGreaterThanOrEqual(1);
  await expect(page.getByText(live.outputPath, { exact: true })).toBeVisible({
    timeout: 30_000,
  });
}

async function requestGatewayRestart(): Promise<void> {
  const port = Number(process.env.M8_GATEWAY_CONTROL_PORT);
  const token = process.env.M8_GATEWAY_CONTROL_TOKEN;
  if (!Number.isSafeInteger(port) || port < 1 || !token) {
    throw new Error("M8_GATEWAY_CONTROL_REQUIRED");
  }
  await new Promise<void>((resolve, reject) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    let response = "";
    socket.setTimeout(90_000);
    socket.on("connect", () => {
      socket.write(`restart_gateway ${token}\n`);
    });
    socket.on("data", (chunk: Buffer) => {
      response += chunk.toString("utf8");
      if (response.length > 16) socket.destroy();
    });
    socket.on("end", () => {
      if (response === "ok\n") resolve();
      else reject(new Error("M8_GATEWAY_RESTART_FAILED"));
    });
    socket.on("timeout", () => {
      socket.destroy();
      reject(new Error("M8_GATEWAY_RESTART_TIMEOUT"));
    });
    socket.on("error", () => {
      reject(new Error("M8_GATEWAY_RESTART_FAILED"));
    });
  });
}

export async function submitRecoveryAuthority(
  authority: RecoveryAuthorityHandoff,
): Promise<void> {
  const port = Number(process.env.M8_GATEWAY_CONTROL_PORT);
  const token = process.env.M8_GATEWAY_CONTROL_TOKEN;
  if (!Number.isSafeInteger(port) || port < 1 || !token) {
    throw new Error("M8_GATEWAY_CONTROL_REQUIRED");
  }
  const encoded = Buffer.from(JSON.stringify(authority), "utf8").toString(
    "base64",
  );
  if (encoded.length > 30_000) {
    throw new Error("M8_RECOVERY_AUTHORITY_TOO_LARGE");
  }
  await new Promise<void>((resolve, reject) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    let response = "";
    socket.setTimeout(10_000);
    socket.on("connect", () => {
      socket.write(`recovery_authority ${token} ${encoded}\n`);
    });
    socket.on("data", (chunk: Buffer) => {
      response += chunk.toString("utf8");
      if (response.length > 16) socket.destroy();
    });
    socket.on("end", () => {
      if (response === "ok\n") resolve();
      else reject(new Error("M8_RECOVERY_AUTHORITY_FAILED"));
    });
    socket.on("timeout", () => {
      socket.destroy();
      reject(new Error("M8_RECOVERY_AUTHORITY_TIMEOUT"));
    });
    socket.on("error", () => {
      reject(new Error("M8_RECOVERY_AUTHORITY_FAILED"));
    });
  });
}

const recoveryProbeEnvironmentSchema = z
  .object({
    phase: z.enum(["restore", "source"]),
    adminEmail: z.string().email(),
    adminPassword: z.string().min(1),
    outsiderEmail: z.string().email(),
    outsiderPassword: z.string().min(1),
    purgeProjectId: uuidSchema,
    purgeThreadId: uuidSchema,
    purgeFileId: uuidSchema,
    liveProjectId: uuidSchema,
    liveThreadId: uuidSchema,
    liveRunId: uuidSchema,
    liveArtifactId: uuidSchema,
  })
  .strict();

function recoveryProbeEnvironment() {
  return recoveryProbeEnvironmentSchema.parse({
    phase: process.env.M8_RECOVERY_PHASE,
    adminEmail: process.env.M8_RECOVERY_ADMIN_EMAIL,
    adminPassword: process.env.M8_RECOVERY_ADMIN_PASSWORD,
    outsiderEmail: process.env.M8_RECOVERY_OUTSIDER_EMAIL,
    outsiderPassword: process.env.M8_RECOVERY_OUTSIDER_PASSWORD,
    purgeProjectId: process.env.M8_RECOVERY_PURGE_PROJECT_ID,
    purgeThreadId: process.env.M8_RECOVERY_PURGE_THREAD_ID,
    purgeFileId: process.env.M8_RECOVERY_PURGE_FILE_ID,
    liveProjectId: process.env.M8_RECOVERY_LIVE_PROJECT_ID,
    liveThreadId: process.env.M8_RECOVERY_LIVE_THREAD_ID,
    liveRunId: process.env.M8_RECOVERY_LIVE_RUN_ID,
    liveArtifactId: process.env.M8_RECOVERY_LIVE_ARTIFACT_ID,
  });
}

async function loginRecoveryAccount(
  context: BrowserContext,
  email: string,
  password: string,
): Promise<void> {
  const response = await context.request.post("/api/v1/auth/login/local", {
    data: { email, password },
    headers: ORIGIN_HEADERS,
  });
  await expectStatus(response, 200);
}

export async function runRecoveryBrowserProbe(
  browser: Browser,
): Promise<{ phase: "restore" | "source"; boundariesPassed: number }> {
  const authority = recoveryProbeEnvironment();
  const admin = await browser.newContext();
  const outsider = await browser.newContext();
  try {
    await loginRecoveryAccount(
      admin,
      authority.adminEmail,
      authority.adminPassword,
    );
    await loginRecoveryAccount(
      outsider,
      authority.outsiderEmail,
      authority.outsiderPassword,
    );
    const health = await admin.request.get("/health");
    await expectStatus(health, 200);
    expect(await health.json()).toEqual({
      status: "healthy",
      service: "deer-flow-gateway",
    });
    const projects = await listProjects(admin);
    expect(projects.map(({ id }) => id)).toContain(authority.liveProjectId);
    const liveProject = await admin.request.get(
      `/api/projects/${authority.liveProjectId}`,
    );
    await expectStatus(liveProject, 200);
    const thread = await admin.request.get(
      `/api/projects/${authority.liveProjectId}/private-work/threads/${authority.liveThreadId}`,
    );
    await expectStatus(thread, 200);
    if (authority.phase === "source") {
      return { phase: "source", boundariesPassed: 4 };
    }
    const run = await admin.request.get(
      `/api/projects/${authority.liveProjectId}/private-work/threads/${authority.liveThreadId}/runs/${authority.liveRunId}`,
    );
    await expectStatus(run, 200);
    const artifact = await admin.request.get(
      `/api/projects/${authority.liveProjectId}/private-work/artifacts/${authority.liveArtifactId}?thread_id=${authority.liveThreadId}`,
    );
    await expectStatus(artifact, 200);
    await assertSharedAssetsAreSafe(admin, authority.liveProjectId);
    const purged = await admin.request.get(
      `/api/projects/${authority.purgeProjectId}/private-work/threads/${authority.purgeThreadId}/files/${authority.purgeFileId}`,
    );
    await expectStatus(purged, 404);
    await expectProjectNotFound(outsider, authority.liveProjectId);
    const denials = await expectPrivateRunNotFound(outsider, {
      projectId: authority.liveProjectId,
      threadId: authority.liveThreadId,
      runId: authority.liveRunId,
      artifactId: authority.liveArtifactId,
    });
    expect(denials).toBe(4);
    return { phase: "restore", boundariesPassed: 12 };
  } finally {
    await Promise.all([admin.close(), outsider.close()]);
  }
}

export async function reloadAndResumeFromLastCursor(
  page: Page,
  live: PinnedLiveAgent,
): Promise<void> {
  await requestGatewayRestart();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByText(live.outputPath, { exact: true })).toBeVisible({
    timeout: 60_000,
  });
  const cursor = live.frameIds[0];
  if (cursor === undefined) {
    throw new Error("M8_LIVE_CURSOR_REQUIRED");
  }
  expect(cursor).toBeGreaterThan(0);
  const replay = await page
    .context()
    .request.get(
      `/api/projects/${live.project.id}/private-work/threads/${live.threadId}/runs/${live.publicHandle.runId}/stream`,
      { headers: { "Last-Event-ID": String(cursor) }, timeout: 60_000 },
    );
  await expectStatus(replay, 200);
  const frames = parseBoundedSse(await replay.text());
  expect(frames.length).toBeGreaterThan(0);
  expect(frames.every(({ id }) => id > cursor)).toBe(true);
  expect(new Set(frames.map(({ id }) => id)).size).toBe(frames.length);
  expect(frames.filter(({ event }) => event === "end")).toHaveLength(1);
  live.replayPassed = true;
}

export async function expectPrivateRunNotFound(
  context: BrowserContext,
  handle: LivePublicHandle,
): Promise<number> {
  liveHandleSchema.parse(handle);
  const paths = [
    `/api/projects/${handle.projectId}/private-work/threads/${handle.threadId}`,
    `/api/projects/${handle.projectId}/private-work/threads/${handle.threadId}/runs/${handle.runId}`,
    `/api/projects/${handle.projectId}/private-work/threads/${handle.threadId}/runs/${handle.runId}/events`,
    `/api/projects/${handle.projectId}/private-work/artifacts/${handle.artifactId}?thread_id=${handle.threadId}`,
  ];
  for (const path of paths) {
    const response = await context.request.get(path);
    await expectStatus(response, 404);
  }
  return paths.length;
}

const liveHandleSchema = z
  .object({
    projectId: uuidSchema,
    threadId: uuidSchema,
    runId: uuidSchema,
    artifactId: uuidSchema,
  })
  .strict();

export function liveBrowserResult(live: PinnedLiveAgent): M8LiveBrowserResult {
  const summary = live.summary;
  if (!summary || !live.replayPassed || live.privateDenials < 4) {
    throw new Error("M8_LIVE_BROWSER_RESULT_INCOMPLETE");
  }
  return {
    summary,
    replay_passed: true,
    private_denials: live.privateDenials,
  };
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
