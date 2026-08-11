import { describe, expect, it } from "@rstest/core";

import { WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES } from "@/core/project-workflows/run-contracts";
import {
  WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS,
  WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES,
  WORKFLOW_RUNTIME_MAX_INPUT_BYTES,
  WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE,
  serializeWorkflowRuntimePolicyChecksumInput,
  workflowRuntimeAdminPolicyV1Schema,
  workflowRuntimeEffectivePolicyV1Schema,
  workflowRuntimePolicyChecksum,
  workflowRuntimePolicyUpdateRequestV1Schema,
  workflowRuntimePolicyUpdateResponseV1Schema,
  workflowRuntimePolicyV1Schema,
  workflowRuntimeReadinessV1Schema,
  workflowRuntimeStoredPolicyV1Schema,
} from "@/core/project-workflows/runtime-policy";

import runInvalidFixture from "../../../fixtures/workflows/workflow-run-invalid-v1.json";
import runtimePolicyFixture from "../../../fixtures/workflows/workflow-runtime-policy-v1.json";

const clonePolicy = (): Record<string, unknown> =>
  structuredClone(runtimePolicyFixture.policy) as Record<string, unknown>;

const storedPolicy = () => ({
  ...runtimePolicyFixture.stored_identity,
  payload_checksum: runtimePolicyFixture.payload_checksum,
  value: structuredClone(runtimePolicyFixture.policy),
});

const effectivePolicy = () => ({
  ...runtimePolicyFixture.effective_identity,
  payload_checksum: runtimePolicyFixture.payload_checksum,
});

const adminProjection = () => ({
  ...structuredClone(runtimePolicyFixture.admin_projection),
  stored: storedPolicy(),
  effective: effectivePolicy(),
});

const adminProjectionForMode = (
  enabled: boolean,
  admissionEnabled: boolean,
) => {
  const value = workflowRuntimePolicyV1Schema.parse({
    ...structuredClone(runtimePolicyFixture.policy),
    enabled,
    admission_enabled: admissionEnabled,
  });
  const checksum = workflowRuntimePolicyChecksum(value);
  return {
    ...adminProjection(),
    stored: {
      ...storedPolicy(),
      payload_checksum: checksum,
      value,
    },
    effective: {
      ...effectivePolicy(),
      payload_checksum: checksum,
    },
  };
};

