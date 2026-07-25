"use client";

import { BotIcon, KeyRoundIcon, NetworkIcon, SparklesIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const NAVIGATION = [
  { href: "/admin/assets/agents", label: "Agent", icon: BotIcon },
  { href: "/admin/assets/skills", label: "Skill", icon: SparklesIcon },
  { href: "/admin/assets/mcp", label: "MCP", icon: NetworkIcon },
  {
    href: "/admin/assets/credentials",
    label: "Credential",
    icon: KeyRoundIcon,
  },
] as const;

export function AdminAssetsNavigation({ pathname }: { pathname: string }) {
  return (
    <nav
      aria-label="平台资产导航"
      className="flex min-w-0 flex-wrap items-center gap-1"
    >
      {NAVIGATION.map(({ href, label, icon: Icon }) => (
        <Link
          key={href}
          href={href}
          aria-current={pathname === href ? "page" : undefined}
          className={cn(
            "focus-visible:ring-ring flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium focus-visible:ring-2 focus-visible:outline-none",
            pathname === href
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
          )}
        >
          <Icon aria-hidden className="size-4" />
          {label}
        </Link>
      ))}
    </nav>
  );
}

export function AdminAssetsShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <section
      data-testid="admin-assets-shell"
      aria-label="平台资产管理"
      className="bg-background min-w-0 overflow-x-clip"
    >
      <div className="border-border/70 bg-muted/20 border-b px-4 py-2 lg:px-6">
        <AdminAssetsNavigation pathname={pathname} />
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}
