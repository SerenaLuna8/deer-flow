"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import {
  useChangeProjectMemberRole,
  useCreateProjectInvitation,
  useLeaveProject,
  useProjectInvitations,
  useProjectMembers,
  useRemoveProjectMember,
  useRevokeProjectInvitation,
} from "@/core/projects/hooks";
import type {
  ProjectInvitation,
  ProjectMembership,
} from "@/core/projects/types";

import { useCurrentProject } from "../project-context";
import { ProjectPageHeader } from "../project-page-header";
import { projectErrorMessage } from "../project-view-model";

import { CreateInvitationDialog } from "./create-invitation-dialog";
import { MemberRoleDialog } from "./member-role-dialog";
import { findSelfMembership } from "./self-membership";

type DestructiveMemberAction =
  | { kind: "remove-member"; member: ProjectMembership }
  | { kind: "leave-project"; membership: ProjectMembership }
  | { kind: "revoke-invitation"; invitation: ProjectInvitation };

function destructiveMemberActionCopy(
  action: DestructiveMemberAction | null,
  labels: Translations["project"]["members"]["confirmations"],
) {
  switch (action?.kind) {
    case "remove-member":
      return {
        title: labels.removeTitle,
        description: labels.removeDescription(action.member.account_email),
        confirm: labels.removeConfirm,
      };
    case "leave-project":
      return {
        title: labels.leaveTitle,
        description: labels.leaveDescription,
        confirm: labels.leaveConfirm,
      };
    case "revoke-invitation":
      return {
        title: labels.revokeTitle,
        description: labels.revokeDescription(action.invitation.invited_email),
        confirm: labels.revokeConfirm,
      };
    default:
      return null;
  }
}

