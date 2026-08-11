import { describe, expect, it } from "@rstest/core";

import { serializeCanonicalJsonValue } from "@/core/project-workflows/canonical";
import {
  assessWorkflowSchemaCompatibility,
  workflowCompilerSnapshotContractV1Schema,
  workflowSchemaCompatibilityCaseV1Schema,
} from "@/core/project-workflows/compatibility";
import * as publicRunContracts from "@/core/project-workflows/run-contracts";
import {
  WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES,
  WORKFLOW_RUN_INPUT_MAX_DEPTH,
  WORKFLOW_RUN_INPUT_MAX_NODES,
  workflowOwnerPrivateRunV1Schema,
  workflowRunAdmissionRequestV1Schema,
  workflowRunAdmissionResponseV1Schema,
} from "@/core/project-workflows/run-contracts";
import type { JsonValue } from "@/core/project-workflows/types";

import fixtureValue from "../../../fixtures/workflows/workflow-run-contracts-v1.json";
import invalidFixtureValue from "../../../fixtures/workflows/workflow-run-invalid-v1.json";
import {
  workflowExecutionReferenceV1Schema,
  workflowPrivateJobV1Schema,
  workflowRunContractFixtureV1Schema,
  workflowRunJobAuthorityV1Schema,
} from "../../../support/workflow-private-run-contracts";

const cloneFixture = () => structuredClone(fixtureValue);
const cloneInvalidFixture = () => structuredClone(invalidFixtureValue);
const requiredItem = <Value>(
  values: readonly Value[],
  index: number,
): Value => {
  const value = values[index];
  if (value === undefined)
    throw new Error(`missing fixture item at index ${index}`);
  return value;
};

const safeRunError = () => ({
  code: "WORKFLOW_INPUT_INVALID" as const,
  safe_message: "工作流执行失败",
  line: null,
  column: null,
});

const ownerRunForStatus = (status: string): Record<string, unknown> => ({
  ...cloneFixture().owner_private_run,
  status,
  started_at: status === "queued" ? null : "2026-08-10T01:00:01Z",
  completed_at: [
    "succeeded",
    "failed",
    "cancelled",
    "side_effect_unknown",
  ].includes(status)
    ? "2026-08-10T01:00:02Z"
    : null,
  error:
    status === "failed" || status === "side_effect_unknown"
      ? safeRunError()
      : null,
});

const privateJobForStatus = (status: string): Record<string, unknown> => ({
  ...requiredItem(cloneFixture().authority_bundles, 0).job,
  status,
  attempt_count: status === "queued" ? 0 : 1,
  started_at: status === "queued" ? null : "2026-08-10T01:00:01Z",
  completed_at: ["succeeded", "failed", "cancelled", "dead"].includes(status)
    ? "2026-08-10T01:00:02Z"
    : null,
  public_error_code: ["retry_wait", "failed", "dead"].includes(status)
    ? "WORKFLOW_TEMPORARY"
    : null,
});

const invalidInputPayload = (
  testCase: (typeof invalidFixtureValue.input_cases)[number],
): Record<string, unknown> => {
  if (testCase.kind === "input_id") {
    return { [testCase.value ?? ""]: 1 };
  }
  if (testCase.kind === "unpaired_surrogate_value") {
    return { value: "\ud800" };
  }
  if (testCase.kind === "unpaired_surrogate_key") {
    return { value: { ["bad\ud800"]: 1 } };
  }
  if (testCase.kind === "depth") {
    let nested: unknown = "leaf";
    for (let level = 0; level < (testCase.array_levels ?? 0); level += 1) {
      nested = [nested];
    }
    return { value: nested };
  }
  if (testCase.kind === "node_count") {
    return { value: Array(testCase.array_length ?? 0).fill(null) };
  }
  if (testCase.kind === "canonical_bytes") {
    return { value: "x".repeat(testCase.string_length ?? 0) };
  }
  throw new Error(`unknown invalid input fixture kind: ${testCase.kind}`);
};

