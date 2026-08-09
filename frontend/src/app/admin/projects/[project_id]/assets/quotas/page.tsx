import { notFound } from "next/navigation";

import { AdminProjectQuotaPage } from "@/components/admin/assets/admin-project-quota-page";
import { assetIdSchema } from "@/core/shared-assets/types";

export default async function AdminProjectQuotaRoute({
  params,
}: {
  params: Promise<{ project_id: string }>;
}) {
  const parsed = assetIdSchema.safeParse((await params).project_id);
  if (!parsed.success) notFound();
  return <AdminProjectQuotaPage projectId={parsed.data} />;
}
