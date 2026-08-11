import { describe, expect, it, rs } from "@rstest/core";
import { QueryClient, skipToken } from "@tanstack/react-query";

import {
  createPrivateWorkScopeRegistry,
  transitionPrivateWorkScope,
} from "@/core/private-work/scope-registry";
import { ProjectWorkflowApiError } from "@/core/project-workflows/api";
import {
  createWorkflowDefinitionTransport,
  type WorkflowDefinitionTransport,
} from "@/core/project-workflows/definition-api";
import {
  workflowDraftResponseV1Schema,
  workflowDraftSaveRequestV1Schema,
} from "@/core/project-workflows/definition-contracts";
import {
  saveWorkflowDraftMutationOptions,
  workflowDefinitionMutationKey,
  workflowDefinitionQueryKey,
  workflowDefinitionsQueryKey,
  workflowDefinitionsQueryOptions,
  workflowDraftQueryKey,
  workflowDraftQueryOptions,
  workflowVersionQueryKey,
  workflowVersionsQueryKey,
} from "@/core/project-workflows/definition-queries";
import { projectWorkflowRoot } from "@/core/project-workflows/query-keys";

import definitionFixture from "../../../fixtures/workflows/workflow-definition-transport-v1.json";

const SCOPE_A = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const SCOPE_B = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
};
const WORKFLOW_ID = definitionFixture.definition.id;
const VERSION_ID = definitionFixture.definition.current_published_version_id;
const DRAFT = workflowDraftResponseV1Schema.parse(definitionFixture.draft);
const DRAFT_SAVE = workflowDraftSaveRequestV1Schema.parse({
  expected_revision: definitionFixture.draft.revision,
  spec: definitionFixture.draft.spec,
  canvas: definitionFixture.draft.canvas,
});

function transportWith(
  overrides: Partial<WorkflowDefinitionTransport>,
): WorkflowDefinitionTransport {
  return { ...createWorkflowDefinitionTransport(), ...overrides };
}

describe("G18 Workflow Definition query keys", () => {
  it("keys list/detail/Draft/Version by exact account, project and Workflow coordinates", () => {
    const listKey = workflowDefinitionsQueryKey(SCOPE_A, {
      publication: "published",
      limit: 25,
    });
    expect(listKey).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "definitions",
      "list",
      {
        query: null,
        lifecycle: "active",
        publication: "published",
        sort: "updated_desc",
        cursor: null,
        limit: 25,
      },
    ]);
    expect(workflowDefinitionQueryKey(SCOPE_A, WORKFLOW_ID)).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "definitions",
      WORKFLOW_ID,
      "detail",
    ]);
    expect(workflowDraftQueryKey(SCOPE_A, WORKFLOW_ID)).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "definitions",
      WORKFLOW_ID,
      "draft",
    ]);
    expect(workflowVersionsQueryKey(SCOPE_A, WORKFLOW_ID, {})).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "definitions",
      WORKFLOW_ID,
      "versions",
      "list",
      { cursor: null, limit: 50 },
    ]);
    expect(workflowVersionQueryKey(SCOPE_A, WORKFLOW_ID, VERSION_ID)).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "definitions",
      WORKFLOW_ID,
      "versions",
      VERSION_ID,
    ]);
    expect(
      workflowDefinitionMutationKey(SCOPE_A, WORKFLOW_ID, "save-draft"),
    ).toEqual([
      ...projectWorkflowRoot(SCOPE_A),
      "definitions",
      WORKFLOW_ID,
      "mutation",
      "save-draft",
    ]);
    expect(JSON.stringify(listKey)).not.toContain("project-slug");
    expect(workflowDraftQueryKey(SCOPE_A, WORKFLOW_ID)).not.toEqual(
      workflowDraftQueryKey(SCOPE_B, WORKFLOW_ID),
    );
  });
});

