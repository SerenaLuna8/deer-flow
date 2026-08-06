import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function ProjectForbidden() {
  return (
    <main
      data-error-status="403"
      className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center px-6 text-center"
    >
      <p className="text-muted-foreground text-sm font-medium">403</p>
      <h1 className="mt-2 text-2xl font-semibold">当前角色没有访问权限</h1>
      <p className="text-muted-foreground mt-3 leading-6">
        你仍是该项目成员，但当前角色不具备此治理区域所需的能力。
      </p>
      <Button asChild className="mt-6">
        <Link href="/workspace">返回工作空间</Link>
      </Button>
    </main>
  );
}
