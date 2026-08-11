import { describe, expect, it } from "@rstest/core";

import * as productionProjectWorkflows from "@/core/project-workflows";
import {
  canonicalWorkflowCursorSchema,
  workflowActivationIdSchema,
  workflowAttemptSchema,
  workflowEmptyEventPayloadV1Schema,
  workflowErrorCodeSchema,
  workflowErrorCodes,
  workflowEventEnvelopeV1Schema,
  workflowEventTypesV1,
  workflowIterationPathSchema,
  workflowNodeCompletedEventPayloadV1Schema,
  workflowNodeDeltaEventPayloadV1Schema,
  workflowNodeFailedEventPayloadV1Schema,
  workflowNodeLastRunV1Schema,
  workflowNodeLifecycleEventPayloadV1Schema,
  workflowNodeLogEventPayloadV1Schema,
  workflowProjectReadinessV1Schema,
  projectWorkflowEntryEnabled,
  workflowRunCancelledEventPayloadV1Schema,
  workflowRunCompletedEventPayloadV1Schema,
  workflowRunFailedEventPayloadV1Schema,
  workflowRunSideEffectUnknownEventPayloadV1Schema,
  workflowRunStatusV1Schema,
  workflowValidationIssueV1Schema,
  safePreviewV1Schema,
} from "@/core/project-workflows";
import * as productionTransport from "@/core/project-workflows/transport";

import httpOutcomeFixture from "../../../fixtures/workflows/workflow-http-outcomes-v1.json";
import runInvalidFixture from "../../../fixtures/workflows/workflow-run-invalid-v1.json";
import {
  workflowHttpBodyV1Schema,
  workflowHttpSettledOutcomeV1Schema,
} from "../../../support/workflow-private-http-outcome-contracts";

const RUN_ID = "00000000-0000-4000-8000-000000000010";
const NODE_ID = "00000000-0000-4000-8000-000000000011";
const WORKFLOW_VERSION_ID = "00000000-0000-4000-8000-000000000012";
const SCOPE_PATH_HASH = "a".repeat(64);

const readyPayload = {
  status: "ready",
  code: "WORKFLOW_CONTROL_PLANE_READY",
  workflow_enabled: true,
  schema_ready: true,
  admission_ready: false,
  request_id: "req-ready",
} as const;

describe("WorkflowProjectReadinessV1", () => {
  it.each([
    readyPayload,
    { ...readyPayload, admission_ready: true },
    {
      status: "ready",
      code: "WORKFLOW_DISABLED",
      workflow_enabled: false,
      schema_ready: true,
      admission_ready: false,
      request_id: "req-disabled",
    },
    {
      status: "unavailable",
      code: "WORKFLOW_SCHEMA_UNAVAILABLE",
      workflow_enabled: false,
      schema_ready: false,
      admission_ready: false,
      request_id: "req-schema",
    },
    {
      status: "unavailable",
      code: "WORKFLOW_POLICY_UNAVAILABLE",
      workflow_enabled: false,
      schema_ready: true,
      admission_ready: false,
      request_id: "req-policy",
    },
  ])("accepts only a frozen readiness combination", (payload) => {
    expect(workflowProjectReadinessV1Schema.parse(payload)).toEqual(payload);
  });

  it.each([
    { status: "unavailable" },
    { code: "WORKFLOW_DISABLED" },
    { schema_ready: false },
    { admission_ready: 1 },
    { provider_id: "private-provider" },
    { origin_trace_id: "private-trace" },
    { code: "WORKFLOW_UNKNOWN" },
  ])(
    "rejects contradictions, coercion, unknown values, and private fields",
    (change) => {
      expect(
        workflowProjectReadinessV1Schema.safeParse({
          ...readyPayload,
          ...change,
        }).success,
      ).toBe(false);
    },
  );

  it.each([
    [readyPayload, true],
    [{ ...readyPayload, admission_ready: true }, true],
    [
      {
        status: "ready",
        code: "WORKFLOW_DISABLED",
        workflow_enabled: false,
        schema_ready: true,
        admission_ready: false,
        request_id: "req-disabled",
      },
      false,
    ],
    [
      {
        status: "unavailable",
        code: "WORKFLOW_SCHEMA_UNAVAILABLE",
        workflow_enabled: false,
        schema_ready: false,
        admission_ready: false,
        request_id: "req-schema",
      },
      false,
    ],
    [
      {
        status: "unavailable",
        code: "WORKFLOW_POLICY_UNAVAILABLE",
        workflow_enabled: false,
        schema_ready: true,
        admission_ready: false,
        request_id: "req-policy",
      },
      false,
    ],
  ] as const)(
    "gates navigation from strict control-plane readiness without admission state",
    (payload, expected) => {
      const readiness = workflowProjectReadinessV1Schema.parse(payload);
      expect(projectWorkflowEntryEnabled(false, true, readiness)).toBe(
        expected,
      );
    },
  );

  it("also requires dynamic mode and workflow.read", () => {
    const readiness = workflowProjectReadinessV1Schema.parse(readyPayload);

    expect(projectWorkflowEntryEnabled(true, true, readiness)).toBe(false);
    expect(projectWorkflowEntryEnabled(false, false, readiness)).toBe(false);
    expect(projectWorkflowEntryEnabled(false, true, undefined)).toBe(false);
  });
});

