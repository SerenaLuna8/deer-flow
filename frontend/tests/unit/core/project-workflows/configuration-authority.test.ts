import { describe, expect, it } from "@rstest/core";

import {
  workflowRuntimePolicyChecksum,
  workflowRuntimePolicyV1Schema,
} from "@/core/project-workflows/runtime-policy";

import runtimePolicyFixture from "../../../fixtures/workflows/workflow-runtime-policy-v1.json";

const WORKFLOW_PRODUCT_ENVIRONMENT_NAMES = [
  "WORKFLOW_RUNTIME",
  "ACT_WEAVE_WORKFLOW_RUNTIME",
  "DEER_FLOW_WORKFLOW_RUNTIME",
  "WORKFLOW_CODE_PROFILE",
  "WORKFLOW_HTTP_RETRY",
  "NEXT_PUBLIC_WORKFLOW_RETENTION",
] as const;

describe("Workflow runtime configuration authority", () => {
  it("does not read browser, build, or server ambient Workflow values", () => {
    const baseline = workflowRuntimePolicyV1Schema.parse(
      runtimePolicyFixture.policy,
    );
    const previous = new Map(
      WORKFLOW_PRODUCT_ENVIRONMENT_NAMES.map((name) => [
        name,
        process.env[name],
      ]),
    );

    try {
      for (const name of WORKFLOW_PRODUCT_ENVIRONMENT_NAMES) {
        process.env[name] = '{"enabled":true,"max_retry_attempts":99}';
      }

      const loaded = workflowRuntimePolicyV1Schema.parse(
        runtimePolicyFixture.policy,
      );
      expect(loaded).toEqual(baseline);
      expect(workflowRuntimePolicyChecksum(loaded)).toBe(
        runtimePolicyFixture.payload_checksum,
      );
    } finally {
      for (const [name, value] of previous) {
        if (value === undefined) {
          delete process.env[name];
        } else {
          process.env[name] = value;
        }
      }
    }
  });
});
