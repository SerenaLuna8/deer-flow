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
import { Textarea } from "@/components/ui/textarea";
import type { PatchProjectInput, Project } from "@/core/projects/types";

export function EditProjectDialog({
  project,
  open,
  onOpenChange,
  onSubmit,
  pending,
  errorMessage,
}: {
  project: Project | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: PatchProjectInput) => Promise<void>;
  pending: boolean;
  errorMessage: string | null;
}) {
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [icon, setIcon] = useState("folder");

  useEffect(() => {
    if (!project || !open) return;
    setDisplayName(project.display_name);
    setDescription(project.description);
    setIcon(project.icon);
  }, [project, open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>编辑项目</DialogTitle>
          <DialogDescription>项目标识不可修改。</DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void onSubmit({ display_name: displayName, description, icon });
          }}
        >
          <label className="grid gap-2 text-sm">
            项目名称
            <Input
              required
              maxLength={120}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>
          <label className="grid gap-2 text-sm">
            图标
            <Input
              required
              maxLength={32}
              value={icon}
              onChange={(event) => setIcon(event.target.value)}
            />
          </label>
          <label className="grid gap-2 text-sm">
            描述
            <Textarea
              maxLength={500}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          {errorMessage && (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage}
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {pending ? "保存中…" : "保存修改"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
