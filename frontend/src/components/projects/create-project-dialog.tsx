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
import type { CreateProjectInput } from "@/core/projects/types";

export function CreateProjectDialog({
  open,
  onOpenChange,
  onSubmit,
  pending,
  errorMessage,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (input: CreateProjectInput) => void;
  pending: boolean;
  errorMessage: string | null;
}) {
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    if (!open) {
      setDisplayName("");
      setSlug("");
      setDescription("");
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>创建项目</DialogTitle>
          <DialogDescription>
            创建后你将成为项目 Admin，可继续邀请成员和配置共享资产。
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit({
              slug,
              display_name: displayName,
              description,
              icon: "folder",
            });
          }}
        >
          <label className="grid gap-2 text-sm">
            项目名称
            <Input
              autoFocus
              required
              maxLength={120}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>
          <label className="grid gap-2 text-sm">
            项目标识
            <Input
              required
              minLength={3}
              maxLength={63}
              pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
              placeholder="research-lab"
              value={slug}
              onChange={(event) => setSlug(event.target.value.toLowerCase())}
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
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? "创建中…" : "创建项目"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
