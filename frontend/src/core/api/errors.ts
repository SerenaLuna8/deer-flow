/**
 * Throw an Error from a failed Gateway REST response.
 *
 * Parses the FastAPI error envelope (`{ detail: string | { code, message } }`)
 * and falls back to the caller-provided message when the body is missing or
 * not that shape.
 * Shared by the domain API modules (channels, scheduled tasks) so the envelope
 * format is interpreted in exactly one place.
 */
export class GatewayApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "GatewayApiError";
    this.status = status;
    this.code = code;
  }
}

export async function throwGatewayApiError(
  response: Response,
  fallback: string,
): Promise<never> {
  const body = (await response.json().catch(() => ({}))) as {
    detail?: unknown;
  };
  if (typeof body.detail === "string") {
    throw new GatewayApiError(response.status, null, body.detail);
  }
  if (
    typeof body.detail === "object" &&
    body.detail !== null &&
    "code" in body.detail
  ) {
    const detail = body.detail as { code?: unknown; message?: unknown };
    throw new GatewayApiError(
      response.status,
      typeof detail.code === "string" ? detail.code : null,
      typeof detail.message === "string" ? detail.message : fallback,
    );
  }
  throw new GatewayApiError(response.status, null, fallback);
}
