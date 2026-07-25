import { ShieldXIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export function ProjectAccessDenied({
  projectSlug,
  area,
}: {
  projectSlug: string;
  area: string;
}) {
  return (
    <main
      role="alert"
      data-error-status="403"
      className="mx-auto flex min-h-[50vh] max-w-xl flex-col items-center justify-center px-6 text-center"
    >
      <span className="bg-muted flex size-12 items-center justify-center rounded-2xl">
        <ShieldXIcon aria-hidden className="text-muted-foreground size-6" />
      </span>
      <h1 className="mt-5 text-2xl font-semibold">没有访问权限</h1>
      <p className="text-muted-foreground mt-3 text-sm leading-6">
        你是该项目的成员，但当前角色无权访问{area}
        。如需使用，请联系项目管理员调整角色。
      </p>
      <Button asChild className="mt-6">
        <Link href={`/projects/${encodeURIComponent(projectSlug)}`}>
          返回项目首页
        </Link>
      </Button>
    </main>
  );
}