describe("WorkflowRun public/private contract boundary", () => {
  it("keeps server-private authority schemas out of the production module", () => {
    expect(Object.keys(publicRunContracts).sort()).toEqual(
      [
        "WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES",
        "WORKFLOW_RUN_INPUT_MAX_DEPTH",
        "WORKFLOW_RUN_INPUT_MAX_NODES",
        "workflowOwnerPrivateRunV1Schema",
        "workflowRunAdmissionRequestV1Schema",
        "workflowRunAdmissionResponseV1Schema",
      ].sort(),
    );
  });

  it("round-trips the shared Python/TypeScript fixture", () => {
    const fixture = workflowRunContractFixtureV1Schema.parse(fixtureValue);

    expect(fixture.schema_version).toBe(1);
    expect(fixture.admission_response.status).toBe("queued");
    expect(
      fixture.authority_bundles.map(({ mapping }) => mapping.cause),
    ).toEqual(["initial", "resume"]);
    expect(
      fixture.authority_bundles.map(({ mapping }) => mapping.execution_epoch),
    ).toEqual([1, 2]);
  });

  it("accepts cancellation before start as a terminal Run and zero-attempt Job", () => {
    const fixture = cloneFixture();
    const run = workflowOwnerPrivateRunV1Schema.parse(
      fixture.cancelled_before_start_run,
    );
    const job = workflowPrivateJobV1Schema.parse(
      fixture.cancelled_before_start_job,
    );

    expect(run.status).toBe("cancelled");
    expect(run.started_at).toBeNull();
    expect(run.completed_at).not.toBeNull();
    expect(job.status).toBe("cancelled");
    expect(job.attempt_count).toBe(0);
    expect(job.started_at).toBeNull();
    expect(job.completed_at).not.toBeNull();
  });

  it("accepts exact portable admission input boundaries", () => {
    let exactDepth: unknown = "leaf";
    for (let level = 0; level < WORKFLOW_RUN_INPUT_MAX_DEPTH - 1; level += 1) {
      exactDepth = [exactDepth];
    }
    const exactNodes = Array(WORKFLOW_RUN_INPUT_MAX_NODES - 2).fill(null);
    const emptyPayloadBytes = new TextEncoder().encode(
      serializeCanonicalJsonValue({ payload: "" }),
    ).byteLength;
    const exactBytes = "x".repeat(
      WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES - emptyPayloadBytes,
    );

    expect(
      workflowRunAdmissionRequestV1Schema.safeParse({
        workflow_version_id: null,
        inputs: {
          [`a${"_".repeat(127)}`]: "汉字😀",
          "allowed.path:-_": true,
        },
      }).success,
    ).toBe(true);
    expect(
      workflowRunAdmissionRequestV1Schema.safeParse({
        workflow_version_id: null,
        inputs: { value: exactDepth },
      }).success,
    ).toBe(true);
    expect(
      workflowRunAdmissionRequestV1Schema.safeParse({
        workflow_version_id: null,
        inputs: { value: exactNodes },
      }).success,
    ).toBe(true);

    const exactByteInputs = { payload: exactBytes } satisfies Record<
      string,
      JsonValue
    >;
    expect(
      new TextEncoder().encode(serializeCanonicalJsonValue(exactByteInputs))
        .byteLength,
    ).toBe(WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES);
    expect(
      workflowRunAdmissionRequestV1Schema.safeParse({
        workflow_version_id: null,
        inputs: exactByteInputs,
      }).success,
    ).toBe(true);
  });

  it("rejects the shared invalid admission input corpus", () => {
    for (const testCase of cloneInvalidFixture().input_cases) {
      expect(
        workflowRunAdmissionRequestV1Schema.safeParse({
          workflow_version_id: null,
          inputs: invalidInputPayload(testCase),
        }).success,
        testCase.id,
      ).toBe(false);
    }
  });

  it("rejects 65k subnormal values without materializing amplified JSON", () => {
    const amplified = Array(WORKFLOW_RUN_INPUT_MAX_NODES - 2).fill(
      Number.MIN_VALUE,
    );

    const result = workflowRunAdmissionRequestV1Schema.safeParse({
      workflow_version_id: null,
      inputs: { value: amplified },
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.map((issue) => issue.message)).toContain(
        "Workflow inputs exceed the maximum canonical UTF-8 byte count",
      );
    }
  });

  it("accepts and preserves canonical RFC3339 UTC timestamps", () => {
    const parsed = workflowOwnerPrivateRunV1Schema.parse({
      ...ownerRunForStatus("running"),
      created_at: "2026-08-10T01:00:00.1Z",
      started_at: "2026-08-10T01:00:01.123456Z",
    });

    expect(parsed.created_at).toBe("2026-08-10T01:00:00.1Z");
    expect(parsed.started_at).toBe("2026-08-10T01:00:01.123456Z");
  });

  it("rejects the shared invalid timestamp corpus for Run, Job, and mapping", () => {
    for (const timestamp of cloneInvalidFixture().time_values) {
      expect(
        workflowOwnerPrivateRunV1Schema.safeParse({
          ...ownerRunForStatus("running"),
          created_at: timestamp,
        }).success,
        `run ${timestamp}`,
      ).toBe(false);
      expect(
        workflowPrivateJobV1Schema.safeParse({
          ...privateJobForStatus("running"),
          created_at: timestamp,
        }).success,
        `job ${timestamp}`,
      ).toBe(false);

      const authority = requiredItem(cloneFixture().authority_bundles, 0);
      authority.job.created_at = timestamp;
      authority.mapping.created_at = timestamp;
      expect(
        workflowRunJobAuthorityV1Schema.safeParse(authority).success,
        `mapping ${timestamp}`,
      ).toBe(false);
    }
  });

  it.each([
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "side_effect_unknown",
  ])("accepts the exact owner Run status shape %s", (status) => {
    expect(
      workflowOwnerPrivateRunV1Schema.safeParse(ownerRunForStatus(status))
        .success,
    ).toBe(true);
  });

  it("rejects the shared invalid owner Run status corpus", () => {
    for (const testCase of cloneInvalidFixture().run_cases) {
      const run = ownerRunForStatus(testCase.status);
      if (testCase.error_mode === "safe") run.error = safeRunError();
      if (testCase.error_mode === "none") run.error = null;
      for (const [key, value] of Object.entries(testCase)) {
        if (!["id", "status", "error_mode"].includes(key)) run[key] = value;
      }
      expect(
        workflowOwnerPrivateRunV1Schema.safeParse(run).success,
        testCase.id,
      ).toBe(false);
    }
  });

  it.each([
    "queued",
    "leased",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "dead",
  ])("accepts the exact private Job status shape %s", (status) => {
    expect(
      workflowPrivateJobV1Schema.safeParse(privateJobForStatus(status)).success,
    ).toBe(true);
  });

  it("rejects the shared invalid private Job status corpus", () => {
    for (const testCase of cloneInvalidFixture().job_cases) {
      const job = privateJobForStatus(testCase.status);
      for (const [key, value] of Object.entries(testCase)) {
        if (!["id", "status"].includes(key)) job[key] = value;
      }
      expect(
        workflowPrivateJobV1Schema.safeParse(job).success,
        testCase.id,
      ).toBe(false);
    }
  });

  it.each([
    "project_id",
    "owner_user_id",
    "origin_trace_id",
    "execution_epoch",
    "current_job_id",
    "checkpoint_id",
    "credential_version_id",
    "required_worker_profile_digest",
    "idempotency_key",
  ])("rejects server-owned admission field %s", (field) => {
    const payload = {
      ...cloneFixture().admission_request,
      [field]: "forbidden",
    };
    expect(workflowRunAdmissionRequestV1Schema.safeParse(payload).success).toBe(
      false,
    );
  });

  it.each([
    "project_id",
    "owner_user_id",
    "origin_trace_id",
    "current_job_id",
    "job_id",
    "attempt_count",
    "lease_token",
  ])("rejects private field %s from public Run DTOs", (field) => {
    const fixture = cloneFixture();
    expect(
      workflowRunAdmissionResponseV1Schema.safeParse({
        ...fixture.admission_response,
        [field]: "forbidden",
      }).success,
    ).toBe(false);
    expect(
      workflowOwnerPrivateRunV1Schema.safeParse({
        ...fixture.owner_private_run,
        [field]: "forbidden",
      }).success,
    ).toBe(false);
  });

  it("enforces terminal timestamps and retry identity", () => {
    const run = cloneFixture().owner_private_run;
    expect(
      workflowOwnerPrivateRunV1Schema.safeParse({
        ...run,
        status: "succeeded",
        completed_at: null,
      }).success,
    ).toBe(false);
    expect(
      workflowOwnerPrivateRunV1Schema.safeParse({
        ...run,
        retry_of_run_id: run.run_id,
      }).success,
    ).toBe(false);

    expect(
      workflowOwnerPrivateRunV1Schema.safeParse({
        ...run,
        status: "side_effect_unknown",
        completed_at: "2026-08-10T01:00:02Z",
        error: {
          code: "SIDE_EFFECT_STATE_UNKNOWN",
          safe_message: "无法确认远端写请求是否已生效",
          line: null,
          column: null,
        },
      }).success,
    ).toBe(true);
  });

  it("keeps Agent and Workflow execution references mutually exclusive", () => {
    const references = cloneFixture().execution_references;
    const agent = requiredItem(references, 0);
    const workflow = requiredItem(references, 1);
    expect(workflowExecutionReferenceV1Schema.parse(agent).kind).toBe(
      "agent_run",
    );
    expect(workflowExecutionReferenceV1Schema.parse(workflow).kind).toBe(
      "workflow_run",
    );
    expect(
      workflowExecutionReferenceV1Schema.safeParse({
        ...workflow,
        run_id: agent.run_id,
      }).success,
    ).toBe(false);
  });

  it.each(invalidFixtureValue.uuid_values)(
    "rejects $id at every Run DTO boundary",
    ({ value: uuid }) => {
      const fixture = cloneFixture();

      expect(
        workflowRunAdmissionRequestV1Schema.safeParse({
          ...fixture.admission_request,
          workflow_version_id: uuid,
        }).success,
      ).toBe(false);

      expect(
        workflowRunAdmissionResponseV1Schema.safeParse({
          ...fixture.admission_response,
          run_id: uuid,
        }).success,
      ).toBe(false);

      expect(
        workflowOwnerPrivateRunV1Schema.safeParse({
          ...fixture.owner_private_run,
          workflow_id: uuid,
        }).success,
      ).toBe(false);

      const authority = requiredItem(fixture.authority_bundles, 0);
      expect(
        workflowRunJobAuthorityV1Schema.safeParse({
          ...authority,
          run: { ...authority.run, owner_user_id: uuid },
        }).success,
      ).toBe(false);
      expect(
        workflowPrivateJobV1Schema.safeParse({
          ...authority.job,
          project_id: uuid,
        }).success,
      ).toBe(false);
      expect(
        workflowRunJobAuthorityV1Schema.safeParse({
          ...authority,
          mapping: { ...authority.mapping, job_id: uuid },
        }).success,
      ).toBe(false);

      const agentReference = requiredItem(fixture.execution_references, 0);
      expect(
        workflowExecutionReferenceV1Schema.safeParse({
          ...agentReference,
          run_id: uuid,
        }).success,
      ).toBe(false);

      const workflowReference = requiredItem(fixture.execution_references, 1);
      expect(
        workflowExecutionReferenceV1Schema.safeParse({
          ...workflowReference,
          workflow_run_id: uuid,
        }).success,
      ).toBe(false);

      for (const snapshotField of [
        "workflow_run_id",
        "workflow_version_id",
      ] as const) {
        expect(
          workflowCompilerSnapshotContractV1Schema.safeParse({
            ...fixture.compiler_snapshot_contract,
            snapshot_identity: {
              ...fixture.compiler_snapshot_contract.snapshot_identity,
              [snapshotField]: uuid,
            },
          }).success,
        ).toBe(false);
      }
    },
  );

  it.each(invalidFixtureValue.public_error_codes)(
    "rejects non-canonical public_error_code %s",
    (code) => {
      const job = requiredItem(cloneFixture().authority_bundles, 0).job;
      expect(
        workflowPrivateJobV1Schema.safeParse({
          ...job,
          public_error_code: code,
        }).success,
      ).toBe(false);
    },
  );

  it("maps initial to epoch 1, resume to a later epoch, and leaves automatic attempts in the same epoch", () => {
    const bundles = cloneFixture().authority_bundles;
    const initial = requiredItem(bundles, 0);
    const resumed = requiredItem(bundles, 1);
    initial.mapping.execution_epoch = 2;
    expect(workflowRunJobAuthorityV1Schema.safeParse(initial).success).toBe(
      false,
    );
    resumed.mapping.execution_epoch = 1;
    expect(workflowRunJobAuthorityV1Schema.safeParse(resumed).success).toBe(
      false,
    );

    const automaticRetry = requiredItem(cloneFixture().authority_bundles, 0);
    automaticRetry.job.attempt_count = 2;
    expect(
      workflowRunJobAuthorityV1Schema.parse(automaticRetry).mapping
        .execution_epoch,
    ).toBe(1);
  });

  it.each([
    ["current_job_id", "99999999-9999-4999-8999-999999999999"],
    ["origin_trace_id", "different-trace"],
    ["mapping_job_id", "99999999-9999-4999-8999-999999999999"],
    ["workflow_epoch", 2],
    ["profile_digest", "d".repeat(64)],
  ] as const)("rejects authority drift at %s", (field, value) => {
    const payload = requiredItem(cloneFixture().authority_bundles, 0);
    if (field === "current_job_id")
      payload.run.current_job_id = value as string;
    if (field === "origin_trace_id")
      payload.job.origin_trace_id = value as string;
    if (field === "mapping_job_id") payload.mapping.job_id = value as string;
    if (field === "workflow_epoch")
      payload.job.execution_reference.workflow_epoch = value as number;
    if (field === "profile_digest")
      payload.job.execution_reference.required_worker_profile_digest = value;

    expect(workflowRunJobAuthorityV1Schema.safeParse(payload).success).toBe(
      false,
    );
  });
});

