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
import { useI18n } from "@/core/i18n/hooks";
import { normalizeProjectSlug, projectSlugError } from "@/core/projects/slug";
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
  const { t } = useI18n();
  const copy = t.projectWorkspace.createDialog;
  const [displayName, setDisplayName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const slugValidationError = projectSlugError(slug, copy);
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
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            const normalizedSlug = normalizeProjectSlug(slug);
            const validationError = projectSlugError(normalizedSlug, copy);
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
            {copy.projectName}
            <Input
              autoFocus
              required
              maxLength={120}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
            />
          </label>
          <label className="grid gap-2 text-sm">
            {copy.projectSlug}
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
              {copy.slugHelp}
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
            {copy.descriptionLabel}
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
              {copy.cancel}
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? copy.creating : copy.create}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
