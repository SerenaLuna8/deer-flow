import type { BrowserContext } from "@playwright/test";
import { describe, expect, it } from "@rstest/core";

import {
  bindExecutableSystemAgent,
  syntheticAccount,
} from "../../e2e-release/support/m8-api";

describe("M8 synthetic account", () => {
  it("uses the backend-accepted reserved example domain", () => {
    const account = syntheticAccount("contract");

    expect(account.email).toMatch(/^m8-contract-[0-9a-f]{32}@example\.com$/u);
    expect(account.password).not.toContain(account.email);
  });
});

describe("M8 system Agent binding", () => {
  it("binds published system dependencies before the Agent", async () => {
    const ids = {
      skill: crypto.randomUUID(),
      skillVersion: crypto.randomUUID(),
      mcp: crypto.randomUUID(),
      mcpVersion: crypto.randomUUID(),
      agent: crypto.randomUUID(),
      agentVersion: crypto.randomUUID(),
      project: crypto.randomUUID(),
    };
    const calls: string[] = [];
    const page = (id: string, versionId: string) => ({
      system_items: [
        {
          id,
          scope: "system",
          current_published_version_id: versionId,
          status: "active",
          capabilities: ["shared_assets.read"],
          binding: null,
        },
      ],
      project_items: [],
    });
    const pages: Record<string, unknown> = {
      [`/api/projects/${ids.project}/skills`]: page(
        ids.skill,
        ids.skillVersion,
      ),
      [`/api/projects/${ids.project}/mcp-servers`]: page(
        ids.mcp,
        ids.mcpVersion,
      ),
      [`/api/projects/${ids.project}/agents`]: page(
        ids.agent,
        ids.agentVersion,
      ),
    };
    const context = {
      cookies: async () => [{ name: "csrf_token", value: "synthetic-csrf" }],
      request: {
        get: async (url: string) => {
          calls.push(`GET ${url}`);
          return {
            status: () => 200,
            json: async () => pages[url],
          };
        },
        post: async (url: string) => {
          calls.push(`POST ${url}`);
          return { status: () => 201 };
        },
      },
    } as unknown as BrowserContext;

    await expect(bindExecutableSystemAgent(context, ids.project)).resolves.toBe(
      ids.agent,
    );
    expect(calls).toEqual([
      `GET /api/projects/${ids.project}/skills`,
      `POST /api/projects/${ids.project}/system-skill-bindings`,
      `GET /api/projects/${ids.project}/mcp-servers`,
      `POST /api/projects/${ids.project}/system-mcp-bindings`,
      `GET /api/projects/${ids.project}/agents`,
      `POST /api/projects/${ids.project}/system-agent-bindings`,
    ]);
  });
});