const eventBase = {
  schema_version: 1,
  run_id: RUN_ID,
  workflow_version_id: WORKFLOW_VERSION_ID,
  seq: "42",
  occurred_at: "2026-08-10T01:02:03+08:00",
} as const;

const nodeEventIdentity = {
  node_id: NODE_ID,
  activation_id: "activation-01",
  scope_path_hash: SCOPE_PATH_HASH,
  iteration_path: [3],
  attempt: 1,
} as const;

const runEventIdentity = {
  node_id: null,
  activation_id: null,
  scope_path_hash: null,
  iteration_path: [],
  attempt: null,
} as const;

const safeError = {
  code: "WORKFLOW_HTTP_TIMEOUT",
  safe_message: "请求超时",
  line: null,
  column: null,
} as const;

describe("canonical Workflow transport UUIDs", () => {
  it.each(runInvalidFixture.uuid_values)(
    "rejects $id in every UUID-bearing transport DTO",
    ({ value: uuid }) => {
      const issue = {
        severity: "error",
        code: "WORKFLOW_PORT_NOT_FOUND",
        message: "端口不存在",
        path: ["nodes", "0"],
        node_id: NODE_ID,
        edge_id: null,
        port_id: null,
      } as const;
      expect(
        workflowValidationIssueV1Schema.safeParse({
          ...issue,
          node_id: uuid,
        }).success,
      ).toBe(false);

      const event = {
        ...eventBase,
        ...nodeEventIdentity,
        type: "workflow.node.started",
        payload: { node_type: "llm" },
      } as const;
      for (const field of [
        "run_id",
        "workflow_version_id",
        "node_id",
      ] as const) {
        expect(
          workflowEventEnvelopeV1Schema.safeParse({
            ...event,
            [field]: uuid,
          }).success,
        ).toBe(false);
      }

      const lastRun = {
        run_id: RUN_ID,
        node_id: NODE_ID,
        activation_id: "activation-01",
        iteration_path: [1],
        attempt: 1,
        status: "running",
      } as const;
      for (const field of ["run_id", "node_id"] as const) {
        expect(
          workflowNodeLastRunV1Schema.safeParse({
            ...lastRun,
            [field]: uuid,
          }).success,
        ).toBe(false);
      }
    },
  );
});

