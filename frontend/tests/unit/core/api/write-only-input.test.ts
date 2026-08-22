import { describe, expect, test } from "@rstest/core";

import { consumeWriteOnlyInput } from "@/core/api/write-only-input";

describe("write-only browser input", () => {
  test("clears the control synchronously before the request receives its value", async () => {
    let control = "temporary-secret";
    const submitted = consumeWriteOnlyInput(control, () => {
      control = "";
    });

    expect(control).toBe("");
    expect(submitted).toBe("temporary-secret");

    await Promise.reject(new Error("request failed")).catch(() => undefined);
    expect(control).toBe("");
  });
});
