"use client";

import {
  CheckCircle2Icon,
  FolderKanbanIcon,
  LockKeyholeIcon,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { useUpdateProject } from "@/core/projects/hooks";
import type { PatchProjectInput } from "@/core/projects/types";

import { useCurrentProject } from "../project-context";
import { projectErrorMessage } from "../project-view-model";

export const PROJECT_GENERAL_SETTINGS_FIELDS = [
  "display_name",
  "icon",
  "description",
] as const;

function projectFormString(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

export function projectGeneralSettingsInput(form: FormData): PatchProjectInput {
  return {
    display_name: projectFormString(form, "display_name"),
    icon: projectFormString(form, "icon"),
    description: projectFormString(form, "description"),
  };
}

export function ProjectGeneralSettings() {
  const project = useCurrentProject();
  const { user } = useAuth();
  const update = useUpdateProject(user?.id, project.id);
  const { t } = useI18n();
  const labels = t.project.settings.general;
  const [saved, setSaved] = useState(false);
  const canUpdate = project.capabilities.includes("project.update");

  return (
    <section
      aria-labelledby="project-general-settings-title"
      className="border-border/70 bg-card rounded-2xl border"
    >
      <div className="border-border/70 border-b px-5 py-5 sm:px-6">
        <div className="flex items-start gap-3">
          <span className="bg-primary/10 text-primary flex size-10 shrink-0 items-center justify-center rounded-xl">
            <FolderKanbanIcon aria-hidden className="size-5" />
          </span>
          <div>
            <h2
              id="project-general-settings-title"
              className="text-lg font-semibold"
            >
              {labels.title}
            </h2>
            <p className="text-muted-foreground mt-1 text-sm">
              {labels.description}
            </p>
          </div>
        </div>
      </div>

      <form
        key={`${project.id}:${project.display_name}:${project.description}:${project.icon}`}
        className="space-y-5 px-5 py-6 sm:px-6"
        onSubmit={(event) => {
          event.preventDefault();
          if (!canUpdate) return;
          setSaved(false);
          const form = new FormData(event.currentTarget);
          update.mutate(projectGeneralSettingsInput(form), {
            onSuccess: () => setSaved(true),
          });
        }}
      >
        <div className="grid gap-5 sm:grid-cols-2">
          <label className="grid gap-2 text-sm font-medium">
            {labels.displayName}
            <Input
              name="display_name"
              defaultValue={project.display_name}
              maxLength={120}
              required
              readOnly={!canUpdate}
            />
          </label>
          <label className="grid gap-2 text-sm font-medium">
            {labels.icon}
            <Input
              name="icon"
              defaultValue={project.icon}
              maxLength={32}
              required
              readOnly={!canUpdate}
            />
          </label>
        </div>

        <label className="grid gap-2 text-sm font-medium">
          {labels.slug}
          <div className="relative">
            <Input
              value={project.slug}
              readOnly
              aria-describedby="project-slug-description"
              className="pr-10 font-mono"
            />
            <LockKeyholeIcon
              aria-hidden
              className="text-muted-foreground absolute top-1/2 right-3 size-4 -translate-y-1/2"
            />
          </div>
          <span
            id="project-slug-description"
            className="text-muted-foreground text-xs font-normal"
          >
            {labels.slugDescription}
          </span>
        </label>

        <label className="grid gap-2 text-sm font-medium">
          {labels.projectDescription}
          <Textarea
            name="description"
            defaultValue={project.description}
            maxLength={500}
            rows={4}
            readOnly={!canUpdate}
            placeholder={labels.descriptionPlaceholder}
          />
        </label>

        {update.error ? (
          <p role="alert" className="text-destructive text-sm">
            {projectErrorMessage(update.error, t.projectWorkspace.errors)}
          </p>
        ) : null}
        {saved && !update.error ? (
          <p
            role="status"
            className="text-muted-foreground flex items-center gap-2 text-sm"
          >
            <CheckCircle2Icon aria-hidden className="size-4 text-emerald-600" />
            {labels.saved}
          </p>
        ) : null}

        {canUpdate ? (
          <div className="flex justify-end">
            <Button type="submit" disabled={update.isPending}>
              {update.isPending ? labels.saving : labels.save}
            </Button>
          </div>
        ) : null}
      </form>
    </section>
  );
}