export function ProjectMembersPage({
  embedded = false,
}: {
  embedded?: boolean;
}) {
  const project = useCurrentProject();
  const { user } = useAuth();
  const { t } = useI18n();
  const labels = t.project.members;
  const router = useRouter();
  const canManage = project.capabilities.includes("project.members.manage");
  const members = useProjectMembers(user?.id, project.id);
  const invitations = useProjectInvitations(
    canManage ? user?.id : null,
    project.id,
  );
  const changeRole = useChangeProjectMemberRole(user?.id, project.id);
  const removeMember = useRemoveProjectMember(user?.id, project.id);
  const leave = useLeaveProject(user?.id, project.id);
  const createInvitation = useCreateProjectInvitation(user?.id, project.id);
  const revokeInvitation = useRevokeProjectInvitation(user?.id, project.id);
  const [editingMember, setEditingMember] = useState<ProjectMembership | null>(
    null,
  );
  const [invitationOpen, setInvitationOpen] = useState(false);
  const [destructiveAction, setDestructiveAction] =
    useState<DestructiveMemberAction | null>(null);

  useEffect(() => {
    if (changeRole.isSuccess) setEditingMember(null);
  }, [changeRole.isSuccess]);

  const selfMembership = findSelfMembership(members.data ?? [], user);
  const destructiveActionCopy = destructiveMemberActionCopy(
    destructiveAction,
    labels.confirmations,
  );
  const destructiveActionPending =
    destructiveAction?.kind === "remove-member"
      ? removeMember.isPending
      : destructiveAction?.kind === "leave-project"
        ? leave.isPending
        : destructiveAction?.kind === "revoke-invitation"
          ? revokeInvitation.isPending
          : false;
  const destructiveActionError =
    destructiveAction?.kind === "remove-member"
      ? removeMember.error
      : destructiveAction?.kind === "leave-project"
        ? leave.error
        : destructiveAction?.kind === "revoke-invitation"
          ? revokeInvitation.error
          : null;

  function confirmDestructiveAction() {
    if (!destructiveAction) return;
    switch (destructiveAction.kind) {
      case "remove-member":
        removeMember.mutate(
          {
            membershipId: destructiveAction.member.membership_id,
            version: destructiveAction.member.version,
          },
          { onSuccess: () => setDestructiveAction(null) },
        );
        return;
      case "leave-project":
        leave.mutate(destructiveAction.membership.version, {
          onSuccess: () => {
            setDestructiveAction(null);
            router.replace("/workspace");
          },
        });
        return;
      case "revoke-invitation":
        revokeInvitation.mutate(
          {
            invitationId: destructiveAction.invitation.id,
            version: destructiveAction.invitation.version,
          },
          { onSuccess: () => setDestructiveAction(null) },
        );
    }
  }

  const inviteAction = canManage ? (
    <Button type="button" onClick={() => setInvitationOpen(true)}>
      {labels.inviteMember}
    </Button>
  ) : null;

  const body = (
    <>
      {embedded ? (
        <header className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="max-w-2xl">
            <h2 className="text-2xl font-semibold tracking-tight">
              {labels.title}
            </h2>
            <p className="text-muted-foreground mt-2 leading-6">
              {labels.description}
            </p>
          </div>
          {inviteAction}
        </header>
      ) : (
        <ProjectPageHeader
          eyebrow={labels.managementEyebrow(project.display_name)}
          title={labels.title}
          description={labels.description}
          actions={inviteAction}
        />
      )}

      <section
        aria-labelledby="project-members-title"
        className={embedded ? undefined : "mt-6"}
      >
        <h2 id="project-members-title" className="mb-3 text-lg font-semibold">
          {labels.membersTitle}
        </h2>
        {members.isLoading ? (
          <Skeleton className="h-40 rounded-xl" />
        ) : members.error ? (
          <p role="alert">
            {projectErrorMessage(members.error, t.projectWorkspace.errors)}
          </p>
        ) : (
          <div className="border-border overflow-x-auto rounded-xl border">
            <table className="w-full text-left text-sm">
              <thead className="bg-muted/60">
                <tr>
                  <th className="px-4 py-3">{labels.columns.account}</th>
                  <th className="px-4 py-3">{labels.columns.role}</th>
                  <th className="px-4 py-3">{labels.columns.status}</th>
                  <th className="px-4 py-3 text-right">
                    {labels.columns.actions}
                  </th>
                </tr>
              </thead>
              <tbody>
                {members.data?.map((member) => (
                  <tr key={member.membership_id} className="border-t">
                    <td className="px-4 py-3">{member.account_email}</td>
                    <td className="px-4 py-3">{labels.roles[member.role]}</td>
                    <td className="px-4 py-3">
                      <Badge variant="secondary">
                        {labels.membershipStatuses[member.status]}
                      </Badge>
                    </td>
                    <td className="space-x-2 px-4 py-3 text-right">
                      {canManage && member.status === "active" && (
                        <>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => setEditingMember(member)}
                          >
                            {labels.actions.changeRole}
                          </Button>
                          {member.membership_id !==
                            selfMembership?.membership_id && (
                            <Button
                              type="button"
                              size="sm"
                              variant="destructive"
                              disabled={removeMember.isPending}
                              onClick={() => {
                                removeMember.reset();
                                setDestructiveAction({
                                  kind: "remove-member",
                                  member,
                                });
                              }}
                            >
                              {labels.actions.removeMember}
                            </Button>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {(removeMember.error ?? leave.error) && (
          <p role="alert" className="text-destructive mt-3 text-sm">
            {projectErrorMessage(
              removeMember.error ?? leave.error,
              t.projectWorkspace.errors,
            )}
          </p>
        )}
        {selfMembership && (
          <Button
            type="button"
            variant="outline"
            className="mt-4"
            disabled={leave.isPending}
            onClick={() => {
              leave.reset();
              setDestructiveAction({
                kind: "leave-project",
                membership: selfMembership,
              });
            }}
          >
            {labels.actions.leaveProject}
          </Button>
        )}
      </section>

      <section aria-labelledby="project-invitations-title" className="mt-8">
        <h2
          id="project-invitations-title"
          className="mb-3 text-lg font-semibold"
        >
          {labels.invitationsTitle}
        </h2>
        {invitations.isLoading ? (
          <Skeleton className="h-28 rounded-xl" />
        ) : invitations.error ? (
          <p role="alert">
            {projectErrorMessage(invitations.error, t.projectWorkspace.errors)}
          </p>
        ) : invitations.data?.length ? (
          <ul className="space-y-3">
            {invitations.data.map((item) => (
              <li
                key={item.id}
                className="border-border flex flex-col gap-3 rounded-xl border p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                <div>
                  <p className="font-medium">{item.invited_email}</p>
                  <p className="text-muted-foreground mt-1 text-xs">
                    {labels.roles[item.role]} ·{" "}
                    {labels.invitationStatuses[item.status]}
                  </p>
                </div>
                {canManage && item.status === "pending" && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={revokeInvitation.isPending}
                    onClick={() => {
                      revokeInvitation.reset();
                      setDestructiveAction({
                        kind: "revoke-invitation",
                        invitation: item,
                      });
                    }}
                  >
                    {labels.actions.revokeInvitation}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground text-sm">
            {labels.emptyInvitations}
          </p>
        )}
      </section>

      {revokeInvitation.error && (
        <p role="alert" className="text-destructive mt-3 text-sm">
          {projectErrorMessage(
            revokeInvitation.error,
            t.projectWorkspace.errors,
          )}
        </p>
      )}

      <Dialog
        open={destructiveAction !== null}
        onOpenChange={(open) => {
          if (!open && !destructiveActionPending) setDestructiveAction(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{destructiveActionCopy?.title}</DialogTitle>
            <DialogDescription>
              {destructiveActionCopy?.description}
            </DialogDescription>
          </DialogHeader>
          {destructiveActionError && (
            <p role="alert" className="text-destructive text-sm">
              {projectErrorMessage(
                destructiveActionError,
                t.projectWorkspace.errors,
              )}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              autoFocus
              disabled={destructiveActionPending}
              onClick={() => setDestructiveAction(null)}
            >
              {labels.confirmations.cancel}
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={!destructiveAction || destructiveActionPending}
              onClick={confirmDestructiveAction}
            >
              {destructiveActionCopy?.confirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <MemberRoleDialog
        member={editingMember}
        open={editingMember !== null}
        pending={changeRole.isPending}
        errorMessage={
          changeRole.error
            ? projectErrorMessage(changeRole.error, t.projectWorkspace.errors)
            : null
        }
        onOpenChange={(open) => !open && setEditingMember(null)}
        onSubmit={(role) => {
          if (!editingMember) return;
          changeRole.mutate({
            membershipId: editingMember.membership_id,
            input: { role, version: editingMember.version },
          });
        }}
      />
      <CreateInvitationDialog
        open={invitationOpen}
        pending={createInvitation.isPending}
        result={createInvitation.data}
        errorMessage={
          createInvitation.error
            ? projectErrorMessage(
                createInvitation.error,
                t.projectWorkspace.errors,
              )
            : null
        }
        onOpenChange={(open) => {
          setInvitationOpen(open);
          if (!open) createInvitation.reset();
        }}
        onSubmit={(input) => createInvitation.mutate(input)}
      />
    </>
  );

  if (embedded) {
    return <div className="min-w-0">{body}</div>;
  }

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 lg:px-8">
      {body}
    </main>
  );
}