describe("workflow_runtime policy v1", () => {
  it.each(runInvalidFixture.uuid_values)(
    "rejects $id in stored and effective policy DTOs",
    ({ value: uuid }) => {
      expect(
        workflowRuntimeStoredPolicyV1Schema.safeParse({
          ...storedPolicy(),
          policy_version_id: uuid,
        }).success,
      ).toBe(false);
      expect(
        workflowRuntimeEffectivePolicyV1Schema.safeParse({
          ...effectivePolicy(),
          policy_version_id: uuid,
        }).success,
      ).toBe(false);
    },
  );

  it("parses the shared secret-free golden and produces the Python checksum", () => {
    const policy = workflowRuntimePolicyV1Schema.parse(
      runtimePolicyFixture.policy,
    );

    expect(workflowRuntimePolicyChecksum(policy)).toBe(
      runtimePolicyFixture.payload_checksum,
    );
    expect(
      serializeWorkflowRuntimePolicyChecksumInput({
        ...policy,
        catalog: policy.catalog,
      }),
    ).toBe(serializeWorkflowRuntimePolicyChecksumInput(policy));
  });

  it("keeps the first-batch type/version catalog closed and canonical", () => {
    const futureVersion = clonePolicy();
    (
      (
        (futureVersion.catalog as Record<string, unknown>)
          .allowed_type_versions as Array<Record<string, unknown>>
      )[0]!.versions as number[]
    )[0] = 2;
    expect(workflowRuntimePolicyV1Schema.safeParse(futureVersion).success).toBe(
      false,
    );

    const outOfOrder = clonePolicy();
    const entries = (outOfOrder.catalog as Record<string, unknown>)
      .allowed_type_versions as unknown[];
    [entries[0], entries[1]] = [entries[1], entries[0]];
    expect(workflowRuntimePolicyV1Schema.safeParse(outOfOrder).success).toBe(
      false,
    );
  });

  it("keeps input, preview, and aggregate-group policy limits within their public contracts", () => {
    expect(WORKFLOW_RUNTIME_MAX_INPUT_BYTES).toBe(
      WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES,
    );
    expect(WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES).toBe(65_536);
    expect(WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS).toBe(254);

    const atHardCap = clonePolicy();
    Object.assign(atHardCap.graph_limits as Record<string, unknown>, {
      max_aggregate_groups: WORKFLOW_RUNTIME_MAX_AGGREGATE_GROUPS,
    });
    Object.assign(atHardCap.execution_limits as Record<string, unknown>, {
      max_input_bytes: WORKFLOW_RUNTIME_MAX_INPUT_BYTES,
      max_event_preview_bytes: WORKFLOW_RUNTIME_MAX_EVENT_PREVIEW_BYTES,
    });
    expect(workflowRuntimePolicyV1Schema.safeParse(atHardCap).success).toBe(
      true,
    );

    for (const [section, overrides] of [
      [
        "graph_limits",
        runtimePolicyFixture.audit_negative_cases.graph_limit_overrides,
      ],
      [
        "execution_limits",
        runtimePolicyFixture.audit_negative_cases.execution_limit_overrides,
      ],
    ] as const) {
      for (const [field, value] of Object.entries(overrides)) {
        const aboveHardCap = clonePolicy();
        (aboveHardCap[section] as Record<string, unknown>)[field] = value;
        expect(
          workflowRuntimePolicyV1Schema.safeParse(aboveHardCap).success,
        ).toBe(false);
      }
    }

    // The System Admin may narrow each limit below the immutable contract cap.
    expect(workflowRuntimePolicyV1Schema.safeParse(clonePolicy()).success).toBe(
      true,
    );
  });

  it("rejects config, environment, import-path, locator, secret, and unknown fields", () => {
    const cases: Array<[string[], string, unknown]> = [
      [[], "config_file", "config.yaml"],
      [[], "environment", { WORKFLOW_ENABLED: "true" }],
      [["code"], "import_path", "package.module:Executor"],
      [["code"], "provider_locator", "https://provisioner.internal"],
      [["code"], "secret", "plaintext"],
      [["http"], "proxy_url", "https://proxy.internal"],
      [["http"], "credential_id", "system-credential"],
      [["http"], "api_token", "plaintext"],
    ];

    for (const [path, field, value] of cases) {
      const policy = clonePolicy();
      let target = policy;
      for (const segment of path) {
        target = target[segment] as Record<string, unknown>;
      }
      target[field] = value;
      expect(workflowRuntimePolicyV1Schema.safeParse(policy).success).toBe(
        false,
      );
    }
  });

  it("does not coerce hardening flags and cannot widen disabled authority", () => {
    const admission = clonePolicy();
    admission.admission_enabled = true;
    expect(workflowRuntimePolicyV1Schema.safeParse(admission).success).toBe(
      false,
    );

    const hardening = clonePolicy();
    (
      (hardening.code as Record<string, unknown>).hard_limits as Record<
        string,
        unknown
      >
    ).allow_mounts = 0;
    expect(workflowRuntimePolicyV1Schema.safeParse(hardening).success).toBe(
      false,
    );

    const future = clonePolicy();
    (future.future as Record<string, unknown>).agent_enabled = true;
    expect(workflowRuntimePolicyV1Schema.safeParse(future).success).toBe(false);
  });

  it("admits Code only with one exact static deny-all profile", () => {
    const enabled = clonePolicy();
    enabled.code = {
      ...(enabled.code as Record<string, unknown>),
      enabled: true,
      provider_adapter_key: "aio_isolated_code_v1",
      execution_profile_id: "python312-isolated-v1",
      image_digest: `sha256:${"a".repeat(64)}`,
      isolation_profile: "workflow-python-code-v1",
    };
    (enabled.execution_limits as Record<string, unknown>).max_code_activations =
      10;
    expect(workflowRuntimePolicyV1Schema.safeParse(enabled).success).toBe(true);

    for (const provider_adapter_key of [
      "deerflow.community.aio_sandbox:AioSandboxProvider",
      "https://provisioner.internal",
      "local",
      "custom_adapter",
    ]) {
      const invalid = structuredClone(enabled);
      (invalid.code as Record<string, unknown>).provider_adapter_key =
        provider_adapter_key;
      expect(workflowRuntimePolicyV1Schema.safeParse(invalid).success).toBe(
        false,
      );
    }

    const partial = clonePolicy();
    (partial.code as Record<string, unknown>).provider_adapter_key =
      "aio_isolated_code_v1";
    expect(workflowRuntimePolicyV1Schema.safeParse(partial).success).toBe(
      false,
    );
  });

  it("admits HTTP only through fixed HTTPS egress and bounded response policy", () => {
    const enabled = clonePolicy();
    enabled.http = {
      ...(enabled.http as Record<string, unknown>),
      enabled: true,
      write_enabled: true,
      egress_profile_id: "controlled-egress-v1",
      egress_profile_digest: "b".repeat(64),
      injection_profiles: [
        {
          id: "api-key-v1",
          location: "header",
          scheme: "api_key",
          target_header: "x-api-key",
          credential_payload_contract: "api_key_v1",
        },
      ],
      endpoint_policies: [
        {
          id: "example-write-api",
          origin: "https://api.example.com:443",
          allowed_methods: ["POST"],
          injection_profile_ids: ["api-key-v1"],
          write_idempotency: "server_derived_key",
          idempotency_header: "idempotency-key",
        },
      ],
    };
    (enabled.execution_limits as Record<string, unknown>).max_http_calls = 10;
    expect(workflowRuntimePolicyV1Schema.safeParse(enabled).success).toBe(true);

    for (const origin of [
      "http://api.example.com",
      "https://api.example.com/v1",
      "https://user:password@api.example.com",
      "https://api.example.com:0",
      "https://api.example.com:65536",
      ...runtimePolicyFixture.audit_negative_cases.rejected_origins,
    ]) {
      const invalid = structuredClone(enabled);
      (
        (invalid.http as Record<string, unknown>).endpoint_policies as Array<
          Record<string, unknown>
        >
      )[0]!.origin = origin;
      expect(workflowRuntimePolicyV1Schema.safeParse(invalid).success).toBe(
        false,
      );
    }

    for (const origin of [
      "https://api.example.com:1",
      "https://api.example.com:65535",
    ]) {
      const valid = structuredClone(enabled);
      (
        (valid.http as Record<string, unknown>).endpoint_policies as Array<
          Record<string, unknown>
        >
      )[0]!.origin = origin;
      expect(workflowRuntimePolicyV1Schema.safeParse(valid).success).toBe(true);
    }

    for (const origin of runtimePolicyFixture.audit_negative_cases
      .allowed_origins) {
      const valid = structuredClone(enabled);
      (
        (valid.http as Record<string, unknown>).endpoint_policies as Array<
          Record<string, unknown>
        >
      )[0]!.origin = origin;
      expect(workflowRuntimePolicyV1Schema.safeParse(valid).success).toBe(true);
    }

    for (const idempotency_header of [null, "authorization", "x-api-key"]) {
      const invalid = structuredClone(enabled);
      (
        (invalid.http as Record<string, unknown>).endpoint_policies as Array<
          Record<string, unknown>
        >
      )[0]!.idempotency_header = idempotency_header;
      expect(workflowRuntimePolicyV1Schema.safeParse(invalid).success).toBe(
        false,
      );
    }

    for (const [field, value] of [
      ["max_wire_response_bytes", 2_097_153],
      ["max_decompressed_response_bytes", 2_097_153],
      ["max_json_depth", 65],
      ...Object.entries(
        runtimePolicyFixture.audit_negative_cases.transport_limit_overrides,
      ),
    ] as const) {
      const invalid = clonePolicy();
      (
        (invalid.http as Record<string, unknown>).transport as Record<
          string,
          unknown
        >
      )[field] = value;
      expect(workflowRuntimePolicyV1Schema.safeParse(invalid).success).toBe(
        false,
      );
    }

    for (const header of runtimePolicyFixture.audit_negative_cases
      .transport_controlled_headers) {
      const invalidInjection = structuredClone(enabled);
      (
        (invalidInjection.http as Record<string, unknown>)
          .injection_profiles as Array<Record<string, unknown>>
      )[0]!.target_header = header;
      expect(
        workflowRuntimePolicyV1Schema.safeParse(invalidInjection).success,
      ).toBe(false);
    }

    for (const [scheme, credential_payload_contract] of [
      ["bearer", "bearer_token_v1"],
      ["basic", "basic_auth_v1"],
    ] as const) {
      const validCredentialInjection = structuredClone(enabled);
      const http = validCredentialInjection.http as Record<string, unknown>;
      http.injection_profiles = [
        {
          id: `${scheme}-v1`,
          location: "header",
          scheme,
          target_header: "authorization",
          credential_payload_contract,
        },
      ];
      (
        http.endpoint_policies as Array<Record<string, unknown>>
      )[0]!.injection_profile_ids = [`${scheme}-v1`];
      expect(
        workflowRuntimePolicyV1Schema.safeParse(validCredentialInjection)
          .success,
      ).toBe(true);
    }

    const invalidApiKeyAuthorization = structuredClone(enabled);
    (
      (invalidApiKeyAuthorization.http as Record<string, unknown>)
        .injection_profiles as Array<Record<string, unknown>>
    )[0]!.target_header = "authorization";
    expect(
      workflowRuntimePolicyV1Schema.safeParse(invalidApiKeyAuthorization)
        .success,
    ).toBe(false);

    for (const header of [
      ...runtimePolicyFixture.audit_negative_cases.transport_controlled_headers,
      ...runtimePolicyFixture.audit_negative_cases
        .credential_controlled_headers,
    ]) {
      const invalidIdempotency = structuredClone(enabled);
      (
        (invalidIdempotency.http as Record<string, unknown>)
          .endpoint_policies as Array<Record<string, unknown>>
      )[0]!.idempotency_header = header;
      expect(
        workflowRuntimePolicyV1Schema.safeParse(invalidIdempotency).success,
      ).toBe(false);
    }
  });
});