describe("WorkflowHttpSettledOutcomeV1", () => {
  it("keeps private settled-effect schemas out of production exports", () => {
    for (const privateExport of [
      "workflowHttpHeaderV1Schema",
      "workflowHttpBodyV1Schema",
      "workflowHttpObservedByteCountV1Schema",
      "workflowHttpResponseV1Schema",
      "workflowHttpSettledOutcomeV1Schema",
    ]) {
      for (const productionModule of [
        productionTransport,
        productionProjectWorkflows,
      ]) {
        expect(
          Object.prototype.hasOwnProperty.call(productionModule, privateExport),
        ).toBe(false);
      }
    }
  });

  it("round-trips the shared typed settled-outcome corpus", () => {
    expect(
      httpOutcomeFixture.map((outcome) =>
        workflowHttpSettledOutcomeV1Schema.parse(outcome),
      ),
    ).toHaveLength(3);
  });

  it("separates exact raw counts from retained canonical JSON bytes", () => {
    const outcome = structuredClone(
      workflowHttpSettledOutcomeV1Schema.parse(httpOutcomeFixture[0]),
    );
    if (outcome.kind === "response_invalid")
      throw new Error("fixture must contain a replayable response");
    outcome.response.body = { kind: "json", value: 1e-7 };
    outcome.response.wire_byte_count = { value: 4, relation: "exact" };
    outcome.response.decoded_byte_count = { value: 4, relation: "exact" };
    outcome.response.retained_body_byte_count = 70;
    expect(workflowHttpSettledOutcomeV1Schema.safeParse(outcome).success).toBe(
      true,
    );
  });

  it("rejects binary64-amplified JSON within the retained-body budget", () => {
    expect(
      workflowHttpBodyV1Schema.safeParse({
        kind: "json",
        value: Array.from({ length: 65_535 }, () => Number.MIN_VALUE),
      }).success,
    ).toBe(false);
  });

  it("requires cap-aware observations and rejects secret response material", () => {
    const limit = structuredClone(
      workflowHttpSettledOutcomeV1Schema.parse(httpOutcomeFixture[2]),
    );
    if (limit.kind !== "response_invalid")
      throw new Error("fixture must contain a response-invalid outcome");
    limit.wire_byte_count.relation = "exact";
    limit.decoded_byte_count.relation = "exact";
    expect(workflowHttpSettledOutcomeV1Schema.safeParse(limit).success).toBe(
      false,
    );

    const response = structuredClone(
      workflowHttpSettledOutcomeV1Schema.parse(httpOutcomeFixture[0]),
    );
    if (response.kind === "response_invalid")
      throw new Error("fixture must contain a replayable response");
    response.response.headers[0]!.name = "set-cookie";
    expect(workflowHttpSettledOutcomeV1Schema.safeParse(response).success).toBe(
      false,
    );
  });
});

