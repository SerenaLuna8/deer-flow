"use client";

import { KeyRoundIcon, ShieldCheckIcon } from "lucide-react";

import { useAuth } from "@/core/auth/AuthProvider";

import { ProjectCredentialsWorkspace } from "../assets/project-assets-page";
import { useCurrentProject } from "../project-context";

export function ProjectCredentialPage() {
  const { user } = useAuth();
  const project = useCurrentProject();
  if (!user) return null;

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <header className="mb-8 max-w-3xl">
        <p className="text-primary mb-2 text-sm font-medium">
          {project.display_name} · 安全连接
        </p>
        <div className="flex items-center gap-3">
          <span className="bg-primary/10 text-primary flex size-11 items-center justify-center rounded-xl">
            <KeyRoundIcon aria-hidden className="size-5" />
          </span>
          <h1 className="text-3xl font-semibold tracking-tight">Credential</h1>
        </div>
        <p className="text-muted-foreground mt-3">
          管理 MCP 连接所需的环境变量、请求头和 OAuth 字段。
        </p>
      </header>

      <section className="border-border/70 bg-muted/30 mb-8 flex items-start gap-3 rounded-2xl border px-5 py-4">
        <ShieldCheckIcon
          aria-hidden
          className="mt-0.5 size-5 shrink-0 text-emerald-700 dark:text-emerald-400"
        />
        <div>
          <h2 className="text-sm font-semibold">敏感值只写入一次</h2>
          <p className="text-muted-foreground mt-1 text-sm leading-6">
            页面只展示名称、类型和字段结构。凭据值提交后不会回显，替换时需要重新填写当前版本的全部字段。
          </p>
        </div>
      </section>

      <ProjectCredentialsWorkspace accountId={user.id} projectId={project.id} />
    </main>
  );
}
