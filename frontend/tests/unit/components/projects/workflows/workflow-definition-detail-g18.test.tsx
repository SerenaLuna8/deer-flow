import { describe, expect, it } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  applyWorkflowValidationIfCurrent,
  createWorkflowDefinitionConflict,
  createWorkflowDefinitionOperationKeys,
  settleWorkflowPublishIfCurrent,
  workflowCredentialSlotSchemaChecksum,
  workflowDefinitionPermissions,
  workflowDefinitionRequestIdentity,
  workflowDraftDocument,
  workflowDraftSaveRequest,
  WorkflowDefinitionConflictBanner,
  WorkflowDefinitionGrantPanel,
} from "@/components/projects/workflows/definitions/detail/workflow-definition-detail";
import { WorkflowValidationAndVersionPanel } from "@/components/projects/workflows/definitions/detail/workflow-definition-route-client";
import { WorkflowWorkbenchStoreProvider } from "@/components/projects/workflows/workbench/workbench-store-context";
import {
  workflowNodeRegistryV1,
  type NodeCatalogResponseV1,
} from "@/core/project-workflows/catalog";
import {
  workflowVersionResponseV1Schema,
  type WorkflowDefinitionResponseV1,
  type WorkflowDraftResponseV1,
} from "@/core/project-workflows/definition-contracts";
import { createWorkflowEditorStore } from "@/core/project-workflows/editor/store";

import canvasFixture from "../../../../fixtures/workflows/canvas-document-v1.json";
import specFixture from "../../../../fixtures/workflows/workflow-spec-v1.json";

const WORKFLOW_ID = "11111111-1111-4111-8111-111111111111";
const VERSION_ID = "22222222-2222-4222-8222-222222222222";
const NODE_ID = "33333333-3333-4333-8333-333333333333";
const CHECKSUM = "a".repeat(64);

const catalog: NodeCatalogResponseV1 = {
  schema_version: 1,
  catalog_generation: "d".repeat(64),
  availability_generation: "e".repeat(64),
  entries: workflowNodeRegistryV1.map((definition) => ({
    definition,
    availability: { state: "enabled" },
  })),
};

const definition: WorkflowDefinitionResponseV1 = {
  id: WORKFLOW_ID,
  name: "客户回访",
  description: "",
  lifecycle: "active",
  publication: "draft_only",
  revision: 3,
  current_published_version_id: null,
  current_published_version_number: null,
  draft_revision: 4,
  draft_checksum: CHECKSUM,
  created_at: "2026-08-10T00:00:00Z",
  updated_at: "2026-08-10T00:00:01Z",
};

const draft: WorkflowDraftResponseV1 = {
  workflow_id: WORKFLOW_ID,
  revision: 4,
  spec: {
    schema_version: 1,
    nodes: [
      {
        id: NODE_ID,
        type: "http_request",
        type_version: 1,
        scope: { kind: "root" },
        config: {},
      },
    ],
    credential_slots: [
      {
        id: "http_auth",
        name: "HTTP Authorization",
        purpose: "http_auth",
        payload_schema: {
          type: "object",
          properties: { token: { type: "string" } },
        },
        required: true,
      },
    ],
  },
  canvas: { schema_version: 1 },
  draft_checksum: CHECKSUM,
  updated_at: "2026-08-10T00:00:01Z",
};

const version = workflowVersionResponseV1Schema.parse({
  id: VERSION_ID,
  workflow_id: WORKFLOW_ID,
  version_number: 1,
  graph_schema_version: 1,
  canvas_schema_version: 1,
  compiler_contract_version: 1,
  semantic_checksum: "b".repeat(64),
  spec: specFixture,
  canvas: canvasFixture,
  credential_slots: [
    {
      slot_id: "slot-api",
      name: "HTTP Authorization",
      purpose: "http_auth",
      payload_schema: {
        type: "object",
        properties: { token: { type: "string" } },
      },
      payload_schema_checksum: "c".repeat(64),
      required: true,
    },
  ],
  missing_required_credential_slot_ids: ["slot-api"],
  executable: false,
  published_at: "2026-08-10T00:00:02Z",
});

