"use client";

import { MessageSquarePlusIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  projectPrivateWorkEntryEnabled,
  useProjectPrivateWorkReadiness,
} from "@/core/private-work/readiness";
import { PROJECT_PRIVATE_WORKSPACE } from "@/core/projects/features";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

import { useMainProjectChat } from "./use-main-project-chat";

export function ProjectChatsPage({ project }: { project: Project }) {
  const mainChat = useMainProjectChat(project);
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
  return (
    <main className="flex size-full min-h-0 items-center justify-center px-6 py-12 text-center">
      <div className="flex max-w-md flex-col items-center">
        <div className="bg-primary/10 text-primary flex size-12 items-center justify-center rounded-2xl">
          <MessageSquarePlusIcon aria-hidden className="size-5" />
        </div>
        <h1 className="mt-5 text-2xl font-semibold">开始项目会话</h1>
        <p className="text-muted-foreground mt-2 text-sm leading-6">
          从会话列表选择已有会话，或使用 Main 开始新的对话。
        </p>
        {canCreate ? (
          <Button
            type="button"
            className="mt-6"
            disabled={
              !entryEnabled || mainChat.isCreating || mainChat.isLoading
            }
            aria-disabled={
              !entryEnabled || mainChat.isCreating || mainChat.isLoading
            }
            onClick={() => void mainChat.startMainChat()}
          >
            <MessageSquarePlusIcon aria-hidden className="size-4" />
            {mainChat.isLoading
              ? "正在准备…"
              : mainChat.isCreating
                ? "正在创建…"
                : "新建对话"}
          </Button>
        ) : (
          <p className="text-muted-foreground mt-6 text-sm">
            你可以查看自己的既有对话，但不能创建新工作
          </p>
        )}
      </div>
    </main>
  );
}
