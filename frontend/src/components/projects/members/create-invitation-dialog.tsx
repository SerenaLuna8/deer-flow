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
import { useI18n } from "@/core/i18n/hooks";
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
  const { t } = useI18n();
  const labels = t.project.members;
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
          <DialogTitle>{labels.inviteDialog.title}</DialogTitle>
          <DialogDescription>
            {labels.inviteDialog.description}
          </DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="space-y-4">
            <label className="block space-y-2 text-sm font-medium">
              {labels.inviteDialog.inviteLink}
              <Input readOnly value={result.invite_url_fragment} />
            </label>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                {labels.inviteDialog.done}
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
              {labels.inviteDialog.email}
              <Input
                type="email"
                value={email}
                required
                onChange={(event) => setEmail(event.target.value)}
              />
            </label>
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">
                {labels.inviteDialog.projectRole}
              </legend>
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
                    {labels.roles[item]}
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
                {labels.inviteDialog.create}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
