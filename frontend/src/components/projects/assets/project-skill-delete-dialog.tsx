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

const SKILL_DELETE_DELAY_MS = 5_000;

export function projectAssetDeleteDescription(
  assetKind: "Skill" | "Agent" | "MCP",
  assetName: string,
): string {
  if (assetKind === "Skill") {
    return `将永久删除整个 Skill 包“${assetName}”，包括包内所有版本与文件。此操作不可恢复；若 Agent 或历史运行仍引用该 Skill 的任一版本，将无法删除。此时可先停用以阻止后续使用；物理删除需解除 Agent 引用，并等待历史运行按保留策略清理。`;
  }
  if (assetKind === "Agent") {
    return `将永久删除整个 Agent“${assetName}”及其全部设置。此操作不可恢复；若该 Agent 是项目当前默认 Agent，请先将 Main 或其他 Agent 设为默认；若已有对话、自动化或运行记录引用该 Agent，将无法删除。`;
  }
  return `将永久删除整个 MCP“${assetName}”及其配置与 Credential 槽位。此操作不可恢复，已发布连接将不再可用；存在 Agent、历史运行或 Credential 授权快照引用时不会级联删除，需先解除引用。`;
}

export function skillDeleteSecondsRemaining(
  startedAt: number,
  now: number,
): number {
  return Math.min(
    5,
    Math.max(0, Math.ceil((startedAt + SKILL_DELETE_DELAY_MS - now) / 1_000)),
  );
}

export function ProjectSkillDeleteConfirmation({
  skillName,
  remainingSeconds,
  pending,
  errorMessage,
  onCancel,
  onConfirm,
}: {
  skillName: string;
  remainingSeconds: number;
  pending: boolean;
  errorMessage: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <ProjectAssetDeleteConfirmation
      assetKind="Skill"
      assetName={skillName}
      remainingSeconds={remainingSeconds}
      pending={pending}
      errorMessage={errorMessage}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );
}

export function ProjectAgentDeleteConfirmation({
  agentName,
  remainingSeconds,
  pending,
  errorMessage,
  onCancel,
  onConfirm,
}: {
  agentName: string;
  remainingSeconds: number;
  pending: boolean;
  errorMessage: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <ProjectAssetDeleteConfirmation
      assetKind="Agent"
      assetName={agentName}
      remainingSeconds={remainingSeconds}
      pending={pending}
      errorMessage={errorMessage}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );
}

export function ProjectMcpDeleteConfirmation({
  mcpName,
  remainingSeconds,
  pending,
  errorMessage,
  onCancel,
  onConfirm,
}: {
  mcpName: string;
  remainingSeconds: number;
  pending: boolean;
  errorMessage: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <ProjectAssetDeleteConfirmation
      assetKind="MCP"
      assetName={mcpName}
      remainingSeconds={remainingSeconds}
      pending={pending}
      errorMessage={errorMessage}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );
}

function ProjectAssetDeleteConfirmation({
  assetKind,
  assetName,
  remainingSeconds,
  pending,
  errorMessage,
  onCancel,
  onConfirm,
}: {
  assetKind: "Skill" | "Agent" | "MCP";
  assetName: string;
  remainingSeconds: number;
  pending: boolean;
  errorMessage: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const waiting = remainingSeconds > 0;

  return (
    <>
      <DialogHeader>
        <DialogTitle>永久删除 {assetKind}？</DialogTitle>
        <DialogDescription>
          {projectAssetDeleteDescription(assetKind, assetName)}
        </DialogDescription>
      </DialogHeader>
      {errorMessage ? (
        <p role="alert" className="text-destructive text-sm">
          {errorMessage}
        </p>
      ) : null}
      <DialogFooter>
        <Button
          type="button"
          variant="outline"
          disabled={pending}
          onClick={onCancel}
        >
          取消
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={pending || waiting}
          onClick={onConfirm}
        >
          {pending
            ? "删除中…"
            : waiting
              ? `确认删除（${remainingSeconds} 秒）`
              : "确认永久删除"}
        </Button>
      </DialogFooter>
    </>
  );
}

export function ProjectSkillDeleteDialog({
  skillName,
  startedAt,
  pending,
  errorMessage,
  onOpenChange,
  onConfirm,
}: {
  skillName: string;
  startedAt: number;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <ProjectAssetDeleteDialog
      assetKind="Skill"
      assetName={skillName}
      startedAt={startedAt}
      pending={pending}
      errorMessage={errorMessage}
      onOpenChange={onOpenChange}
      onConfirm={onConfirm}
    />
  );
}

export function ProjectAgentDeleteDialog({
  agentName,
  startedAt,
  pending,
  errorMessage,
  onOpenChange,
  onConfirm,
}: {
  agentName: string;
  startedAt: number;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <ProjectAssetDeleteDialog
      assetKind="Agent"
      assetName={agentName}
      startedAt={startedAt}
      pending={pending}
      errorMessage={errorMessage}
      onOpenChange={onOpenChange}
      onConfirm={onConfirm}
    />
  );
}

export function ProjectMcpDeleteDialog({
  mcpName,
  startedAt,
  pending,
  errorMessage,
  onOpenChange,
  onConfirm,
}: {
  mcpName: string;
  startedAt: number;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <ProjectAssetDeleteDialog
      assetKind="MCP"
      assetName={mcpName}
      startedAt={startedAt}
      pending={pending}
      errorMessage={errorMessage}
      onOpenChange={onOpenChange}
      onConfirm={onConfirm}
    />
  );
}

function ProjectAssetDeleteDialog({
  assetKind,
  assetName,
  startedAt,
  pending,
  errorMessage,
  onOpenChange,
  onConfirm,
}: {
  assetKind: "Skill" | "Agent" | "MCP";
  assetName: string;
  startedAt: number;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  const remainingSeconds = skillDeleteSecondsRemaining(startedAt, now);

  useEffect(() => {
    const interval = window.setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= startedAt + SKILL_DELETE_DELAY_MS) {
        window.clearInterval(interval);
      }
    }, 250);
    return () => window.clearInterval(interval);
  }, [startedAt]);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onOpenChange(false);
      }}
    >
      <DialogContent
        showCloseButton={!pending}
        onEscapeKeyDown={(event) => pending && event.preventDefault()}
        onInteractOutside={(event) => pending && event.preventDefault()}
      >
        <ProjectAssetDeleteConfirmation
          assetKind={assetKind}
          assetName={assetName}
          remainingSeconds={remainingSeconds}
          pending={pending}
          errorMessage={errorMessage}
          onCancel={() => onOpenChange(false)}
          onConfirm={() => {
            if (!pending && remainingSeconds === 0) onConfirm();
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
