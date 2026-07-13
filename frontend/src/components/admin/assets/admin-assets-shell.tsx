"use client";

import {
  BotIcon,
  KeyRoundIcon,
  LogOutIcon,
  NetworkIcon,
  ShieldCheckIcon,
  SparklesIcon,
  UserRoundIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/core/auth/AuthProvider";
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
      className="flex min-w-max items-center gap-1 md:min-w-0 md:flex-col md:items-stretch"
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
  const { user, logout } = useAuth();

  return (
    <div
      data-testid="admin-assets-shell"
      className="bg-background min-h-screen md:grid md:grid-cols-[15rem_minmax(0,1fr)]"
    >
      <aside className="border-border/70 bg-card hidden min-h-screen border-r md:flex md:flex-col">
        <div className="border-border/70 border-b p-5">
          <div className="text-primary flex items-center gap-2 font-serif text-lg">
            <ShieldCheckIcon aria-hidden className="size-5" />
            DeerFlow
          </div>
          <p className="text-muted-foreground mt-1 text-xs">平台资产管理</p>
        </div>
        <div className="flex-1 p-3">
          <AdminAssetsNavigation pathname={pathname} />
        </div>
        <div className="border-border/70 border-t p-3">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                className="w-full justify-start"
              >
                <UserRoundIcon aria-hidden className="size-4" />
                <span className="truncate">{user?.email}</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-64">
              <DropdownMenuLabel className="truncate">
                {user?.email}
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => void logout()}>
                <LogOutIcon aria-hidden className="size-4" />
                退出登录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </aside>
      <div className="min-w-0">
        <header className="border-border/70 bg-background/95 sticky top-0 z-30 border-b px-4 py-3 backdrop-blur md:hidden">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <p className="text-primary font-serif">DeerFlow</p>
              <p className="text-muted-foreground text-xs">平台资产管理</p>
            </div>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="退出登录"
              onClick={() => void logout()}
            >
              <LogOutIcon aria-hidden className="size-4" />
            </Button>
          </div>
          <div className="overflow-x-auto">
            <AdminAssetsNavigation pathname={pathname} />
          </div>
        </header>
        {children}
      </div>
    </div>
  );
}
