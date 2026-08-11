import { describe, expect, it } from "@rstest/core";

import {
  nodeCatalogResponseV1Schema,
  nodeTypeDefinitionSchema,
  resolvedWorkflowInstancePortsV1Schema,
  workflowNodeRegistryV1,
} from "@/core/project-workflows/catalog";
import {
  workflowCompilerSnapshotContractV1Schema,
  workflowRunSnapshotIdentityV1Schema,
} from "@/core/project-workflows/compatibility";
import {
  workflowOwnerPrivateRunV1Schema,
  workflowRunAdmissionResponseV1Schema,
} from "@/core/project-workflows/run-contracts";
import {
  workflowRuntimeEffectivePolicyV1Schema,
  workflowRuntimePolicyV1Schema,
  workflowRuntimeStoredPolicyV1Schema,
} from "@/core/project-workflows/runtime-policy";
import { workflowEventEnvelopeV1Schema } from "@/core/project-workflows/transport";
import {
  canvasDocumentV1Schema,
  restrictedJsonTemplateSchema,
  restrictedTemplateSchema,
  workflowNodeSpecBaseSchema,
  workflowSpecV1Schema,
} from "@/core/project-workflows/types";

import canvasFixture from "../../../fixtures/workflows/canvas-document-v1.json";
import runFixture from "../../../fixtures/workflows/workflow-run-contracts-v1.json";
import invalidFixture from "../../../fixtures/workflows/workflow-run-invalid-v1.json";
import runtimePolicyFixture from "../../../fixtures/workflows/workflow-runtime-policy-v1.json";
import workflowSpecFixture from "../../../fixtures/workflows/workflow-spec-v1.json";
import {
  workflowPrivateJobV1Schema,
  workflowRunContractFixtureV1Schema,
  workflowRunJobAuthorityV1Schema,
} from "../../../support/workflow-private-run-contracts";

type PathPart = string | number;

const replaceAtPath = (
  source: unknown,
  path: readonly PathPart[],
  replacement: unknown,
): unknown => {
  const cloned: unknown = structuredClone(source);
  let cursor = cloned;

  for (const part of path.slice(0, -1)) {
    if (typeof part === "number" && Array.isArray(cursor)) {
      cursor = cursor[part];
    } else if (
      typeof part === "string" &&
      cursor !== null &&
      typeof cursor === "object"
    ) {
      cursor = (cursor as Record<string, unknown>)[part];
    } else {
      throw new Error(`invalid strict-literal fixture path: ${path.join(".")}`);
    }
  }

  const finalPart = path.at(-1);
  if (typeof finalPart === "number" && Array.isArray(cursor)) {
    cursor[finalPart] = replacement;
  } else if (
    typeof finalPart === "string" &&
    cursor !== null &&
    typeof cursor === "object"
  ) {
    (cursor as Record<string, unknown>)[finalPart] = replacement;
  } else {
    throw new Error(`invalid strict-literal fixture target: ${path.join(".")}`);
  }
  return cloned;
};

const eventFixture = {
  schema_version: 1,
  run_id: "00000000-0000-4000-8000-000000000010",
  workflow_version_id: "00000000-0000-4000-8000-000000000012",
  seq: "1",
  type: "workflow.run.started",
  iteration_path: [],
  occurred_at: "2026-08-10T00:00:00Z",
  payload: {},
};

const catalogFixture = {
  schema_version: 1,
  catalog_generation: "a".repeat(64),
  availability_generation: "b".repeat(64),
  entries: workflowNodeRegistryV1.map((definition) => ({
    definition,
    availability: { state: "enabled", reason_code: null },
    public_limits: null,
  })),
};

const resolvedPortsFixture = {
  schema_version: 1,
  nodes: [],
};

const storedPolicyFixture = {
  ...runtimePolicyFixture.stored_identity,
  payload_checksum: runtimePolicyFixture.payload_checksum,
  value: runtimePolicyFixture.policy,
};

const effectivePolicyFixture = {
  ...runtimePolicyFixture.effective_identity,
  payload_checksum: runtimePolicyFixture.payload_checksum,
};

const firstNode = workflowSpecFixture.nodes[0];
if (firstNode === undefined)
  throw new Error("Workflow fixture must contain a node");

const nodeBaseFixture = {
  id: firstNode.id,
  type_version: firstNode.type_version,
  scope: firstNode.scope,
  custom_label: firstNode.custom_label,
  description: firstNode.description,
  input_bindings: firstNode.input_bindings,
  execution_policy: firstNode.execution_policy,
};