describe("WorkflowEventEnvelopeV1", () => {
  const validEvents: Array<Record<string, unknown>> = [
    {
      ...eventBase,
      ...runEventIdentity,
      type: "workflow.run.started",
      payload: {},
    },
    {
      ...eventBase,
      ...nodeEventIdentity,
      type: "workflow.node.queued",
      payload: { node_type: "start" },
    },
    {
      ...eventBase,
      ...nodeEventIdentity,
      type: "workflow.node.started",
      payload: { node_type: "loop" },
    },
    {
      ...eventBase,
      ...nodeEventIdentity,
      type: "workflow.node.delta",
      payload: { node_type: "llm", text: "chunk", truncated: false },
    },
    {
      ...eventBase,
      ...nodeEventIdentity,
      type: "workflow.node.log",
      payload: {
        node_type: "python_code",
        stream: "stdout",
        text: "safe log tail",
        truncated: false,
      },
    },
    {
      ...eventBase,
      ...nodeEventIdentity,
      type: "workflow.node.completed",
      payload: {
        node_type: "condition",
        duration_ms: 12,
        output_preview: null,
        usage: { model_calls: 1, input_tokens: 10, output_tokens: 5 },
        branch_port_id: "matched",
        retry_count: 0,
        truncated: false,
      },
    },
    {
      ...eventBase,
      ...nodeEventIdentity,
      type: "workflow.node.failed",
      payload: {
        node_type: "http_request",
        duration_ms: 12,
        error: safeError,
        retry_count: 1,
      },
    },
    {
      ...eventBase,
      ...runEventIdentity,
      type: "workflow.run.completed",
      payload: { duration_ms: 42, output_preview: null },
    },
    {
      ...eventBase,
      ...runEventIdentity,
      type: "workflow.run.failed",
      payload: { duration_ms: 42, error: safeError },
    },
    {
      ...eventBase,
      ...runEventIdentity,
      type: "workflow.run.cancelled",
      payload: { duration_ms: null },
    },
    {
      ...eventBase,
      ...runEventIdentity,
      type: "workflow.run.side_effect_unknown",
      payload: {
        code: "SIDE_EFFECT_STATE_UNKNOWN",
        safe_message: "外部副作用状态未知",
      },
    },
  ];

  it("freezes all eleven event names and ten strict payload contracts", () => {
    expect(workflowEventTypesV1).toHaveLength(11);
    expect(workflowEmptyEventPayloadV1Schema.parse({})).toEqual({});
    expect(
      workflowNodeLifecycleEventPayloadV1Schema.parse({ node_type: "start" }),
    ).toEqual({ node_type: "start" });
    expect(
      workflowNodeDeltaEventPayloadV1Schema.parse({
        node_type: "llm",
        text: "chunk",
        truncated: false,
      }),
    ).toBeDefined();
    expect(
      workflowNodeLogEventPayloadV1Schema.parse({
        node_type: "python_code",
        stream: "stderr",
        text: "safe",
        truncated: false,
      }),
    ).toBeDefined();
    expect(
      workflowNodeCompletedEventPayloadV1Schema.parse(validEvents[5]!.payload),
    ).toBeDefined();
    expect(
      workflowNodeFailedEventPayloadV1Schema.parse(validEvents[6]!.payload),
    ).toBeDefined();
    expect(
      workflowRunCompletedEventPayloadV1Schema.parse(validEvents[7]!.payload),
    ).toBeDefined();
    expect(
      workflowRunFailedEventPayloadV1Schema.parse(validEvents[8]!.payload),
    ).toBeDefined();
    expect(
      workflowRunCancelledEventPayloadV1Schema.parse(validEvents[9]!.payload),
    ).toBeDefined();
    expect(
      workflowRunSideEffectUnknownEventPayloadV1Schema.parse(
        validEvents[10]!.payload,
      ),
    ).toBeDefined();

    for (const event of validEvents) {
      expect(workflowEventEnvelopeV1Schema.parse(event)).toEqual(event);
    }
  });

  it.each(["origin_trace_id", "source", "raw_logs", "private_field"])(
    "rejects private top-level field %s",
    (field) => {
      expect(
        workflowEventEnvelopeV1Schema.safeParse({
          ...validEvents[3],
          [field]: "private",
        }).success,
      ).toBe(false);
    },
  );

  it.each([
    "origin_trace_id",
    "source",
    "raw_logs",
    "credential_id",
    "provider_id",
    "output_json",
  ])("rejects arbitrary or private payload field %s", (field) => {
    const event = structuredClone(validEvents[3]!);
    event.payload = {
      ...(event.payload as Record<string, unknown>),
      [field]: "private",
    };
    expect(workflowEventEnvelopeV1Schema.safeParse(event).success).toBe(false);
  });

  it("rejects mismatched payloads and invalid activation identity", () => {
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        payload: {
          node_type: "python_code",
          stream: "stdout",
          text: "wrong payload",
          truncated: false,
        },
      }).success,
    ).toBe(false);
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        activation_id: undefined,
      }).success,
    ).toBe(false);
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        attempt: 0,
      }).success,
    ).toBe(false);
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        iteration_path: [0],
      }).success,
    ).toBe(false);
    const nodeWithoutIteration = { ...validEvents[3] };
    delete nodeWithoutIteration.iteration_path;
    expect(
      workflowEventEnvelopeV1Schema.safeParse(nodeWithoutIteration).success,
    ).toBe(false);
    const runWithoutIteration = { ...validEvents[0] };
    delete runWithoutIteration.iteration_path;
    expect(
      workflowEventEnvelopeV1Schema.safeParse(runWithoutIteration).success,
    ).toBe(false);
  });

  it("rejects node identity on run events and noncanonical transport metadata", () => {
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[7],
        ...nodeEventIdentity,
      }).success,
    ).toBe(false);
    for (const seq of [42, "042", "-1"]) {
      expect(
        workflowEventEnvelopeV1Schema.safeParse({
          ...validEvents[3],
          seq,
        }).success,
      ).toBe(false);
    }
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        occurred_at: "2026-08-10T01:02:03",
      }).success,
    ).toBe(false);
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        schema_version: 2,
      }).success,
    ).toBe(false);
    expect(
      workflowEventEnvelopeV1Schema.safeParse({
        ...validEvents[3],
        type: "workflow.node.private",
      }).success,
    ).toBe(false);
  });
});

