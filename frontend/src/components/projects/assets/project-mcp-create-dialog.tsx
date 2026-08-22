"use client";

import { useState } from "react";

import type { Project } from "@/core/projects/types";
import {
  useCreateConfiguredProjectMcp,
  useReplaceProjectMcpSecret,
  type ConfiguredMcpResponse,
  type CreateConfiguredMcpInput,
} from "@/core/shared-assets";

import {
  ProjectMcpFormDialog,
  type ProjectMcpFormSubmission,
} from "./project-mcp-form-dialog";

export type ProjectMcpCreateCompletion = {
  assetId: string;
  versionId: string;
  status: "published";
};

export function ProjectMcpCreateDialog({
  accountId,
  project,
  open,
  onOpenChange,
  onCompleted,
}: {
  accountId: string;
  project: Project;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCompleted: (result: ProjectMcpCreateCompletion) => void;
}) {
  const create = useCreateConfiguredProjectMcp(accountId, project.id);
  const replaceSecret = useReplaceProjectMcpSecret(accountId, project.id);
  const [created, setCreated] = useState<ConfiguredMcpResponse | null>(null);
  const [secretError, setSecretError] = useState<string | null>(null);
  const [savingSecret, setSavingSecret] = useState(false);

  function complete(result: ConfiguredMcpResponse) {
    onCompleted({
      assetId: result.item.id,
      versionId: result.version.id,
      status: "published",
    });
    setCreated(null);
    setSecretError(null);
    onOpenChange(false);
  }

  async function submit(submission: ProjectMcpFormSubmission) {
    setSecretError(null);
    try {
      const result =
        created ??
        (await create.mutateAsync(
          submission.input as CreateConfiguredMcpInput,
        ));
      setCreated(result);
      if (submission.secret) {
        setSavingSecret(true);
        try {
          await replaceSecret.mutateAsync({
            assetId: result.item.id,
            versionId: result.version.id,
            slotName: submission.secret.slotName,
            input: { payload: submission.secret.payload },
          });
        } finally {
          setSavingSecret(false);
        }
      }
      complete(result);
    } catch (error) {
      if (created || !create.error) {
        setSecretError(
          error instanceof Error ? error.message : "MCP 秘密保存失败。",
        );
      }
    }
  }

  return (
    <ProjectMcpFormDialog
      open={open}
      pending={create.isPending || savingSecret}
      errorMessage={
        secretError ??
        (create.error instanceof Error ? create.error.message : null)
      }
      onOpenChange={(next) => {
        if (!next) {
          create.reset();
          setCreated(null);
          setSecretError(null);
        }
        onOpenChange(next);
      }}
      onSubmit={(submission) => void submit(submission)}
    />
  );
}
