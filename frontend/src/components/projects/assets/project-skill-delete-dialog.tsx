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
    return `删除后，Skill“${assetName}”将从项目中隐藏并停止用于新运行。系统会从所有 Agent 中移除该 Skill；这些 Agent 将保持各自当前状态，不会被自动停用。Skill 的全部版本文件、已配置的秘密密文以及所占存储空间都会保留，删除不会释放存储空间。只有整个项目被最终删除时，这些内容才会一并销毁。此操作不可恢复。`;
  }
  if (assetKind === "Agent") {
    return `删除后，Agent“${assetName}”将不再出现在项目 Agent 列表中，也不再用于新的运行。已有对话和运行记录会保留，正在执行的运行会继续完成。`;
  }
  return `将永久删除整个 MCP“${assetName}”及其配置与秘密槽位。此操作不可恢复，已发布连接将不再可用；存在 Agent、历史运行或执行快照引用时不会级联删除，需先解除引用。`;
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

export function projectAssetDeleteConfirmLabel(
  assetKind: "Skill" | "Agent" | "MCP",
  remainingSeconds: number,
  pending: boolean,
): string {
  if (pending) return "删除中…";
  if (assetKind === "Agent") return "确认删除";
  if (remainingSeconds > 0) return `确认删除（${remainingSeconds} 秒）`;
  return assetKind === "Skill" ? "确认删除" : "确认永久删除";
}

export function projectAssetDeleteTitle(
  assetKind: "Skill" | "Agent" | "MCP",
): string {
  return assetKind === "MCP"
    ? `永久删除 ${assetKind}？`
    : `删除 ${assetKind}？`;
}

export function projectSkillDeleteSuccessMessage(
  affectedAgentCount: number,
): string {
  return `已删除 Skill，并从 ${affectedAgentCount} 个 Agent 中移除绑定。`;
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
  const waiting = assetKind !== "Agent" && remainingSeconds > 0;

  return (
    <>
      <DialogHeader>
        <DialogTitle>{projectAssetDeleteTitle(assetKind)}</DialogTitle>
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
          {projectAssetDeleteConfirmLabel(assetKind, remainingSeconds, pending)}
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
            if (!pending && (assetKind === "Agent" || remainingSeconds === 0)) {
              onConfirm();
            }
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