describe("safe runtime projections", () => {
  it("accepts bounded SafePreview and rejects secret/private metadata", () => {
    const preview = {
      format: "json",
      text: '{"ok":true}',
      truncated: false,
      redacted: true,
      original_byte_count: 11,
    } as const;
    expect(safePreviewV1Schema.parse(preview)).toEqual(preview);
    expect(
      safePreviewV1Schema.safeParse({
        ...preview,
        credential_id: RUN_ID,
      }).success,
    ).toBe(false);
    expect(
      safePreviewV1Schema.safeParse({
        ...preview,
        original_byte_count: -1,
      }).success,
    ).toBe(false);
  });

  it("applies preview and log limits to UTF-8 bytes", () => {
    expect(
      safePreviewV1Schema.safeParse({
        format: "text",
        text: "a".repeat(65_536),
        truncated: false,
        redacted: false,
        original_byte_count: 65_536,
      }).success,
    ).toBe(true);
    expect(
      safePreviewV1Schema.safeParse({
        format: "text",
        text: "😀".repeat(16_385),
        truncated: true,
        redacted: false,
        original_byte_count: 65_540,
      }).success,
    ).toBe(false);
    expect(
      safePreviewV1Schema.safeParse({
        format: "text",
        text: "完成",
        truncated: false,
        redacted: false,
        original_byte_count: 1,
      }).success,
    ).toBe(false);
    expect(
      workflowNodeLogEventPayloadV1Schema.safeParse({
        node_type: "python_code",
        stream: "stdout",
        text: "😀".repeat(16_385),
        truncated: true,
      }).success,
    ).toBe(false);
  });

  it("accepts a stable ValidationIssue location and rejects authority fields", () => {
    const issue = {
      severity: "error",
      code: "WORKFLOW_PORT_NOT_FOUND",
      message: "端口不存在",
      path: ["nodes", "0", "input_bindings", "prompt"],
      node_id: NODE_ID,
      edge_id: "edge-1",
      port_id: "prompt",
    } as const;
    expect(workflowValidationIssueV1Schema.parse(issue)).toEqual(issue);
    expect(
      workflowValidationIssueV1Schema.safeParse({
        ...issue,
        project_id: RUN_ID,
      }).success,
    ).toBe(false);
    expect(
      workflowValidationIssueV1Schema.safeParse({
        ...issue,
        code: "workflow_port_not_found",
      }).success,
    ).toBe(false);
    for (const edgeId of ["has space", "a".repeat(129)]) {
      expect(
        workflowValidationIssueV1Schema.safeParse({
          ...issue,
          edge_id: edgeId,
        }).success,
      ).toBe(false);
    }
  });

  it("accepts WorkflowNodeLastRunV1 and rejects private or malformed identity", () => {
    const lastRun = {
      run_id: RUN_ID,
      node_id: NODE_ID,
      activation_id: "activation-01",
      iteration_path: [1, 2],
      attempt: 2,
      status: "succeeded",
      started_at: "2026-08-10T01:02:03+08:00",
      duration_ms: 12,
      input_preview: null,
      output_preview: {
        format: "summary",
        text: "完成",
        truncated: false,
        redacted: false,
        original_byte_count: null,
      },
      error: null,
      usage: { model_calls: 1, input_tokens: 10, output_tokens: 5 },
      branch_port_id: null,
      retry_count: 1,
      truncated: false,
    } as const;
    expect(workflowNodeLastRunV1Schema.parse(lastRun)).toEqual(lastRun);
    expect(
      workflowNodeLastRunV1Schema.safeParse({
        ...lastRun,
        origin_trace_id: "private",
      }).success,
    ).toBe(false);
    expect(
      workflowNodeLastRunV1Schema.safeParse({
        ...lastRun,
        started_at: "2026-08-10T01:02:03",
      }).success,
    ).toBe(false);
    expect(
      workflowNodeLastRunV1Schema.safeParse({
        ...lastRun,
        activation_id: "contains space",
      }).success,
    ).toBe(false);
  });
});