describe("G18 Workflow Definition detail state", () => {
  it("derives read/edit/publish/grant permissions only from closed capabilities", () => {
    expect(workflowDefinitionPermissions(["workflow.read"])).toEqual({
      canRead: true,
      canEdit: false,
      canPublish: false,
      canDraftGrant: false,
      canVersionGrant: false,
    });
    expect(
      workflowDefinitionPermissions([
        "workflow.read",
        "workflow.edit",
        "workflow.publish",
        "workflow.credential.grant",
      ]),
    ).toEqual({
      canRead: true,
      canEdit: true,
      canPublish: true,
      canDraftGrant: true,
      canVersionGrant: true,
    });
    expect(
      workflowDefinitionPermissions([
        "workflow.read",
        "workflow.credential.grant",
      ]),
    ).toEqual({
      canRead: true,
      canEdit: false,
      canPublish: false,
      canDraftGrant: false,
      canVersionGrant: false,
    });
    expect(
      workflowDefinitionPermissions([
        "workflow.read",
        "workflow.edit",
        "workflow.credential.grant",
      ]),
    ).toMatchObject({ canDraftGrant: true, canVersionGrant: false });
    expect(
      workflowDefinitionPermissions([
        "workflow.read",
        "workflow.publish",
        "workflow.credential.grant",
      ]),
    ).toMatchObject({ canDraftGrant: false, canVersionGrant: true });
  });

  it("builds save authority only from the loaded revision and current document", () => {
    const current = workflowDraftDocument(draft);
    current.spec.nodes![0]!.custom_label = "本地未保存";

    const request = workflowDraftSaveRequest(draft, current);
    expect(request).toEqual({
      expected_revision: 4,
      spec: current.spec,
      canvas: current.canvas,
    });
    expect(request.spec).not.toBe(current.spec);
  });

  it("preserves a deep local snapshot on conflict and only exposes remote for explicit resolution", () => {
    const current = workflowDraftDocument(draft);
    current.spec.nodes![0]!.custom_label = "本地版本";
    const conflict = createWorkflowDefinitionConflict(draft, current);
    current.spec.nodes![0]!.custom_label = "后续篡改";

    expect(conflict.local.spec.nodes![0]!.custom_label).toBe("本地版本");
    expect(conflict.remote).toBeNull();
    expect(() => {
      conflict.local.spec.nodes![0]!.custom_label = "不能覆盖";
    }).toThrow();
  });

  it("reuses one operation key until success and isolates operation kinds", () => {
    let sequence = 0;
    const keys = createWorkflowDefinitionOperationKeys(
      () => `key-${++sequence}`,
    );
    expect(keys.current("save", "request-a")).toBe("key-1");
    expect(keys.current("save", "request-a")).toBe("key-1");
    expect(keys.current("publish", "request-a")).toBe("key-2");
    expect(keys.current("save", "request-b")).toBe("key-3");
    keys.complete("save", "request-a");
    expect(keys.current("save", "request-b")).toBe("key-3");
    keys.complete("save", "request-b");
    expect(keys.current("save", "request-b")).toBe("key-4");
  });

  it("derives idempotency identities from exact canonical request semantics", () => {
    expect(
      workflowDefinitionRequestIdentity({ revision: 3, checksum: CHECKSUM }),
    ).toBe(
      workflowDefinitionRequestIdentity({ checksum: CHECKSUM, revision: 3 }),
    );
    expect(
      workflowDefinitionRequestIdentity({ revision: 4, checksum: CHECKSUM }),
    ).not.toBe(
      workflowDefinitionRequestIdentity({ revision: 3, checksum: CHECKSUM }),
    );
  });

  it("computes a canonical slot schema checksum independent of object key order", () => {
    const left = workflowCredentialSlotSchemaChecksum({
      type: "object",
      properties: { token: { type: "string" } },
    });
    const right = workflowCredentialSlotSchemaChecksum({
      properties: { token: { type: "string" } },
      type: "object",
    });
    expect(left).toBe(right);
    expect(left).toMatch(/^[0-9a-f]{64}$/);
  });

  it("does not mark edits made after a publish request as saved", () => {
    const store = createWorkflowEditorStore({
      document: workflowDraftDocument(draft),
    });
    const submitted = store.getState().current;
    expect(
      store.dispatch({
        type: "update_node_presentation",
        node_id: NODE_ID,
        custom_label: "发布请求后的本地更改",
        description: null,
      }).applied,
    ).toBe(true);

    expect(settleWorkflowPublishIfCurrent(store, submitted)).toBe(false);
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().current.spec.nodes?.[0]?.custom_label).toBe(
      "发布请求后的本地更改",
    );
    expect(store.getState().history.past.length).toBeGreaterThan(0);
  });

  it("ignores stale validation issues after the submitted Draft changes", () => {
    const store = createWorkflowEditorStore({
      document: workflowDraftDocument(draft),
    });
    const submitted = store.getState().current;
    store.dispatch({
      type: "update_node_presentation",
      node_id: NODE_ID,
      custom_label: "校验期间变化",
      description: null,
    });

    expect(
      applyWorkflowValidationIfCurrent(store, submitted, [
        {
          severity: "error",
          code: "WORKFLOW_STALE_SERVER_ISSUE",
          message: "stale",
          path: ["nodes", "0"],
          node_id: NODE_ID,
        },
      ]),
    ).toBe(false);
    expect(
      store
        .getState()
        .validationIssues.some(
          (issue) => issue.code === "WORKFLOW_STALE_SERVER_ISSUE",
        ),
    ).toBe(false);
  });
});