describe("Workflow schema/compiler compatibility", () => {
  it("matches every shared compatibility decision without silent upgrade", () => {
    for (const rawCase of cloneFixture().compatibility_cases) {
      const compatibilityCase =
        workflowSchemaCompatibilityCaseV1Schema.parse(rawCase);
      const assessed = assessWorkflowSchemaCompatibility({
        artifactKind: compatibilityCase.artifact_kind,
        source: compatibilityCase.source,
        supported: compatibilityCase.supported,
        migrationPaths: compatibilityCase.migration_paths,
      });
      expect(assessed).toEqual(compatibilityCase.expected);
      expect(assessed.silent_upgrade_allowed).toBe(false);
    }
  });

  it("keeps unknown future Run snapshots read-only", () => {
    const current = requiredItem(cloneFixture().compatibility_cases, 0);
    const assessed = assessWorkflowSchemaCompatibility({
      artifactKind: "run_snapshot",
      source: {
        ...current.source,
        compiler_contract_version: 99,
      },
      supported: current.supported,
      migrationPaths: [],
    });

    expect(assessed).toMatchObject({
      status: "read_only_unsupported",
      reason: "RUN_SNAPSHOT_MIGRATION_FORBIDDEN",
      read_only: true,
      silent_upgrade_allowed: false,
    });
  });

  it("never applies an explicit path to a Published Version", () => {
    const published = workflowSchemaCompatibilityCaseV1Schema.parse(
      requiredItem(cloneFixture().compatibility_cases, 2),
    );
    expect(
      assessWorkflowSchemaCompatibility({
        artifactKind: published.artifact_kind,
        source: published.source,
        supported: published.supported,
        migrationPaths: published.migration_paths,
      }),
    ).toEqual(published.expected);

    expect(
      workflowSchemaCompatibilityCaseV1Schema.safeParse({
        ...published,
        expected: { ...published.expected, auto_upgrade: true },
      }).success,
    ).toBe(false);
  });

  it("requires the compiler identity to match the frozen Run snapshot exactly", () => {
    const contract = cloneFixture().compiler_snapshot_contract;
    expect(workflowCompilerSnapshotContractV1Schema.parse(contract)).toEqual(
      contract,
    );
    contract.snapshot_identity.compiler_contract_version = 2;
    expect(
      workflowCompilerSnapshotContractV1Schema.safeParse(contract).success,
    ).toBe(false);
  });
});