describe("closed status, error, cursor, and activation identities", () => {
  it("freezes first-wave WorkflowRun statuses", () => {
    for (const status of [
      "queued",
      "running",
      "succeeded",
      "failed",
      "cancelled",
      "side_effect_unknown",
    ]) {
      expect(workflowRunStatusV1Schema.parse(status)).toBe(status);
    }
    expect(workflowRunStatusV1Schema.safeParse("waiting_input").success).toBe(
      false,
    );
  });

  it("freezes the complete public WorkflowErrorCode enum", () => {
    expect(workflowErrorCodeSchema.options).toEqual(workflowErrorCodes);
    expect(workflowErrorCodes).toHaveLength(37);
    expect(workflowErrorCodeSchema.parse("SIDE_EFFECT_STATE_UNKNOWN")).toBe(
      "SIDE_EFFECT_STATE_UNKNOWN",
    );
    expect(
      workflowErrorCodeSchema.safeParse("WORKFLOW_PROVIDER_INTERNAL_PATH")
        .success,
    ).toBe(false);
  });

  it.each(["0", "1", "42", "9007199254740992", "9223372036854775807"])(
    "accepts canonical decimal cursor %s without numeric coercion",
    (cursor) => {
      expect(canonicalWorkflowCursorSchema.parse(cursor)).toBe(cursor);
    },
  );

  it.each([
    42,
    "",
    "00",
    "042",
    "-1",
    "+1",
    "1.0",
    "9223372036854775808",
    "9".repeat(256),
  ])("rejects noncanonical cursor %s", (cursor) => {
    expect(canonicalWorkflowCursorSchema.safeParse(cursor).success).toBe(false);
  });

  it("bounds activation, iteration, and attempt identities", () => {
    expect(workflowActivationIdSchema.parse("activation:1.2_test-run")).toBe(
      "activation:1.2_test-run",
    );
    expect(workflowIterationPathSchema.parse([1, 2, 3])).toEqual([1, 2, 3]);
    expect(workflowAttemptSchema.parse(1)).toBe(1);
    expect(workflowIterationPathSchema.parse([2_147_483_647])).toEqual([
      2_147_483_647,
    ]);
    expect(workflowAttemptSchema.parse(2_147_483_647)).toBe(2_147_483_647);

    expect(workflowActivationIdSchema.safeParse("").success).toBe(false);
    expect(workflowActivationIdSchema.safeParse("has space").success).toBe(
      false,
    );
    expect(workflowIterationPathSchema.safeParse([0]).success).toBe(false);
    expect(
      workflowIterationPathSchema.safeParse(Array.from({ length: 17 }, () => 1))
        .success,
    ).toBe(false);
    expect(workflowAttemptSchema.safeParse(0).success).toBe(false);
    expect(workflowIterationPathSchema.safeParse([2_147_483_648]).success).toBe(
      false,
    );
    expect(workflowAttemptSchema.safeParse(2_147_483_648).success).toBe(false);
    expect(
      workflowAttemptSchema.safeParse(Number.MAX_SAFE_INTEGER + 1).success,
    ).toBe(false);
  });
});
