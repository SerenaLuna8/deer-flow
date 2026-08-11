import type { ReactNode } from "react";

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import type { NodeCatalogEntry } from "@/core/project-workflows/catalog";
import type { WorkflowDraftPortLocale } from "@/core/project-workflows/editor/ports";
import type { WorkflowNodeKind } from "@/core/project-workflows/types";
import type { Capability } from "@/core/projects/types";

export type WorkflowDraftNode = NonNullable<
  WorkflowPersistedDocumentV1["spec"]["nodes"]
>[number];

/** Secret-free projection of the authenticated system Model catalog. */
export type WorkflowModelCatalogOption = Readonly<{
  name: string;
  display_name: string;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  workflow_authoring: Readonly<{
    modes: readonly ("chat" | "completion")[];
    supports_streaming: boolean;
    parameters: readonly Readonly<{
      name: "temperature" | "max_tokens";
      kind: "number" | "integer";
      minimum: number;
      maximum: number;
    }>[];
  }>;
}>;

export type WorkflowModelCatalogProjection = Readonly<{
  status: "loading" | "ready" | "unavailable";
  models: readonly WorkflowModelCatalogOption[];
}>;

/**
 * The single sealed input shared by all first-batch Inspector panels.
 * Panels may only mutate authored state through the per-Workbench command port.
 */
export type WorkflowNodeConfigPanelProps = {
  nodeId: string;
  node: WorkflowDraftNode;
  document: WorkflowPersistedDocumentV1;
  catalogEntry: NodeCatalogEntry;
  locale: WorkflowDraftPortLocale;
  modelCatalog?: WorkflowModelCatalogProjection;
  capabilities: readonly Capability[];
  readOnly: boolean;
  disabled: boolean;
};

export type WorkflowNodeConfigPanel = (
  props: WorkflowNodeConfigPanelProps,
) => ReactNode;

export type WorkflowNodeConfigPanelRegistry = Readonly<
  Record<WorkflowNodeKind, WorkflowNodeConfigPanel>
>;
