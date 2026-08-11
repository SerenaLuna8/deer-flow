import { describe, expect, it } from "@rstest/core";

import {
  CAPABILITIES,
  WORKFLOW_CAPABILITIES,
  capabilitySchema,
} from "@/core/projects/types";

const EXPECTED_WORKFLOW_CAPABILITIES = [
  "workflow.read",
  "workflow.edit",
  "workflow.publish",
  "workflow.execute",
  "workflow.code.use",
  "workflow.http.use",
  "workflow.http.write",
  "workflow.credential.grant",
  "workflow.run.read_own",
  "workflow.run.cancel_own",
] as const;

describe("project Workflow capabilities", () => {
  it("keeps the closed frontend enum in backend contract order", () => {
    expect(WORKFLOW_CAPABILITIES).toEqual(EXPECTED_WORKFLOW_CAPABILITIES);
    expect(
      CAPABILITIES.filter((capability) => capability.startsWith("workflow.")),
    ).toEqual(EXPECTED_WORKFLOW_CAPABILITIES);
    expect(new Set(CAPABILITIES).size).toBe(CAPABILITIES.length);
  });

  it("accepts every frozen capability and rejects future or misspelled authority", () => {
    for (const capability of EXPECTED_WORKFLOW_CAPABILITIES) {
      expect(capabilitySchema.parse(capability)).toBe(capability);
    }
    expect(() => capabilitySchema.parse("workflow.run.retry_own")).toThrow();
    expect(() => capabilitySchema.parse("workflow.http.admin")).toThrow();
    expect(() => capabilitySchema.parse("workflow.wait.respond_own")).toThrow();
  });
});
