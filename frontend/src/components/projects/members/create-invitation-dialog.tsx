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
import { Input } from "@/components/ui/input";
import type {
  CreateProjectInvitationInput,
  CreatedProjectInvitation,
  InvitableProjectRole,
} from "@/core/projects/types";
import { INVITABLE_PROJECT_ROLES } from "@/core/projects/types";

export function CreateInvitationDialog({
  open,
  pending,
  result,
  errorMessage,
  onOpenChange,
  onSubmit,
}: {
  open: boolean;
  pending: boolean;
  result: CreatedProjectInvitation | undefined;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: CreateProjectInvitationInput) => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InvitableProjectRole>("viewer");

  useEffect(() => {
    if (!open) {
      setEmail("");
      setRole("viewer");
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>邀请成员</DialogTitle>
          <DialogDescription>
            邀请链接只显示一次，请通过可信渠道发送。
          </DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="space-y-4">
            <label className="block space-y-2 text-sm font-medium">
              邀请链接
              <Input readOnly value={result.invite_url_fragment} />
            </label>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                完成
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              onSubmit({ email, role });
            }}
          >
            <label className="block space-y-2 text-sm font-medium">
              邮箱
              <Input
                type="email"
                value={email}
                required
                onChange={(event) => setEmail(event.target.value)}
              />
            </label>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">项目角色</legend>
              <div className="grid grid-cols-2 gap-2">
                {INVITABLE_PROJECT_ROLES.map((item) => (
                  <label
                    key={item}
                    className="border-border flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
                  >
                    <input
                      type="radio"
                      name="invitation-role"
                      checked={role === item}
                      onChange={() => setRole(item)}
                    />
                    {item[0]?.toUpperCase()}
                    {item.slice(1)}
                  </label>
                ))}
              </div>
            </fieldset>
            {errorMessage && (
              <p role="alert" className="text-destructive text-sm">
                {errorMessage}
              </p>
            )}
            <DialogFooter>
              <Button type="submit" disabled={pending}>
                创建邀请
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
