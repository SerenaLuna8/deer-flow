"use client";

import { KeyRoundIcon, ShieldCheckIcon } from "lucide-react";

import { useAuth } from "@/core/auth/AuthProvider";

import { ProjectCredentialsWorkspace } from "../assets/project-assets-page";
import { useCurrentProject } from "../project-context";
import { ProjectPageHeader } from "../project-page-header";

export function ProjectCredentialPage() {
  const { user } = useAuth();
  const project = useCurrentProject();
  if (!user) return null;

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
      <ProjectPageHeader
        className="mb-5"
        eyebrow={`${project.display_name} · 安全连接`}
        title="Credential"
        description="管理 MCP 连接所需的环境变量、请求头和 OAuth 字段。"
        icon={<KeyRoundIcon aria-hidden className="size-4" />}
      />

      <section className="border-border/70 bg-muted/30 mb-5 flex items-start gap-3 rounded-xl border px-4 py-3">
        <ShieldCheckIcon
          aria-hidden
          className="mt-0.5 size-4 shrink-0 text-emerald-700 dark:text-emerald-400"
        />
        <div>
          <h2 className="text-sm font-semibold">敏感值只写入一次</h2>
          <p className="text-muted-foreground mt-0.5 text-xs leading-5 sm:text-sm">
            页面只展示名称、类型和字段结构。凭据值提交后不会回显，替换时需要重新填写当前版本的全部字段。
          </p>
        </div>
      </section>

      <ProjectCredentialsWorkspace accountId={user.id} projectId={project.id} />
    </main>
  );
}
