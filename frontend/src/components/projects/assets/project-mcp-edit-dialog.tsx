"use client";

import { useState } from "react";

import type { Project } from "@/core/projects/types";
import {
  useReplaceProjectMcpSecret,
  useUpdateConfiguredProjectMcp,
  type ConfiguredMcpResponse,
  type ProjectMcpEditableConfigurationResponse,
  type UpdateConfiguredMcpInput,
} from "@/core/shared-assets";

import {
  ProjectMcpFormDialog,
  type ProjectMcpFormSubmission,
} from "./project-mcp-form-dialog";

export type ProjectMcpEditCompletion = {
  assetId: string;
  versionId: string;
  status: "published";
};

export function ProjectMcpEditDialog({
  accountId,
  project,
  configuration,
  open,
  onOpenChange,
  onCompleted,
}: {
  accountId: string;
  project: Project;
  configuration: ProjectMcpEditableConfigurationResponse;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCompleted: (result: ProjectMcpEditCompletion) => void;
}) {
  const update = useUpdateConfiguredProjectMcp(accountId, project.id);
  const replaceSecret = useReplaceProjectMcpSecret(accountId, project.id);
  const [updated, setUpdated] = useState<ConfiguredMcpResponse | null>(null);
  const [secretError, setSecretError] = useState<string | null>(null);
  const [savingSecret, setSavingSecret] = useState(false);

  function complete(result: ConfiguredMcpResponse) {
    onCompleted({
      assetId: result.item.id,
      versionId: result.version.id,
      status: "published",
    });
    setUpdated(null);
    setSecretError(null);
    onOpenChange(false);
  }

  async function submit(submission: ProjectMcpFormSubmission) {
    setSecretError(null);
    try {
      const result =
        updated ??
        (await update.mutateAsync({
          assetId: configuration.item.id,
          input: submission.input as UpdateConfiguredMcpInput,
        }));
      setUpdated(result);
      if (submission.secret) {
        setSavingSecret(true);
        try {
          await replaceSecret.execute({
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
      setSecretError(
        error instanceof Error ? error.message : "MCP 配置或秘密保存失败。",
      );
    }
  }

  return (
    <ProjectMcpFormDialog
      open={open}
      pending={update.isPending || savingSecret}
      errorMessage={
        secretError ??
        (update.error instanceof Error ? update.error.message : null)
      }
      configuration={configuration}
      onOpenChange={(next) => {
        if (!next) {
          update.reset();
          setUpdated(null);
          setSecretError(null);
        }
        onOpenChange(next);
      }}
      onSubmit={(submission) => void submit(submission)}
    />
  );
}
