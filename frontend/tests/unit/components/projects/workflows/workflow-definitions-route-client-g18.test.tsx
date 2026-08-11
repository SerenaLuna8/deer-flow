import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  resolveWorkflowDefinitionCreateAttempt,
  WorkflowDefinitionsRouteClient,
} from "@/components/projects/workflows/definitions/workflow-definitions-route-client";
import type { Project } from "@/core/projects/types";

const PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const WORKFLOW_ID = "11111111-1111-4111-8111-111111111111";

let capabilities: Project["capabilities"] = ["workflow.read"];
let readinessEnabled: boolean | undefined;
let listEnabled: boolean | undefined;
let readinessResult: Record<string, unknown>;
let definitionsResult: Record<string, unknown>;

const router = {
  push: rs.fn(),
  refresh: rs.fn(),
};

function idleMutation() {
  return {
    isPending: false,
    reset: rs.fn(),
    mutateAsync: rs.fn(),
  };
}

rs.mock("next/navigation", () => ({ useRouter: () => router }));
rs.mock("@/components/projects/project-context", () => ({
  useCurrentProject: () => ({
    id: PROJECT_ID,
    slug: "alpha",
    capabilities,
  }),
}));
rs.mock("@/core/project-workflows/api", () => ({
  ProjectWorkflowApiError: class ProjectWorkflowApiError extends Error {},
  readProjectWorkflowReadiness: rs.fn(),
}));
rs.mock("@/core/project-workflows/definition-api", () => ({
  createWorkflowDefinitionIdempotencyKey: () => "idem-key",
}));
rs.mock("@/core/project-workflows/hooks", () => ({
  useProjectWorkflowReadiness: (enabled: boolean) => {
    readinessEnabled = enabled;
    return readinessResult;
  },
}));
rs.mock("@/core/project-workflows/definition-queries", () => ({
  useWorkflowDefinitions: (_filters: unknown, enabled: boolean) => {
    listEnabled = enabled;
    return definitionsResult;
  },
  useCreateWorkflowDefinition: idleMutation,
  useArchiveWorkflowDefinition: idleMutation,
}));

const READY_WITHOUT_ADMISSION = {
  status: "ready",
  code: "WORKFLOW_CONTROL_PLANE_READY",
  workflow_enabled: true,
  schema_ready: true,
  admission_ready: false,
  request_id: "req-ready",
};

function readyDefinitions() {
  return {
    isPending: false,
    isError: false,
    isFetchingNextPage: false,
    hasNextPage: false,
    data: {
      pages: [
        {
          items: [
            {
              id: WORKFLOW_ID,
              name: "只读流程",
              description: "safe summary",
              lifecycle: "active",
              publication: "draft_only",
              revision: 1,
              current_published_version_id: null,
              current_published_version_number: null,
              draft_revision: 1,
              draft_checksum: "a".repeat(64),
              created_at: "2026-08-10T08:00:00Z",
              updated_at: "2026-08-10T09:00:00Z",
            },
          ],
          next_cursor: null,
        },
      ],
    },
    refetch: rs.fn(),
    fetchNextPage: rs.fn(),
  };
}

function render(): string {
  return renderToStaticMarkup(<WorkflowDefinitionsRouteClient />);
}

describe("G18 Workflow Definition route client", () => {
  test("reuses a create key only for the exact same semantic payload", () => {
    let sequence = 0;
    const generate = () => `key-${++sequence}`;
    const first = resolveWorkflowDefinitionCreateAttempt(
      null,
      { name: "流程", description: "第一版" },
      generate,
    );
    const retry = resolveWorkflowDefinitionCreateAttempt(
      first,
      { name: "流程", description: "第一版" },
      generate,
    );
    const changed = resolveWorkflowDefinitionCreateAttempt(
      retry,
      { name: "流程", description: "第二版" },
      generate,
    );

    expect(retry).toBe(first);
    expect(changed.idempotencyKey).toBe("key-2");
    expect(changed.body.description).toBe("第二版");
  });

  test("keeps navigation/list available when admission is offline and stays read-only without workflow.edit", () => {
    capabilities = ["workflow.read"];
    readinessResult = {
      data: READY_WITHOUT_ADMISSION,
      isPending: false,
      isError: false,
      refetch: rs.fn(),
    };
    definitionsResult = readyDefinitions();

    const html = render();

    expect(readinessEnabled).toBe(true);
    expect(listEnabled).toBe(true);
    expect(html).toContain("只读流程");
    expect(html).toContain(">查看<");
    expect(html).not.toContain("创建空白工作流");
    expect(html).not.toContain(">归档<");
  });

  test("shows editor controls only from workflow.edit", () => {
    capabilities = ["workflow.read", "workflow.edit"];
    readinessResult = {
      data: READY_WITHOUT_ADMISSION,
      isPending: false,
      isError: false,
      refetch: rs.fn(),
    };
    definitionsResult = readyDefinitions();

    const html = render();

    expect(html).toContain("创建空白工作流");
    expect(html).toContain(">编辑<");
    expect(html).toContain(">归档<");
  });

  test("does not mount the list for confirmed disabled or retryable unavailable readiness", () => {
    capabilities = ["workflow.read", "workflow.edit"];
    definitionsResult = readyDefinitions();
    readinessResult = {
      data: {
        status: "ready",
        code: "WORKFLOW_DISABLED",
        workflow_enabled: false,
        schema_ready: true,
        admission_ready: false,
        request_id: "req-disabled",
      },
      isPending: false,
      isError: false,
      refetch: rs.fn(),
    };
    const disabled = render();
    expect(listEnabled).toBe(false);
    expect(disabled).toContain('data-testid="workflow-disabled"');

    readinessResult = {
      data: {
        status: "unavailable",
        code: "WORKFLOW_POLICY_UNAVAILABLE",
        workflow_enabled: false,
        schema_ready: true,
        admission_ready: false,
        request_id: "req-unavailable",
      },
      isPending: false,
      isError: false,
      refetch: rs.fn(),
    };
    const unavailable = render();
    expect(listEnabled).toBe(false);
    expect(unavailable).toContain('data-testid="workflow-unavailable"');
    expect(unavailable).not.toContain('data-testid="workflow-empty"');
  });

  test("does not turn a Definition list failure into an empty result", () => {
    capabilities = ["workflow.read"];
    readinessResult = {
      data: READY_WITHOUT_ADMISSION,
      isPending: false,
      isError: false,
      refetch: rs.fn(),
    };
    definitionsResult = {
      isPending: false,
      isError: true,
      data: undefined,
      isFetchingNextPage: false,
      hasNextPage: false,
      refetch: rs.fn(),
      fetchNextPage: rs.fn(),
    };

    const html = render();

    expect(listEnabled).toBe(true);
    expect(html).toContain('data-testid="workflow-unavailable"');
    expect(html).not.toContain('data-testid="workflow-empty"');
  });

  test("mounts neither readiness nor list request without workflow.read", () => {
    capabilities = [];
    readinessResult = {
      data: undefined,
      isPending: true,
      isError: false,
      refetch: rs.fn(),
    };
    definitionsResult = readyDefinitions();

    const html = render();

    expect(readinessEnabled).toBe(false);
    expect(listEnabled).toBe(false);
    expect(html).toContain('data-error-status="403"');
    expect(html).toContain("没有访问权限");
    expect(html).not.toContain('data-testid="workflow-unavailable"');
  });
});
