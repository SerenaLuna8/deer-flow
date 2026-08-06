import { z } from "zod";

import type { ProjectPrivateWorkScope } from "../private-work/types";

import { throwGatewayApiError } from "./errors";
import { fetch } from "./fetcher";

export const feedbackDataSchema = z
  .object({
    feedback_id: z.string().min(1),
    run_id: z.string().min(1),
    thread_id: z.string().min(1),
    message_id: z.string().min(1).nullable(),
    rating: z.union([z.literal(1), z.literal(-1)]),
    comment: z.string().nullable(),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

export type FeedbackData = z.infer<typeof feedbackDataSchema>;
export type FeedbackRating = FeedbackData["rating"];

type FeedbackAccess = Pick<ProjectPrivateWorkScope, "apiBaseURL">;

function feedbackURL(
  privateWork: FeedbackAccess,
  threadId: string,
  runId: string,
) {
  return `${privateWork.apiBaseURL}/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/feedback`;
}

export async function getFeedback(
  privateWork: FeedbackAccess,
  threadId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<FeedbackData | null> {
  const response = await fetch(feedbackURL(privateWork, threadId, runId), {
    signal,
  });
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to load feedback");
  }
  const value: unknown = await response.json();
  return value === null ? null : feedbackDataSchema.parse(value);
}

export async function upsertFeedback(
  privateWork: FeedbackAccess,
  threadId: string,
  runId: string,
  rating: FeedbackRating,
  messageId?: string | null,
  comment?: string | null,
  signal?: AbortSignal,
): Promise<FeedbackData> {
  const response = await fetch(feedbackURL(privateWork, threadId, runId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      rating,
      message_id: messageId ?? null,
      comment: comment ?? null,
    }),
    signal,
  });
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to submit feedback");
  }
  return feedbackDataSchema.parse(await response.json());
}

export async function deleteFeedback(
  privateWork: FeedbackAccess,
  threadId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(feedbackURL(privateWork, threadId, runId), {
    method: "DELETE",
    signal,
  });
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to delete feedback");
  }
}
