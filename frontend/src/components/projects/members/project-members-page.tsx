"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  useChangeProjectMemberRole,
  useCreateProjectInvitation,
  useLeaveProject,
  useProjectInvitations,
  useProjectMembers,
  useRemoveProjectMember,
  useRevokeProjectInvitation,
} from "@/core/projects/hooks";
import type { ProjectMembership } from "@/core/projects/types";

import { useCurrentProject } from "../project-context";
import { projectErrorMessage } from "../project-view-model";

import { CreateInvitationDialog } from "./create-invitation-dialog";
import { MemberRoleDialog } from "./member-role-dialog";

export function ProjectMembersPage() {
  const project = useCurrentProject();
  const { user } = useAuth();
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

  useEffect(() => {
    if (changeRole.isSuccess) setEditingMember(null);
  }, [changeRole.isSuccess]);

  const selfMembership = members.data?.find(
    (membership) => membership.user_id === user?.id,
  );

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">成员与邀请</h1>
          <p className="text-muted-foreground mt-2">
            管理项目成员、角色和待处理邀请。
          </p>
        </div>
        {canManage && (
          <Button type="button" onClick={() => setInvitationOpen(true)}>
            邀请成员
          </Button>
        )}
      </div>

      <section aria-labelledby="project-members-title" className="mt-8">
        <h2 id="project-members-title" className="mb-3 text-lg font-semibold">
          成员
        </h2>
        {members.isLoading ? (
          <Skeleton className="h-40 rounded-xl" />
        ) : members.error ? (
          <p role="alert">{projectErrorMessage(members.error)}</p>
        ) : (
          <div className="border-border overflow-x-auto rounded-xl border">
            <table className="w-full text-left text-sm">
              <thead className="bg-muted/60">
                <tr>
                  <th className="px-4 py-3">账户</th>
                  <th className="px-4 py-3">角色</th>
                  <th className="px-4 py-3">状态</th>
                  <th className="px-4 py-3 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {members.data?.map((member) => (
                  <tr key={member.membership_id} className="border-t">
                    <td className="px-4 py-3">{member.account_email}</td>
                    <td className="px-4 py-3">{member.role}</td>
                    <td className="px-4 py-3">
                      <Badge variant="secondary">{member.status}</Badge>
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
                            修改角色
                          </Button>
                          {member.user_id !== user?.id && (
                            <Button
                              type="button"
                              size="sm"
                              variant="destructive"
                              disabled={removeMember.isPending}
                              onClick={() =>
                                removeMember.mutate({
                                  membershipId: member.membership_id,
                                  version: member.version,
                                })
                              }
                            >
                              移除成员
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
            {projectErrorMessage(removeMember.error ?? leave.error)}
          </p>
        )}
        {selfMembership && (
          <Button
            type="button"
            variant="outline"
            className="mt-4"
            disabled={leave.isPending}
            onClick={() =>
              leave.mutate(selfMembership.version, {
                onSuccess: () => router.replace("/workspace"),
              })
            }
          >
            退出项目
          </Button>
        )}
      </section>

      <section aria-labelledby="project-invitations-title" className="mt-10">
        <h2
          id="project-invitations-title"
          className="mb-3 text-lg font-semibold"
        >
          邀请
        </h2>
        {invitations.isLoading ? (
          <Skeleton className="h-28 rounded-xl" />
        ) : invitations.error ? (
          <p role="alert">{projectErrorMessage(invitations.error)}</p>
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
                    {item.role} · {item.status}
                  </p>
                </div>
                {canManage && item.status === "pending" && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={revokeInvitation.isPending}
                    onClick={() =>
                      revokeInvitation.mutate({
                        invitationId: item.id,
                        version: item.version,
                      })
                    }
                  >
                    撤销邀请
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground text-sm">暂无项目邀请。</p>
        )}
      </section>

      {revokeInvitation.error && (
        <p role="alert" className="text-destructive mt-3 text-sm">
          {projectErrorMessage(revokeInvitation.error)}
        </p>
      )}

      <MemberRoleDialog
        member={editingMember}
        open={editingMember !== null}
        pending={changeRole.isPending}
        errorMessage={
          changeRole.error ? projectErrorMessage(changeRole.error) : null
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
            ? projectErrorMessage(createInvitation.error)
            : null
        }
        onOpenChange={(open) => {
          setInvitationOpen(open);
          if (!open) createInvitation.reset();
        }}
        onSubmit={(input) => createInvitation.mutate(input)}
      />
    </main>
  );
}
