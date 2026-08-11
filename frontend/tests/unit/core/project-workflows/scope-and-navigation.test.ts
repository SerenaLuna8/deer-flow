import { readFileSync } from "node:fs";

import { describe, expect, it, rs } from "@rstest/core";
import { QueryClient, skipToken } from "@tanstack/react-query";

import {
  createPrivateWorkScopeRegistry,
  transitionPrivateWorkScope,
} from "@/core/private-work/scope-registry";
import {
  projectWorkflowNodeCatalogQueryOptions,
  projectWorkflowReadinessQueryOptions,
  type WorkflowNodeCatalogTransport,
  type WorkflowReadinessTransport,
} from "@/core/project-workflows/hooks";
import { projectWorkflowNavigationVisible } from "@/core/project-workflows/navigation";
import {
  projectWorkflowQueryKey,
  projectWorkflowRoot,
} from "@/core/project-workflows/query-keys";
import { workflowProjectReadinessV1Schema } from "@/core/project-workflows/transport";

const ACCOUNT_A = "11111111-1111-4111-8111-111111111111";
const ACCOUNT_B = "22222222-2222-4222-8222-222222222222";
const PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const SCOPE_A = { accountId: ACCOUNT_A, projectId: PROJECT_A };
const SCOPE_B = { accountId: ACCOUNT_A, projectId: PROJECT_B };

const READY_WITHOUT_ADMISSION = workflowProjectReadinessV1Schema.parse({
  status: "ready",
  code: "WORKFLOW_CONTROL_PLANE_READY",
  workflow_enabled: true,
  schema_ready: true,
  admission_ready: false,
  request_id: "req-ready",
});

describe("project Workflow query scope", () => {
  it("keys every query by exact account UUID and project UUID, never slug", () => {
    expect(projectWorkflowRoot(SCOPE_A)).toEqual([
      "account",
      ACCOUNT_A,
      "project",
      PROJECT_A,
      "workflows",
    ]);
    expect(projectWorkflowQueryKey(SCOPE_A, "readiness")).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "readiness",
    ]);
    expect(projectWorkflowRoot(SCOPE_A)).not.toEqual(
      projectWorkflowRoot({ accountId: ACCOUNT_B, projectId: PROJECT_A }),
    );
    expect(projectWorkflowRoot(SCOPE_A)).not.toEqual(
      projectWorkflowRoot(SCOPE_B),
    );
    expect(projectWorkflowRoot(SCOPE_A)).not.toContain("project-slug");
  });

  it("does not install a request function without workflow.read", () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const readProjectWorkflowReadiness = rs.fn();
    const transport: WorkflowReadinessTransport = {
      readProjectWorkflowReadiness,
    };

    const options = projectWorkflowReadinessQueryOptions(
      access,
      false,
      transport,
    );

    expect(options.enabled).toBe(false);
    expect(options.queryFn).toBe(skipToken);
    expect(readProjectWorkflowReadiness).not.toHaveBeenCalled();
    registry.dispose(SCOPE_A);
  });

  it("does not install a Node Catalog request without workflow.read", () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const readProjectWorkflowNodeCatalog = rs.fn();
    const transport: WorkflowNodeCatalogTransport = {
      readProjectWorkflowNodeCatalog,
    };

    const options = projectWorkflowNodeCatalogQueryOptions(
      access,
      false,
      transport,
    );

    expect(options.queryKey).toEqual(
      projectWorkflowQueryKey(SCOPE_A, "node-catalog"),
    );
    expect(options.enabled).toBe(false);
    expect(options.queryFn).toBe(skipToken);
    expect(readProjectWorkflowNodeCatalog).not.toHaveBeenCalled();
    registry.dispose(SCOPE_A);
  });

  it("forwards the exact AbortSignal, disables automatic retry, and permits explicit retry", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    let attempts = 0;
    const seenSignals: AbortSignal[] = [];
    const transport: WorkflowReadinessTransport = {
      readProjectWorkflowReadiness: rs.fn(async (scope, { signal }) => {
        expect(scope).toEqual(SCOPE_A);
        seenSignals.push(signal);
        attempts += 1;
        if (attempts === 1) throw new Error("temporarily unavailable");
        return READY_WITHOUT_ADMISSION;
      }),
    };
    const options = projectWorkflowReadinessQueryOptions(
      access,
      true,
      transport,
    );
    const queryClient = new QueryClient();

    await expect(queryClient.fetchQuery(options)).rejects.toThrow(
      "temporarily unavailable",
    );
    expect(attempts).toBe(1);
    expect(await queryClient.fetchQuery(options)).toEqual(
      READY_WITHOUT_ADMISSION,
    );
    expect(seenSignals).toHaveLength(2);
    expect(seenSignals.every((signal) => signal instanceof AbortSignal)).toBe(
      true,
    );
    expect(options.retry).toBe(false);

    registry.dispose(SCOPE_A);
    queryClient.clear();
  });

  it("aborts, removes, and rejects a late old-scope readiness callback", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const queryClient = new QueryClient();
    let start!: () => void;
    const started = new Promise<void>((resolve) => {
      start = resolve;
    });
    let finish!: (value: typeof READY_WITHOUT_ADMISSION) => void;
    const deferred = new Promise<typeof READY_WITHOUT_ADMISSION>((resolve) => {
      finish = resolve;
    });
    let aborted = false;
    const transport: WorkflowReadinessTransport = {
      readProjectWorkflowReadiness: async (_scope, { signal }) => {
        signal.addEventListener("abort", () => {
          aborted = true;
        });
        start();
        return deferred;
      },
    };
    const key = projectWorkflowQueryKey(SCOPE_A, "readiness");
    const pending = queryClient
      .fetchQuery(projectWorkflowReadinessQueryOptions(access, true, transport))
      .catch(() => undefined);
    await started;

    await transitionPrivateWorkScope(registry, queryClient, SCOPE_A, SCOPE_B);
    expect(aborted).toBe(true);
    expect(registry.has(SCOPE_A)).toBe(false);
    expect(queryClient.getQueryData(key)).toBeUndefined();

    finish(READY_WITHOUT_ADMISSION);
    await pending;
    expect(queryClient.getQueryData(key)).toBeUndefined();
    queryClient.clear();
  });
});

