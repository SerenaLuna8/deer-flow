"use client";

import { KeyRoundIcon } from "lucide-react";

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
        title="项目凭证"
        description="管理项目运行所需的环境变量、请求头、查询参数和 OAuth 字段。"
        icon={<KeyRoundIcon aria-hidden className="size-4" />}
      />

      <ProjectCredentialsWorkspace accountId={user.id} projectId={project.id} />
    </main>
  );
}
