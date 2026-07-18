"use client";

import { LockKeyholeIcon } from "lucide-react";
import { useState } from "react";

import { ProjectAgentSelectorDialog } from "@/components/projects/private-work/agent-selector-dialog";
import { Button } from "@/components/ui/button";
import {
  projectPrivateWorkEntryEnabled,
  useProjectPrivateWorkReadiness,
} from "@/core/private-work/readiness";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export function ProjectPrivateWorkCta({ project }: { project: Project }) {
  const [selectorOpen, setSelectorOpen] = useState(false);
  const canCreate =
    project.capabilities.includes("private_work.create") &&
    project.capabilities.includes("shared_assets.execute");
  const staticWebsiteOnly = isStaticWebsiteOnly();
  const readiness = useProjectPrivateWorkReadiness(
    canCreate && !staticWebsiteOnly,
  );
  const entryEnabled = projectPrivateWorkEntryEnabled(
    PROJECT_PRIVATE_WORKSPACE,
    canCreate,
    readiness.data?.status,
  );
  if (staticWebsiteOnly) return null;
  if (!canCreate) {
    return (
      <section className="border-border/70 bg-muted/30 rounded-2xl border p-6">
        <h2 className="flex items-center gap-2 font-semibold">
          <LockKeyholeIcon size={18} /> 项目内私有工作
        </h2>
        <p className="text-muted-foreground mt-2 text-sm">
          你可以查看自己的既有对话，但不能创建新工作
        </p>
      </section>
    );
  }
  return (
    <>
      <section className="border-border/70 bg-muted/30 rounded-2xl border p-6">
        <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 font-semibold">
              <LockKeyholeIcon size={18} /> 项目内私有工作
            </h2>
            <p className="text-muted-foreground mt-2 text-sm">
              选择项目可执行 Agent，开始只属于你的对话。
            </p>
            {!entryEnabled && (
              <p role="status" className="text-muted-foreground mt-1 text-xs">
                {readiness.data?.status === "unavailable" || readiness.isError
                  ? "暂时无法确认私有工作服务状态。"
                  : readiness.isLoading
                    ? "正在确认私有工作服务状态。"
                    : "私有工作服务尚未就绪。"}
              </p>
            )}
          </div>
          <Button
            type="button"
            variant="outline"
            disabled={!entryEnabled}
            aria-disabled={!entryEnabled}
            onClick={() => setSelectorOpen(true)}
          >
            开始私有对话
          </Button>
        </div>
      </section>
      {selectorOpen && (
        <ProjectAgentSelectorDialog
          project={project}
          open
          onOpenChange={setSelectorOpen}
        />
      )}
    </>
  );
}