describe("project Workflow navigation predicate", () => {
  it.each([
    [false, true, READY_WITHOUT_ADMISSION, true],
    [false, true, { ...READY_WITHOUT_ADMISSION, admission_ready: true }, true],
    [true, true, READY_WITHOUT_ADMISSION, false],
    [false, false, READY_WITHOUT_ADMISSION, false],
    [
      false,
      true,
      workflowProjectReadinessV1Schema.parse({
        status: "ready",
        code: "WORKFLOW_DISABLED",
        workflow_enabled: false,
        schema_ready: true,
        admission_ready: false,
        request_id: "req-disabled",
      }),
      false,
    ],
    [
      false,
      true,
      workflowProjectReadinessV1Schema.parse({
        status: "unavailable",
        code: "WORKFLOW_POLICY_UNAVAILABLE",
        workflow_enabled: false,
        schema_ready: true,
        admission_ready: false,
        request_id: "req-unavailable",
      }),
      false,
    ],
    [false, true, undefined, false],
  ] as const)(
    "uses only static/read/control-plane authority",
    (staticWebsiteOnly, canReadWorkflow, readiness, expected) => {
      expect(
        projectWorkflowNavigationVisible({
          staticWebsiteOnly,
          canReadWorkflow,
          readiness,
        }),
      ).toBe(expected);
    },
  );

  it("is a static-safe pure module with no feature flag or authenticated client import", () => {
    const source = readFileSync(
      "src/core/project-workflows/navigation.ts",
      "utf8",
    );
    const barrelSource = readFileSync(
      "src/core/project-workflows/index.ts",
      "utf8",
    );
    const transportSource = readFileSync(
      "src/core/project-workflows/transport.ts",
      "utf8",
    );
    expect(source).not.toMatch(/^import\s/m);
    expect(source).not.toContain("PROJECT_WORKFLOW");
    expect(source).not.toContain("process.env");
    expect(source).not.toContain("localStorage");
    expect(source).not.toContain("admission_ready");
    expect(source).not.toContain("Worker");
    expect(source).not.toContain("code_ready");
    expect(source).not.toContain("http_ready");
    expect(barrelSource).not.toMatch(/export \* from ["']\.\/hooks["']/);
    expect(barrelSource).not.toMatch(/export \* from ["']\.\/api["']/);
    expect(transportSource).toContain(
      'export { projectWorkflowEntryEnabled } from "./navigation";',
    );
    expect(transportSource).not.toMatch(
      /(?:const|function)\s+projectWorkflowEntryEnabled/,
    );
  });
});
