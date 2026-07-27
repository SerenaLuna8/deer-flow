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
import {
  normalizeProjectSlug,
  PROJECT_SLUG_HELP,
  projectSlugError,
} from "@/core/projects/slug";
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
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const slugValidationError = projectSlugError(slug);
  const showSlugError = slugTouched && slugValidationError !== null;

  useEffect(() => {
    if (!open) {
      setDisplayName("");
      setSlug("");
      setSlugTouched(false);
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
            const normalizedSlug = normalizeProjectSlug(slug);
            const validationError = projectSlugError(normalizedSlug);
            setSlug(normalizedSlug);
            if (validationError) {
              setSlugTouched(true);
              return;
            }
            onSubmit({
              slug: normalizedSlug,
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
              maxLength={63}
              placeholder="research-lab"
              value={slug}
              aria-required="true"
              aria-describedby={
                showSlugError
                  ? "project-slug-help project-slug-error"
                  : "project-slug-help"
              }
              aria-invalid={showSlugError}
              onChange={(event) => setSlug(event.target.value.toLowerCase())}
              onBlur={() => {
                setSlug(normalizeProjectSlug(slug));
                setSlugTouched(true);
              }}
            />
            <span
              id="project-slug-help"
              className="text-muted-foreground text-xs"
            >
              {PROJECT_SLUG_HELP}
            </span>
            {showSlugError && (
              <span
                id="project-slug-error"
                role="alert"
                className="text-destructive text-xs"
              >
                {slugValidationError}
              </span>
            )}
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
