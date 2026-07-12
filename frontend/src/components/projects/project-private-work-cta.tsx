"use client";

import { LockKeyholeIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";

export function ProjectPrivateWorkCta() {
  return (
    <section className="border-border/70 bg-muted/30 rounded-2xl border p-6">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="flex items-center gap-2 font-semibold">
            <LockKeyholeIcon size={18} /> 项目内私有工作
          </h2>
          <p className="text-muted-foreground mt-2 text-sm">
            私有工作区将在后续里程碑开放
          </p>
          <p role="status" className="text-muted-foreground mt-1 text-xs">
            当前不会创建对话或离开本页。
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={!PROJECT_PRIVATE_WORKSPACE}
          aria-disabled={!PROJECT_PRIVATE_WORKSPACE}
        >
          开始私有对话
        </Button>
      </div>
    </section>
  );
}
