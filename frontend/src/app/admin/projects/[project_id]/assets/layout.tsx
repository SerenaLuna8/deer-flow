import { notFound } from "next/navigation";

import { AdminProjectAssetsShell } from "@/components/admin/assets/admin-project-assets-shell";
import { assetIdSchema } from "@/core/shared-assets";

export default async function AdminProjectAssetsLayout({
  children,
  params,
}: Readonly<{
  children: React.ReactNode;
  params: Promise<{ project_id: string }>;
}>) {
  const parsed = assetIdSchema.safeParse((await params).project_id);
  if (!parsed.success) notFound();
  return (
    <AdminProjectAssetsShell projectId={parsed.data}>
      {children}
    </AdminProjectAssetsShell>
  );
}
