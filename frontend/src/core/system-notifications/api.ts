import { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";
import { ProjectApiError } from "@/core/projects/api";
import {
  membershipVersionSchema,
  projectIdSchema,
  redeemedProjectInvitationSchema,
  type RedeemedProjectInvitation,
} from "@/core/projects/types";

import {
  markAllSystemNotificationsReadResponseSchema,
  systemNotificationFiltersSchema,
  systemNotificationPageSchema,
  type MarkAllSystemNotificationsReadResponse,
  type SystemNotificationFilters,
  type SystemNotificationPage,
} from "./types";

const notificationErrorCodeSchema = z.enum([
  "PROJECT_NOT_FOUND",
  "PROJECT_FORBIDDEN",
  "PROJECT_OR_MEMBER_NOT_FOUND",
  "PROJECT_MEMBERSHIP_FORBIDDEN",
  "PROJECT_MEMBER_QUOTA_EXCEEDED",
  "PROJECT_QUOTA_STATE_CONFLICT",
  "PROJECT_INVITATION_CONFLICT",
  "PROJECT_INVITATION_INVALID",
  "PROJECT_VALIDATION_FAILED",
  "DATABASE_UNAVAILABLE",
]);

const errorEnvelopeSchema = z
  .object({
    detail: z
      .object({
        code: notificationErrorCodeSchema,
        message: z.string().min(1),
        request_id: z.string().min(1).optional(),
      })
      .strict(),
  })
  .strict();

const SAFE_MESSAGES: Record<
  z.infer<typeof notificationErrorCodeSchema>,
  string
> = {
  PROJECT_NOT_FOUND: "Notification not found",
  PROJECT_FORBIDDEN: "Notification access is not allowed",
  PROJECT_OR_MEMBER_NOT_FOUND: "Project or member not found",
  PROJECT_MEMBERSHIP_FORBIDDEN:
    "Project membership does not allow this operation",
  PROJECT_MEMBER_QUOTA_EXCEEDED: "Project member quota was exceeded",
  PROJECT_QUOTA_STATE_CONFLICT: "Project quota state conflict",
  PROJECT_INVITATION_CONFLICT: "Project invitation conflict",
  PROJECT_INVITATION_INVALID: "Project invitation is invalid",
  PROJECT_VALIDATION_FAILED: "Notification validation failed",
  DATABASE_UNAVAILABLE: "Notification storage unavailable",
};

function notificationUrl(path = ""): string {
  return `${getBackendBaseURL()}/api/notifications${path}`;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ProjectApiError(
      response.status,
      response.ok
        ? "PROJECT_RESPONSE_INVALID"
        : "PROJECT_ERROR_RESPONSE_INVALID",
      response.ok ? "Notification response was invalid" : "Request failed",
    );
  }
}

async function request(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (error instanceof ProjectApiError || isAbortError(error)) throw error;
    if (error instanceof AuthRequiredError) {
      throw new ProjectApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new ProjectApiError(
      0,
      "PROJECT_NETWORK_ERROR",
      "Notification service is unavailable",
    );
  }
}

async function throwResponseError(response: Response): Promise<never> {
  const parsed = errorEnvelopeSchema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new ProjectApiError(
      response.status,
      "PROJECT_ERROR_RESPONSE_INVALID",
      "Notification request failed",
    );
  }
  const { code } = parsed.data.detail;
  throw new ProjectApiError(response.status, code, SAFE_MESSAGES[code]);
}

async function parseResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
): Promise<T> {
  if (!response.ok) await throwResponseError(response);
  const parsed = schema.safeParse(await readJson(response));
  if (!parsed.success) {
    throw new ProjectApiError(
      response.status,
      "PROJECT_RESPONSE_INVALID",
      "Notification response was invalid",
    );
  }
  return parsed.data;
}

function parseNotificationId(value: string): string {
  const parsed = projectIdSchema.safeParse(value);
  if (!parsed.success) {
    throw new ProjectApiError(
      422,
      "PROJECT_VALIDATION_FAILED",
      "Notification validation failed",
    );
  }
  return parsed.data;
}

function postRequest(body: unknown, signal?: AbortSignal): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  };
}

function postWithoutBody(signal?: AbortSignal): RequestInit {
  return { method: "POST", signal };
}

export async function listSystemNotifications(
  filters: SystemNotificationFilters = {},
  signal?: AbortSignal,
): Promise<SystemNotificationPage> {
  const parsedFilters = systemNotificationFiltersSchema.safeParse(filters);
  if (!parsedFilters.success) {
    throw new ProjectApiError(
      422,
      "PROJECT_VALIDATION_FAILED",
      "Notification validation failed",
    );
  }
  const search = new URLSearchParams();
  if (parsedFilters.data.cursor) {
    search.set("cursor", parsedFilters.data.cursor);
  }
  if (parsedFilters.data.limit !== undefined) {
    search.set("limit", String(parsedFilters.data.limit));
  }
  const query = search.toString();
  return parseResponse(
    await request(notificationUrl(query ? `?${query}` : ""), { signal }),
    systemNotificationPageSchema,
  );
}

export async function markAllSystemNotificationsRead(
  signal?: AbortSignal,
): Promise<MarkAllSystemNotificationsReadResponse> {
  return parseResponse(
    await request(notificationUrl("/read-all"), postWithoutBody(signal)),
    markAllSystemNotificationsReadResponseSchema,
  );
}

export async function acceptSystemNotification(
  notificationId: string,
  version: number,
  signal?: AbortSignal,
): Promise<RedeemedProjectInvitation> {
  const id = parseNotificationId(notificationId);
  const body = membershipVersionSchema.safeParse({ version });
  if (!body.success) {
    throw new ProjectApiError(
      422,
      "PROJECT_VALIDATION_FAILED",
      "Notification validation failed",
    );
  }
  return parseResponse(
    await request(
      notificationUrl(`/${encodeURIComponent(id)}/accept`),
      postRequest(body.data, signal),
    ),
    redeemedProjectInvitationSchema,
  );
}
