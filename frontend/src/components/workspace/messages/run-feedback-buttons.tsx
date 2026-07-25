"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ThumbsDownIcon, ThumbsUpIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  deleteFeedback,
  getFeedback,
  upsertFeedback,
  type FeedbackData,
  type FeedbackRating,
} from "@/core/api/feedback";
import { useI18n } from "@/core/i18n/hooks";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
} from "@/core/private-work/types";
import { cn } from "@/lib/utils";

export function RunFeedbackButtons({
  threadId,
  runId,
  messageId,
}: {
  threadId: string;
  runId: string;
  messageId: string;
}) {
  const { t } = useI18n();
  const privateWork = useProjectPrivateWorkScope();
  const queryClient = useQueryClient();
  const queryKey = privateWorkQueryKey(
    privateWork.scope,
    "feedback",
    threadId,
    runId,
  );
  const feedback = useQuery({
    queryKey,
    queryFn: ({ signal }) => getFeedback(privateWork, threadId, runId, signal),
    retry: false,
  });
  const mutation = useMutation<FeedbackData | null, Error, FeedbackRating>({
    mutationKey: privateWorkQueryKey(
      privateWork.scope,
      "feedback",
      "mutation",
      threadId,
      runId,
    ),
    mutationFn: (rating) =>
      runPrivateWorkAbortable(privateWork, async (signal) => {
        if (feedback.data?.rating === rating) {
          await deleteFeedback(privateWork, threadId, runId, signal);
          return null;
        }
        return upsertFeedback(
          privateWork,
          threadId,
          runId,
          rating,
          messageId,
          null,
          signal,
        );
      }),
    onSuccess: (value) => {
      if (isPrivateWorkAccessActive(privateWork)) {
        queryClient.setQueryData(queryKey, value);
      }
    },
    onError: () => {
      toast.error(t.common.feedbackSaveFailed);
    },
  });
  const disabled = feedback.isLoading || mutation.isPending;

  return (
    <div className="flex gap-1" data-testid="run-feedback-actions">
      {(
        [
          [1, t.common.feedbackHelpful, ThumbsUpIcon],
          [-1, t.common.feedbackNotHelpful, ThumbsDownIcon],
        ] as const
      ).map(([rating, label, Icon]) => {
        const selected = feedback.data?.rating === rating;
        return (
          <Button
            key={rating}
            aria-label={label}
            aria-pressed={selected}
            disabled={disabled}
            size="icon-sm"
            type="button"
            variant="ghost"
            className={cn(selected && "text-foreground")}
            onClick={() => mutation.mutate(rating)}
          >
            <Icon className={cn("size-4", selected && "fill-current")} />
          </Button>
        );
      })}
    </div>
  );
}
