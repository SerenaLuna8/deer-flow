"use client";

import { MailIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useMyProjectInvitations } from "@/core/projects/hooks";

import { projectErrorMessage } from "./project-view-model";

export function WorkspaceInvitations({ userId }: { userId: string }) {
  const invitations = useMyProjectInvitations(userId);

  return (
    <section
      aria-labelledby="workspace-invitations-title"
      aria-label="待接受邀请"
      className="border-border/70 bg-card mb-8 rounded-2xl border p-5"
    >
      <div className="mb-4 flex items-center gap-2">
        <MailIcon aria-hidden className="text-primary size-5" />
        <h2 id="workspace-invitations-title" className="font-semibold">
          待接受邀请
        </h2>
      </div>
      {invitations.isLoading ? (
        <Skeleton className="h-16 rounded-xl" />
      ) : invitations.error ? (
        <p role="alert" className="text-muted-foreground text-sm">
          {projectErrorMessage(invitations.error)}
        </p>
      ) : invitations.data?.length ? (
        <ul className="grid gap-3 sm:grid-cols-2">
          {invitations.data.map((invitation) => (
            <li
              key={invitation.id}
              className="bg-muted/40 flex min-w-0 items-center justify-between gap-3 rounded-xl p-4"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">
                  {invitation.invited_email}
                </p>
                <p className="text-muted-foreground mt-1 text-xs">
                  项目 {invitation.project_id}
                </p>
              </div>
              <Badge variant="secondary">{invitation.role}</Badge>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground text-sm">暂无待接受邀请。</p>
      )}
    </section>
  );
}
