import { describe, expect, rs, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { AgentBuilderWorkspace } from "@/components/projects/agents/agent-builder-workspace";
import {
  agentBuilderActivitiesKey,
  agentBuilderSessionKey,
  type AgentBuilderSession,
} from "@/core/agent-builder";
import { I18nProvider } from "@/core/i18n/context";
import { modelsQueryKey } from "@/core/models/hooks";
import { PrivateWorkProvider } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const THREAD_ID = "44444444-4444-4444-8444-444444444444";
const AGENT_ID = "55555555-5555-4555-8555-555555555555";
const MODEL_ID = "00000000-0000-4000-8000-000000000204";
const NOW = "2026-08-22T08:00:00Z";

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Completed Agent Builder regression fixture",
  icon: "folder",
  role: "editor",
  capabilities: [
    "project.read",
    "project.enter",
    "shared_assets.read",
    "shared_assets.edit",
  ],
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

rs.mock("next/navigation", () => ({
  useRouter: () => ({
    push: rs.fn(),
    replace: rs.fn(),
    refresh: rs.fn(),
  }),
}));

rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: {
      id: ACCOUNT_ID,
      email: "owner@example.test",
      username: "owner",
      system_role: "user",
      needs_setup: false,
      oauth_provider: null,
    },
  }),
}));

rs.mock("@/components/projects/project-context", () => ({
  useCurrentProject: () => project,
}));

const completedSession: AgentBuilderSession = {
  id: SESSION_ID,
  project_id: PROJECT_ID,
  owner_user_id: ACCOUNT_ID,
  thread_id: THREAD_ID,
  slug: "catalog-auditor",
  display_name: "Catalog auditor",
  status: "completed",
  revision: 8,
  blueprint: {
    description: "Audit a product catalog",
    model_ref: MODEL_ID,
    tool_groups: ["read"],
    skill_refs: [],
    mcp_version_ids: [],
    agents_instructions: "# AGENTS.md\n\nAudit the catalog.",
    soul: "# SOUL.md\n\nBe precise.",
    identity: "# IDENTITY.md\n\nCatalog auditor.",
    user_context: "# USER.md\n\nPrefer concise reports.",
  },
  blueprint_checksum: "a".repeat(64),
  assumptions: [],
  conflicts: [],
  messages: [],
  active_clarification: null,
  active_clarifications: [],
  progress: [],
  error_code: null,
  error_message: null,
  created_agent_id: AGENT_ID,
  generation_preference: {
    model_ref: MODEL_ID,
    mode: "pro",
  },
  created_at: NOW,
  updated_at: NOW,
};

function renderCompletedWorkspace(
  session: AgentBuilderSession = completedSession,
): string {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  queryClient.setQueryData(
    agentBuilderSessionKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID),
    session,
  );
  queryClient.setQueryData(
    agentBuilderActivitiesKey(ACCOUNT_ID, PROJECT_ID, SESSION_ID),
    [],
  );
  queryClient.setQueryData(modelsQueryKey, {
    models: [
      {
        name: MODEL_ID,
        model: MODEL_ID,
        display_name: "GPT-5.6 Luna",
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
        supports_vision_bridge: false,
        is_default: true,
      },
    ],
    token_usage: { enabled: false },
  });

  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <PrivateWorkProvider
        access={{
          scope: { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
          client: {} as never,
          apiBaseURL: `/api/projects/${PROJECT_ID}/private-work`,
          queryKeyPrefix: ["completed-agent-builder"],
          reconnectOnMount: false,
          isActive: () => true,
        }}
      >
        <I18nProvider initialLocale="en-US">
          <AgentBuilderWorkspace sessionId={SESSION_ID} />
        </I18nProvider>
      </PrivateWorkProvider>
    </QueryClientProvider>,
  );
}

function workspaceHeader(html: string): string {
  const header = /<header\b[\s\S]*?<\/header>/u.exec(html)?.[0];
  if (!header)
    throw new Error("Agent Builder workspace header was not rendered");
  return header;
}

describe("completed Agent Builder workspace", () => {
  test("uses a compact completed status instead of explanatory header copy", () => {
    const html = renderCompletedWorkspace();
    const header = workspaceHeader(html);

    expect({
      showsAutosave: header.includes("Automatically saved; continue later"),
      showsPermissionFailure: html.includes(
        "Your account cannot continue designing this Agent. Saved session content remains available to view.",
      ),
      showsInitialDefinition: header.includes("Initial Agent Definition"),
      showsReadOnly: header.includes("Read-only"),
      showsDesignRecordExplanation: header.includes("design record"),
      showsMoreActions: header.includes('aria-label="More actions"'),
    }).toEqual({
      showsAutosave: false,
      showsPermissionFailure: false,
      showsInitialDefinition: true,
      showsReadOnly: true,
      showsDesignRecordExplanation: false,
      showsMoreActions: false,
    });
  });

  test("does not show an idle autosave explanation", () => {
    const header = workspaceHeader(
      renderCompletedWorkspace({
        ...completedSession,
        status: "proposal_ready",
        created_agent_id: null,
      }),
    );

    expect(header).not.toContain("Automatically saved; continue later");
  });

  test("keeps the conversation compact while the blueprint opens in its side panel", () => {
    const html = renderCompletedWorkspace();
    const header = workspaceHeader(html);
    const conversationStart = html.indexOf(
      "data-agent-builder-conversation-surface",
    );
    const blueprintStart = html.indexOf("data-agent-builder-blueprint-surface");

    expect(header).toContain('aria-label="Open Agent blueprint"');
    expect(conversationStart).toBeGreaterThanOrEqual(0);
    expect(blueprintStart).toBeGreaterThan(conversationStart);

    const conversation = html.slice(conversationStart, blueprintStart);
    const sidePanel = html.slice(blueprintStart);

    expect(conversation).toContain(
      'data-testid="agent-builder-blueprint-summary"',
    );
    expect(conversation).not.toContain("Runtime configuration");
    expect(conversation).not.toContain('id="agent-builder-commit-name"');
    expect(sidePanel).toContain('data-testid="agent-builder-blueprint-panel"');
    expect(sidePanel).toContain("Runtime configuration");
    expect(sidePanel).toContain('aria-label="Close Agent blueprint"');
    expect(html).toContain(
      "lg:grid-cols-[minmax(20rem,0.9fr)_minmax(28rem,1.1fr)]",
    );
  });
});
