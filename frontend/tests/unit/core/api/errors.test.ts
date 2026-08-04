import { describe, expect, test } from "@rstest/core";

import { GatewayApiError, throwGatewayApiError } from "@/core/api/errors";

describe("Gateway API errors", () => {
  test("retains safe validation fields from the Gateway envelope", async () => {
    const response = new Response(
      JSON.stringify({
        detail: {
          code: "CHANNEL_INSTANCE_INVALID",
          message: "Channel credentials are invalid.",
          request_id: "req-safe",
          fields: ["credentials"],
        },
      }),
      {
        status: 422,
        headers: { "Content-Type": "application/json" },
      },
    );

    try {
      await throwGatewayApiError(response, "fallback");
      throw new Error("expected GatewayApiError");
    } catch (error) {
      expect(error).toBeInstanceOf(GatewayApiError);
      expect((error as GatewayApiError).fields).toEqual(["credentials"]);
    }
  });

  test("drops malformed validation fields", async () => {
    const response = new Response(
      JSON.stringify({
        detail: {
          code: "CHANNEL_INSTANCE_INVALID",
          message: "Channel configuration is invalid.",
          fields: ["credentials", 7],
        },
      }),
      { status: 422 },
    );

    await expect(throwGatewayApiError(response, "fallback")).rejects.toMatchObject(
      { fields: [] },
    );
  });
});
