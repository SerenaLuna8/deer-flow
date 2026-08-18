"use client";

import {
  BellIcon,
  CheckCircle2Icon,
  Clock3Icon,
  MailCheckIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { useI18n } from "@/core/i18n/hooks";
import {
  useAcceptSystemNotification,
  useMarkAllSystemNotificationsRead,
  useSystemNotifications,
} from "@/core/system-notifications/hooks";
import type { SystemNotification } from "@/core/system-notifications/types";

function formatNotificationTime(value: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function notificationErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function ProjectInvitationNotification({
  notification,
  accepting,
  acceptDisabled,
  locallyAccepted,
  onAccept,
}: {
  notification: SystemNotification;
  accepting: boolean;
  acceptDisabled: boolean;
  locallyAccepted: boolean;
  onAccept: () => void;
}) {
  const { locale, t } = useI18n();
  const copy = t.projectWorkspace.notifications;
  const effectiveStatus = locallyAccepted ? "redeemed" : notification.status;
  const actionable = effectiveStatus === "pending";
  return (
    <li
      className="border-border/70 bg-card rounded-xl border p-4 shadow-xs"
      data-testid={`notification-${notification.id}`}
    >
      <div className="flex items-start gap-3">
        <span className="bg-primary/10 text-primary mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-full">
          <MailCheckIcon aria-hidden className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="font-medium">{copy.invitationTitle}</p>
              <p className="mt-1 text-sm">
                {copy.invitedBy(
                  notification.actor.email,
                  notification.project.display_name,
                )}
              </p>
            </div>
            <Badge variant={actionable ? "default" : "secondary"}>
              {copy.statuses[effectiveStatus]}
            </Badge>
          </div>
          <div className="text-muted-foreground mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <span>{copy.role(copy.roles[notification.role])}</span>
            <span className="inline-flex items-center gap-1">
              <Clock3Icon aria-hidden className="size-3" />
              {formatNotificationTime(notification.created_at, locale)}
            </span>
          </div>
          {actionable ? (
            <Button
              type="button"
              size="sm"
              className="mt-4 w-full"
              disabled={acceptDisabled}
              onClick={onAccept}
            >
              <CheckCircle2Icon aria-hidden className="size-4" />
              {accepting ? copy.accepting : copy.accept}
            </Button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export function SystemNotificationCenter({ userId }: { userId: string }) {
  const { t } = useI18n();
  const copy = t.projectWorkspace.notifications;
  const [open, setOpen] = useState(false);
  const [locallyAcceptedIds, setLocallyAcceptedIds] = useState(
    () => new Set<string>(),
  );
  const readAllAttemptedRef = useRef(false);
  const acceptInFlightRef = useRef(false);
  const notifications = useSystemNotifications(userId);
  const markAllRead = useMarkAllSystemNotificationsRead(userId);
  const accept = useAcceptSystemNotification(userId);
  const items = useMemo(
    () => notifications.data?.pages.flatMap((page) => page.items) ?? [],
    [notifications.data?.pages],
  );
  const unreadCount = notifications.data?.pages[0]?.unread_count ?? 0;
  const acceptingId = accept.isPending
    ? accept.variables?.notificationId
    : undefined;

  useEffect(() => {
    if (!open) {
      readAllAttemptedRef.current = false;
      return;
    }
    if (
      unreadCount > 0 &&
      !readAllAttemptedRef.current &&
      !markAllRead.isPending
    ) {
      readAllAttemptedRef.current = true;
      markAllRead.mutate();
    }
  }, [markAllRead, open, unreadCount]);

  useEffect(() => {
    readAllAttemptedRef.current = false;
    acceptInFlightRef.current = false;
    setLocallyAcceptedIds(new Set());
  }, [userId]);

  useEffect(() => {
    const acceptedId = accept.variables?.notificationId;
    if (!accept.isSuccess || !acceptedId) return;
    setLocallyAcceptedIds((current) => {
      const next = new Set(current);
      next.add(acceptedId);
      return next;
    });
    toast.success(copy.joined);
  }, [accept.isSuccess, accept.variables?.notificationId, copy.joined]);

  useEffect(() => {
    if (!accept.isPending) acceptInFlightRef.current = false;
  }, [accept.isPending]);

  const handleAccept = (notification: SystemNotification) => {
    if (acceptInFlightRef.current || accept.isPending) return;
    acceptInFlightRef.current = true;
    accept.mutate({
      notificationId: notification.id,
      version: notification.version,
    });
  };

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="relative"
          aria-label={
            unreadCount > 0 ? copy.unreadTrigger(unreadCount) : copy.trigger
          }
        >
          <BellIcon className="size-4" />
          {unreadCount > 0 ? (
            <span
              aria-hidden
              className="bg-destructive text-destructive-foreground absolute -top-0.5 -right-0.5 flex min-w-4 items-center justify-center rounded-full px-1 text-[10px] leading-4 font-semibold"
            >
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          ) : null}
        </Button>
      </SheetTrigger>
      <SheetContent className="w-full overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b px-5 py-4">
          <SheetTitle>{copy.title}</SheetTitle>
          <SheetDescription>{copy.description}</SheetDescription>
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {notifications.isLoading ? (
            <p className="text-muted-foreground text-sm">{copy.loading}</p>
          ) : notifications.error ? (
            <div role="alert" className="text-destructive text-sm">
              <p>
                {notificationErrorMessage(
                  notifications.error,
                  copy.operationFailed,
                )}
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={() => void notifications.refetch()}
              >
                {copy.retry}
              </Button>
            </div>
          ) : items.length === 0 ? (
            <div className="text-muted-foreground flex min-h-52 flex-col items-center justify-center text-center">
              <BellIcon aria-hidden className="mb-3 size-8 opacity-50" />
              <p className="text-sm">{copy.empty}</p>
            </div>
          ) : (
            <ul className="space-y-3">
              {items.map((notification) => (
                <ProjectInvitationNotification
                  key={notification.id}
                  notification={notification}
                  accepting={acceptingId === notification.id}
                  acceptDisabled={
                    accept.isPending || locallyAcceptedIds.has(notification.id)
                  }
                  locallyAccepted={locallyAcceptedIds.has(notification.id)}
                  onAccept={() => handleAccept(notification)}
                />
              ))}
            </ul>
          )}
          {notifications.hasNextPage ? (
            <Button
              type="button"
              variant="outline"
              className="mt-4 w-full"
              disabled={notifications.isFetchingNextPage}
              onClick={() => void notifications.fetchNextPage()}
            >
              {notifications.isFetchingNextPage
                ? copy.loadingMore
                : copy.loadMore}
            </Button>
          ) : null}
          {markAllRead.error ? (
            <p role="status" className="text-muted-foreground mt-3 text-xs">
              {copy.readSyncPending}
            </p>
          ) : null}
          {accept.error ? (
            <p role="alert" className="text-destructive mt-3 text-sm">
              {notificationErrorMessage(accept.error, copy.operationFailed)}
            </p>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