describe("workflow_runtime admin transport", () => {
  it("accepts only the typed CAS update request", () => {
    const request = workflowRuntimePolicyUpdateRequestV1Schema.parse({
      ...runtimePolicyFixture.update_request,
      value: runtimePolicyFixture.policy,
    });
    expect(request.expected_revision).toBe(6);

    for (const [field, value] of [
      ["section", "workflow_runtime"],
      ["provider_locator", "https://provisioner.internal"],
      ["credential", "plaintext"],
    ] as const) {
      expect(
        workflowRuntimePolicyUpdateRequestV1Schema.safeParse({
          ...request,
          [field]: value,
        }).success,
      ).toBe(false);
    }
  });

  it("keeps CAS and stored revisions within the JavaScript safe range", () => {
    const { max_safe_integer, unsafe_integer } =
      runtimePolicyFixture.audit_negative_cases;

    expect(
      workflowRuntimePolicyUpdateRequestV1Schema.safeParse({
        expected_revision: max_safe_integer,
        value: runtimePolicyFixture.policy,
      }).success,
    ).toBe(true);
    expect(
      workflowRuntimeStoredPolicyV1Schema.safeParse({
        ...storedPolicy(),
        revision: max_safe_integer,
      }).success,
    ).toBe(true);

    expect(
      workflowRuntimePolicyUpdateRequestV1Schema.safeParse({
        expected_revision: unsafe_integer,
        value: runtimePolicyFixture.policy,
      }).success,
    ).toBe(false);
    expect(
      workflowRuntimeStoredPolicyV1Schema.safeParse({
        ...storedPolicy(),
        revision: unsafe_integer,
      }).success,
    ).toBe(false);
  });

  it("binds stored identity to the exact schema and checksum", () => {
    expect(
      workflowRuntimeStoredPolicyV1Schema.parse(storedPolicy()).revision,
    ).toBe(7);
    expect(
      workflowRuntimeStoredPolicyV1Schema.safeParse({
        ...storedPolicy(),
        payload_checksum: "f".repeat(64),
      }).success,
    ).toBe(false);
    expect(
      workflowRuntimeStoredPolicyV1Schema.safeParse({
        ...storedPolicy(),
        schema_version: 2,
      }).success,
    ).toBe(false);
  });

  it("accepts only the four frozen readiness states", () => {
    const accepted = [
      {
        status: "ready",
        code: "WORKFLOW_RUNTIME_READY",
        admission_ready: true,
      },
      {
        status: "ready",
        code: "WORKFLOW_RUNTIME_DISABLED",
        admission_ready: false,
      },
      {
        status: "pending",
        code: "WORKFLOW_RUNTIME_PENDING",
        admission_ready: false,
      },
      {
        status: "unavailable",
        code: "WORKFLOW_RUNTIME_UNAVAILABLE",
        admission_ready: false,
      },
    ];
    for (const readiness of accepted) {
      expect(
        workflowRuntimeReadinessV1Schema.safeParse(readiness).success,
      ).toBe(true);
    }

    for (const readiness of [
      {
        status: "ready",
        code: "WORKFLOW_RUNTIME_PENDING",
        admission_ready: false,
      },
      {
        status: "pending",
        code: "WORKFLOW_RUNTIME_READY",
        admission_ready: false,
      },
      {
        status: "ready",
        code: "WORKFLOW_RUNTIME_DISABLED",
        admission_ready: true,
      },
    ]) {
      expect(
        workflowRuntimeReadinessV1Schema.safeParse(readiness).success,
      ).toBe(false);
    }
  });

  it("freezes effective identity, effect scope, and the complete projection truth table", () => {
    const projection =
      workflowRuntimeAdminPolicyV1Schema.parse(adminProjection());
    expect(projection.effect_scope).toBe(WORKFLOW_RUNTIME_POLICY_EFFECT_SCOPE);

    expect(
      workflowRuntimeAdminPolicyV1Schema.safeParse({
        ...adminProjection(),
        effect_scope: "new_requests_and_runs",
      }).success,
    ).toBe(false);

    const cases = {
      disabledReady: {
        effective: "exact",
        pending_roles: [],
        readiness: {
          status: "ready",
          code: "WORKFLOW_RUNTIME_DISABLED",
          admission_ready: false,
        },
      },
      builderReady: {
        effective: "exact",
        pending_roles: [],
        readiness: {
          status: "ready",
          code: "WORKFLOW_RUNTIME_READY",
          admission_ready: false,
        },
      },
      admissionReady: {
        effective: "exact",
        pending_roles: [],
        readiness: {
          status: "ready",
          code: "WORKFLOW_RUNTIME_READY",
          admission_ready: true,
        },
      },
      pendingWorker: {
        effective: "exact",
        pending_roles: ["worker"],
        readiness: {
          status: "pending",
          code: "WORKFLOW_RUNTIME_PENDING",
          admission_ready: false,
        },
      },
      pendingGateway: {
        effective: "exact",
        pending_roles: ["gateway"],
        readiness: {
          status: "pending",
          code: "WORKFLOW_RUNTIME_PENDING",
          admission_ready: false,
        },
      },
      pendingScheduler: {
        effective: "exact",
        pending_roles: ["scheduler"],
        readiness: {
          status: "pending",
          code: "WORKFLOW_RUNTIME_PENDING",
          admission_ready: false,
        },
      },
      pendingMultiple: {
        effective: "exact",
        pending_roles: ["gateway", "worker"],
        readiness: {
          status: "pending",
          code: "WORKFLOW_RUNTIME_PENDING",
          admission_ready: false,
        },
      },
      unavailable: {
        effective: null,
        pending_roles: ["gateway"],
        readiness: {
          status: "unavailable",
          code: "WORKFLOW_RUNTIME_UNAVAILABLE",
          admission_ready: false,
        },
      },
    } as const;
    const modes = [
      {
        enabled: false,
        admissionEnabled: false,
        valid: new Set(["disabledReady", "unavailable"]),
      },
      {
        enabled: true,
        admissionEnabled: false,
        valid: new Set(["builderReady", "unavailable"]),
      },
      {
        enabled: true,
        admissionEnabled: true,
        valid: new Set(["admissionReady", "pendingWorker", "unavailable"]),
      },
    ];

    for (const mode of modes) {
      const base = adminProjectionForMode(mode.enabled, mode.admissionEnabled);
      for (const [name, testCase] of Object.entries(cases)) {
        const candidate = {
          ...base,
          ...testCase,
          effective: testCase.effective === "exact" ? base.effective : null,
        };
        expect(
          workflowRuntimeAdminPolicyV1Schema.safeParse(candidate).success,
        ).toBe(mode.valid.has(name));
      }
    }
  });

  it("rejects a ready projection whose stored and effective identities differ", () => {
    expect(
      workflowRuntimeAdminPolicyV1Schema.safeParse({
        ...adminProjection(),
        effective: {
          ...effectivePolicy(),
          revision: 6,
        },
      }).success,
    ).toBe(false);
  });

  it("freezes unavailable projection to the fail-closed gateway shape", () => {
    const unavailable = {
      ...adminProjection(),
      effective: null,
      pending_roles: ["gateway"],
      readiness: {
        status: "unavailable",
        code: "WORKFLOW_RUNTIME_UNAVAILABLE",
        admission_ready: false,
      },
    };

    expect(
      workflowRuntimeAdminPolicyV1Schema.safeParse(unavailable).success,
    ).toBe(true);
    for (const contradiction of [
      { effective: effectivePolicy(), pending_roles: ["gateway"] },
      { effective: null, pending_roles: [] },
      { effective: null, pending_roles: ["worker"] },
      { effective: null, pending_roles: ["gateway", "worker"] },
    ]) {
      expect(
        workflowRuntimeAdminPolicyV1Schema.safeParse({
          ...unavailable,
          ...contradiction,
        }).success,
      ).toBe(false);
    }
  });

  it("accepts builder-only effective readiness without admission", () => {
    const value = workflowRuntimePolicyV1Schema.parse({
      ...runtimePolicyFixture.policy,
      enabled: true,
      admission_enabled: false,
    });
    const checksum = workflowRuntimePolicyChecksum(value);
    const stored = {
      ...storedPolicy(),
      payload_checksum: checksum,
      value,
    };
    const projection = {
      ...adminProjection(),
      stored,
      effective: {
        ...effectivePolicy(),
        payload_checksum: checksum,
      },
      readiness: {
        status: "ready",
        code: "WORKFLOW_RUNTIME_READY",
        admission_ready: false,
      },
    };

    expect(
      workflowRuntimeAdminPolicyV1Schema.safeParse(projection).success,
    ).toBe(true);
  });

  it("adds only catalog_revision to the strict update response", () => {
    const response = {
      ...adminProjection(),
      catalog_revision: runtimePolicyFixture.catalog_revision,
    };
    expect(
      workflowRuntimePolicyUpdateResponseV1Schema.parse(response)
        .catalog_revision,
    ).toBe(12);
    expect(
      workflowRuntimePolicyUpdateResponseV1Schema.safeParse({
        ...response,
        system_credential_id: "must-not-leak",
      }).success,
    ).toBe(false);
    expect(
      workflowRuntimePolicyUpdateResponseV1Schema.safeParse({
        ...response,
        catalog_revision:
          runtimePolicyFixture.audit_negative_cases.unsafe_integer,
      }).success,
    ).toBe(false);
  });
});