describe("G18 Workflow Definition query and mutation options", () => {
  it("uses skipToken when disabled and never calls the transport", () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const listDefinitions = rs.fn();
    const transport = transportWith({ listDefinitions });

    const options = workflowDefinitionsQueryOptions(
      access,
      {},
      false,
      transport,
    );
    expect(options.enabled).toBe(false);
    expect(options.queryFn).toBe(skipToken);
    expect(listDefinitions).not.toHaveBeenCalled();
    registry.dispose(SCOPE_A);
  });

  it("forwards TanStack AbortSignal and rejects a late old-scope Draft result", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const queryClient = new QueryClient();
    let started!: () => void;
    const didStart = new Promise<void>((resolve) => {
      started = resolve;
    });
    let finish!: (value: typeof DRAFT) => void;
    const deferred = new Promise<typeof DRAFT>((resolve) => {
      finish = resolve;
    });
    let seenSignal: AbortSignal | undefined;
    const transport = transportWith({
      readDraft: rs.fn(async (scope, _workflowId, { signal }) => {
        expect(scope).toEqual(SCOPE_A);
        seenSignal = signal;
        started();
        return deferred;
      }),
    });
    const key = workflowDraftQueryKey(SCOPE_A, WORKFLOW_ID);
    const pending = queryClient
      .fetchQuery(
        workflowDraftQueryOptions(access, WORKFLOW_ID, true, transport),
      )
      .catch((error: unknown) => error);
    await didStart;

    await transitionPrivateWorkScope(registry, queryClient, SCOPE_A, SCOPE_B);
    expect(seenSignal?.aborted).toBe(true);
    finish(DRAFT);
    const result = await pending;
    expect(result).toBeInstanceOf(Error);
    expect(queryClient.getQueryData(key)).toBeUndefined();
    queryClient.clear();
  });

  it("runs save through the scope abort owner, forwards stable idempotency/CAS and invalidates only after success", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const queryClient = new QueryClient();
    const invalidate = rs.spyOn(queryClient, "invalidateQueries");
    const body = DRAFT_SAVE;
    const saveDraft = rs.fn(
      async (scope, workflowId, request, options): Promise<typeof DRAFT> => {
        expect(scope).toEqual(SCOPE_A);
        expect(workflowId).toBe(WORKFLOW_ID);
        expect(request).toEqual(body);
        expect(options.idempotencyKey).toBe("save-operation-1");
        expect(options.signal).toBeInstanceOf(AbortSignal);
        return DRAFT;
      },
    );
    const transport = transportWith({ saveDraft });
    const options = saveWorkflowDraftMutationOptions(
      queryClient,
      access,
      WORKFLOW_ID,
      transport,
    );

    await expect(
      options.mutationFn({ body, idempotencyKey: "save-operation-1" }),
    ).resolves.toEqual(DRAFT);
    expect(invalidate).not.toHaveBeenCalled();
    await options.onSuccess();
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: [...projectWorkflowRoot(SCOPE_A), "definitions", WORKFLOW_ID],
    });
    registry.dispose(SCOPE_A);
    queryClient.clear();
  });

  it("keeps the local Draft cache untouched on 409", async () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(SCOPE_A);
    const queryClient = new QueryClient();
    const key = workflowDraftQueryKey(SCOPE_A, WORKFLOW_ID);
    const localDraft = { local: "unsaved-canvas" };
    queryClient.setQueryData(key, localDraft);
    const transport = transportWith({
      saveDraft: rs.fn(async () => {
        throw new ProjectWorkflowApiError(
          409,
          "WORKFLOW_DRAFT_CONFLICT",
          "Workflow draft conflict.",
        );
      }),
    });
    const options = saveWorkflowDraftMutationOptions(
      queryClient,
      access,
      WORKFLOW_ID,
      transport,
    );

    await expect(
      options.mutationFn({
        body: DRAFT_SAVE,
        idempotencyKey: "save-operation-conflict",
      }),
    ).rejects.toMatchObject({
      status: 409,
      code: "WORKFLOW_DRAFT_CONFLICT",
    });
    expect(queryClient.getQueryData(key)).toBe(localDraft);
    registry.dispose(SCOPE_A);
    queryClient.clear();
  });

  it("returns a fresh stateless transport rather than a module client singleton", () => {
    expect(createWorkflowDefinitionTransport()).not.toBe(
      createWorkflowDefinitionTransport(),
    );
  });
});
