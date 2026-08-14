import { randomUUID } from "node:crypto";

import { expect, type BrowserContext } from "@playwright/test";

export type ReplayProjectScope = {
  id: string;
  slug: string;
  csrf: string;
  agent: {
    id: string;
    scope: "system" | "project";
  };
};

export async function registerReplayProject(
  context: BrowserContext,
  app: string,
): Promise<ReplayProjectScope> {
  const uniq = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const registration = await context.request.post(
    `${app}/api/v1/auth/register`,
    {
      data: {
        email: `e2e-${uniq}@example.com`,
        username: `e2e_${uniq.replaceAll("-", "_")}`,
        password: "very-strong-password-123",
      },
    },
  );
  expect(registration.status(), await registration.text()).toBe(201);

  const cookies = await context.cookies();
  const csrf = cookies.find((cookie) => cookie.name === "csrf_token")?.value;
  expect(csrf, "register must set csrf_token cookie").toBeTruthy();

  const created = await context.request.post(`${app}/api/projects`, {
    headers: { "X-CSRF-Token": csrf! },
    data: {
      slug: `replay-${uniq}`,
      display_name: "Replay project",
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const project = (await created.json()) as { id?: unknown; slug?: unknown };
  expect(typeof project.id).toBe("string");
  expect(typeof project.slug).toBe("string");
  if (typeof project.id !== "string" || typeof project.slug !== "string") {
    throw new Error("created replay project response is invalid");
  }

  const prepared = await context.request.post(
    `${app}/api/projects/${project.id}/test-only/prepare-agent`,
    { headers: { "X-CSRF-Token": csrf! } },
  );
  expect(prepared.status(), await prepared.text()).toBe(201);
  const agent = (await prepared.json()) as {
    id?: unknown;
    scope?: unknown;
    version?: unknown;
  };
  if (
    typeof agent.id !== "string" ||
    agent.scope !== "project" ||
    typeof agent.version !== "number"
  ) {
    throw new Error("prepared replay Agent response is invalid");
  }

  const activation = await context.request.post(
    `${app}/api/projects/${project.id}/agents/${agent.id}/activate`,
    {
      headers: { "X-CSRF-Token": csrf! },
      data: { expected_asset_version: agent.version },
    },
  );
  expect(activation.status(), await activation.text()).toBe(200);

  return {
    id: project.id,
    slug: project.slug,
    csrf: csrf!,
    agent: {
      id: agent.id,
      scope: agent.scope,
    },
  };
}

export async function createReplayThread(
  context: BrowserContext,
  app: string,
  project: ReplayProjectScope,
): Promise<string> {
  const threadId = randomUUID();
  const created = await context.request.post(
    `${app}/api/projects/${project.id}/private-work/threads`,
    {
      headers: { "X-CSRF-Token": project.csrf },
      data: {
        thread_id: threadId,
        agent_asset_id: project.agent.id,
        agent_scope: project.agent.scope,
        display_name: "New conversation",
        metadata: {},
      },
    },
  );
  expect(created.status(), await created.text()).toBe(201);
  return threadId;
}