describe("G18 Definition conflict and Credential projections", () => {
  it("renders explicit compare/reload actions without replacing local state", () => {
    const html = renderToStaticMarkup(
      <WorkflowDefinitionConflictBanner
        conflict={createWorkflowDefinitionConflict(
          draft,
          workflowDraftDocument(draft),
        )}
        comparing={false}
        onCompare={() => undefined}
        onReload={() => undefined}
      />,
    );
    expect(html).toContain("服务器上的草稿已更新");
    expect(html).toContain("比较差异");
    expect(html).toContain("重新加载服务器草稿");
  });

  it("never exposes Credential binding controls without grant capability", () => {
    const readOnly = renderToStaticMarkup(
      <WorkflowDefinitionGrantPanel
        canDraftGrant={false}
        canVersionGrant={false}
        draft={draft}
        definition={definition}
        versions={[version]}
      />,
    );
    expect(readOnly).toContain("缺少 1 个凭据绑定");
    expect(readOnly).not.toContain("credential_id");
    expect(readOnly).not.toContain("绑定凭据");

    const grant = renderToStaticMarkup(
      <WorkflowDefinitionGrantPanel
        canDraftGrant
        canVersionGrant
        draft={draft}
        definition={definition}
        versions={[version]}
      />,
    );
    expect(grant).toContain("绑定凭据");
    expect(grant).not.toContain("secret");
    expect(grant).not.toContain("envelope");

    const versionOnly = renderToStaticMarkup(
      <WorkflowDefinitionGrantPanel
        canDraftGrant={false}
        canVersionGrant
        draft={draft}
        definition={definition}
        versions={[version]}
      />,
    );
    expect(versionOnly).toContain("绑定凭据");
    expect(versionOnly).not.toContain("Draft 授权意图");
  });

  it("renders Version query failures explicitly instead of treating them as empty", () => {
    const store = createWorkflowEditorStore({
      document: workflowDraftDocument(draft),
    });
    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowValidationAndVersionPanel
          catalog={catalog}
          canDraftGrant
          canVersionGrant
          currentVersionStatus="error"
          definition={definition}
          draft={draft}
          grantBusy={false}
          grantVersions={[]}
          hasMoreVersions={false}
          loadingMoreVersions={false}
          publishResult={null}
          selectedVersionId={VERSION_ID}
          validation={null}
          versionHistoryStatus="error"
          versions={[]}
          onDeleteGrant={() => undefined}
          onFocusIssue={() => undefined}
          onLoadMore={() => undefined}
          onPutGrant={() => undefined}
          onRetryCurrentVersion={() => undefined}
          onRetryVersionHistory={() => undefined}
          onSelectVersion={() => undefined}
        />
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain('data-testid="workflow-version-history-error"');
    expect(html).toContain('data-testid="workflow-current-version-error"');
    expect(html).not.toContain("尚未发布版本");
  });

  it("selects immutable history explicitly for read-only viewing", () => {
    const store = createWorkflowEditorStore({
      document: workflowDraftDocument(draft),
    });
    const html = renderToStaticMarkup(
      <WorkflowWorkbenchStoreProvider store={store}>
        <WorkflowValidationAndVersionPanel
          catalog={catalog}
          canDraftGrant={false}
          canVersionGrant={false}
          currentVersionStatus="ready"
          definition={definition}
          draft={draft}
          grantBusy={false}
          grantVersions={[version]}
          hasMoreVersions={false}
          loadingMoreVersions={false}
          publishResult={null}
          selectedVersionId={VERSION_ID}
          validation={null}
          versionHistoryStatus="ready"
          versions={[version]}
          onDeleteGrant={() => undefined}
          onFocusIssue={() => undefined}
          onLoadMore={() => undefined}
          onPutGrant={() => undefined}
          onRetryCurrentVersion={() => undefined}
          onRetryVersionHistory={() => undefined}
          onSelectVersion={() => undefined}
        />
      </WorkflowWorkbenchStoreProvider>,
    );

    expect(html).toContain("正在查看（只读）");
    expect(html).toContain("版本 1");
    expect(html).toContain('data-testid="workflow-version-readonly-preview"');
  });
});
