"use client";

import { BotIcon, KeyRoundIcon, NetworkIcon, SparklesIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

const NAVIGATION = [
  { href: "/admin/assets/agents", labelKey: "agent", icon: BotIcon },
  { href: "/admin/assets/skills", labelKey: "skill", icon: SparklesIcon },
  { href: "/admin/assets/mcp", labelKey: "mcp", icon: NetworkIcon },
  {
    href: "/admin/assets/credentials",
    labelKey: "credential",
    icon: KeyRoundIcon,
  },
] as const;

export function AdminAssetsNavigation({ pathname }: { pathname: string }) {
  const { t } = useI18n();
  return (
    <nav
      aria-label={t.adminAssets.navigation.platformLabel}
      data-variant="line"
      className="flex min-w-0 flex-wrap items-center gap-5"
    >
      {NAVIGATION.map(({ href, labelKey, icon: Icon }) => (
        <Link
          key={href}
          href={href}
          aria-current={pathname === href ? "page" : undefined}
          className={cn(
            "focus-visible:ring-ring -mb-px flex items-center gap-2 border-b-2 border-transparent px-0.5 py-3 text-sm font-medium focus-visible:rounded-sm focus-visible:ring-2 focus-visible:outline-none",
            pathname === href
              ? "border-primary text-foreground"
              : "text-muted-foreground hover:border-border hover:text-foreground",
          )}
        >
          <Icon aria-hidden className="size-4" />
          {t.adminAssets.navigation[labelKey]}
        </Link>
      ))}
    </nav>
  );
}

export function AdminAssetsShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { t } = useI18n();

  return (
    <section
      data-testid="admin-assets-shell"
      aria-label={t.adminAssets.shell.platformAria}
      className="bg-background min-w-0 overflow-x-clip"
    >
      <div className="border-border bg-card border-b">
        <div className="mx-auto max-w-[96rem] px-4 sm:px-5 lg:px-6">
          <AdminAssetsNavigation pathname={pathname} />
        </div>
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}
