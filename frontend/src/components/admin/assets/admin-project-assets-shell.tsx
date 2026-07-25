"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  KeyRoundIcon,
  NetworkIcon,
  SparklesIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const NAVIGATION = [
  { segment: "agents", label: "Agent", icon: BotIcon },
  { segment: "skills", label: "Skill", icon: SparklesIcon },
  { segment: "mcp", label: "MCP", icon: NetworkIcon },
  { segment: "credentials", label: "Credential", icon: KeyRoundIcon },
] as const;

export function AdminProjectAssetsShell({
  projectId,
  children,
}: {
  projectId: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const base = `/admin/projects/${projectId}/assets`;

  return (
    <section
      data-testid="admin-project-assets-shell"
      aria-label="项目共享资产代管"
      className="bg-background min-w-0 overflow-x-clip"
    >
      <div className="border-border/70 bg-muted/20 border-b px-4 py-4 lg:px-6">
        <div className="mx-auto max-w-7xl space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <Link
                href="/admin/projects"
                className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-sm"
              >
                <ArrowLeftIcon aria-hidden className="size-4" />
                返回项目选择
              </Link>
              <h2 className="mt-2 text-lg font-semibold">项目共享资产代管</h2>
              <p className="text-muted-foreground mt-1 max-w-3xl text-sm">
                仅治理共享 Agent、Skill、MCP、Credential
                与系统资产绑定；不会读取成员、聊天、运行、记忆、文件或其他用户私有内容。
              </p>
            </div>
            <div className="border-border bg-background min-w-0 rounded-lg border px-3 py-2 text-xs">
              <span className="text-muted-foreground block">当前项目 ID</span>
              <span className="block max-w-full font-mono break-all">
                {projectId}
              </span>
            </div>
          </div>
          <nav
            aria-label="项目资产代管导航"
            className="grid grid-cols-2 gap-1 sm:flex sm:flex-wrap"
          >
            {NAVIGATION.map(({ segment, label, icon: Icon }) => {
              const href = `${base}/${segment}`;
              const active = pathname === href;
              return (
                <Link
                  key={segment}
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "focus-visible:ring-ring flex min-w-0 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none sm:justify-start",
                    active
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  )}
                >
                  <Icon aria-hidden className="size-4 shrink-0" />
                  <span className="truncate">{label}</span>
                </Link>
              );
            })}
          </nav>
        </div>
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}
