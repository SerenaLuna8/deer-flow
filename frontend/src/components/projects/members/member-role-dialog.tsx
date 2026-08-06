"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { ProjectMembership, ProjectRole } from "@/core/projects/types";

const ROLES: ProjectRole[] = ["admin", "editor", "runner", "viewer"];

export function MemberRoleDialog({
  member,
  open,
  pending,
  errorMessage,
  onOpenChange,
  onSubmit,
}: {
  member: ProjectMembership | null;
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (role: ProjectRole) => void;
}) {
  const [role, setRole] = useState<ProjectRole>(member?.role ?? "viewer");

  useEffect(() => {
    if (member) setRole(member.role);
  }, [member]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>修改成员角色</DialogTitle>
          <DialogDescription>{member?.account_email}</DialogDescription>
        </DialogHeader>
        <fieldset className="space-y-2">
          <legend className="sr-only">项目角色</legend>
          {ROLES.map((item) => (
            <label
              key={item}
              className="border-border flex items-center gap-3 rounded-lg border px-3 py-2 text-sm"
            >
              <input
                type="radio"
                name="member-role"
                checked={role === item}
                onChange={() => setRole(item)}
              />
              {item[0]?.toUpperCase()}
              {item.slice(1)}
            </label>
          ))}
        </fieldset>
        {errorMessage && (
          <p role="alert" className="text-destructive text-sm">
            {errorMessage}
          </p>
        )}
        <DialogFooter>
          <Button
            type="button"
            disabled={pending || !member}
            onClick={() => onSubmit(role)}
          >
            保存角色
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