const strictLiteralOneCases = [
  {
    name: "RestrictedTemplate.version",
    schema: restrictedTemplateSchema,
    payload: { version: 1, segments: [] },
    path: ["version"],
  },
  {
    name: "RestrictedJsonTemplate.version",
    schema: restrictedJsonTemplateSchema,
    payload: { version: 1, template: {}, bindings: {} },
    path: ["version"],
  },
  {
    name: "WorkflowNodeSpecBase.type_version",
    schema: workflowNodeSpecBaseSchema,
    payload: nodeBaseFixture,
    path: ["type_version"],
  },
  {
    name: "WorkflowSpecV1.schema_version",
    schema: workflowSpecV1Schema,
    payload: workflowSpecFixture,
    path: ["schema_version"],
  },
  {
    name: "CanvasDocumentV1.schema_version",
    schema: canvasDocumentV1Schema,
    payload: canvasFixture,
    path: ["schema_version"],
  },
  {
    name: "WorkflowEventEnvelopeV1.schema_version",
    schema: workflowEventEnvelopeV1Schema,
    payload: eventFixture,
    path: ["schema_version"],
  },
  {
    name: "PortDerivationV1.version",
    schema: nodeTypeDefinitionSchema,
    payload: workflowNodeRegistryV1[0],
    path: ["port_derivation", "version"],
  },
  {
    name: "ResolvedWorkflowInstancePortsV1.schema_version",
    schema: resolvedWorkflowInstancePortsV1Schema,
    payload: resolvedPortsFixture,
    path: ["schema_version"],
  },
  {
    name: "NodeCatalogResponseV1.schema_version",
    schema: nodeCatalogResponseV1Schema,
    payload: catalogFixture,
    path: ["schema_version"],
  },
  {
    name: "WorkflowRuntimePolicyV1.schema_version",
    schema: workflowRuntimePolicyV1Schema,
    payload: runtimePolicyFixture.policy,
    path: ["schema_version"],
  },
  {
    name: "WorkflowRuntimeStoredPolicyV1.schema_version",
    schema: workflowRuntimeStoredPolicyV1Schema,
    payload: storedPolicyFixture,
    path: ["schema_version"],
  },
  {
    name: "WorkflowRuntimeEffectivePolicyV1.schema_version",
    schema: workflowRuntimeEffectivePolicyV1Schema,
    payload: effectivePolicyFixture,
    path: ["schema_version"],
  },
  {
    name: "WorkflowRunAdmissionResponseV1.schema_version",
    schema: workflowRunAdmissionResponseV1Schema,
    payload: runFixture.admission_response,
    path: ["schema_version"],
  },
  {
    name: "WorkflowOwnerPrivateRunV1.schema_version",
    schema: workflowOwnerPrivateRunV1Schema,
    payload: runFixture.owner_private_run,
    path: ["schema_version"],
  },
  {
    name: "WorkflowPrivateRunAuthorityV1.schema_version",
    schema: workflowRunContractFixtureV1Schema,
    payload: runFixture,
    path: ["authority_bundles", 0, "run", "schema_version"],
  },
  {
    name: "WorkflowPrivateJobV1.schema_version",
    schema: workflowPrivateJobV1Schema,
    payload: runFixture.cancelled_before_start_job,
    path: ["schema_version"],
  },
  {
    name: "WorkflowRunJobEpochMappingV1.schema_version",
    schema: workflowRunContractFixtureV1Schema,
    payload: runFixture,
    path: ["authority_bundles", 0, "mapping", "schema_version"],
  },
  {
    name: "WorkflowRunJobAuthorityV1.schema_version",
    schema: workflowRunJobAuthorityV1Schema,
    payload: runFixture.authority_bundles[0],
    path: ["schema_version"],
  },
  {
    name: "WorkflowRunContractFixtureV1.schema_version",
    schema: workflowRunContractFixtureV1Schema,
    payload: runFixture,
    path: ["schema_version"],
  },
  {
    name: "WorkflowRunSnapshotIdentityV1.schema_version",
    schema: workflowRunSnapshotIdentityV1Schema,
    payload: runFixture.compiler_snapshot_contract.snapshot_identity,
    path: ["schema_version"],
  },
  {
    name: "WorkflowCompilerSnapshotContractV1.schema_version",
    schema: workflowCompilerSnapshotContractV1Schema,
    payload: runFixture.compiler_snapshot_contract,
    path: ["schema_version"],
  },
] as const;

describe("strict literal-one contracts", () => {
  it.each(strictLiteralOneCases)(
    "$name rejects shared non-integer JSON scalars",
    ({ path, payload, schema }) => {
      expect(schema.safeParse(payload).success).toBe(true);
      for (const invalidValue of invalidFixture.strict_literal_one_invalid_values) {
        const result = schema.safeParse(
          replaceAtPath(payload, path, invalidValue),
        );
        expect(result.success).toBe(false);
        if (result.success) continue;
        expect(
          result.error.issues.some(
            (issue) => JSON.stringify(issue.path) === JSON.stringify(path),
          ),
        ).toBe(true);
      }
    },
  );
});
