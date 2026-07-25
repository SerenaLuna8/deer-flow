import { notFound, redirect } from "next/navigation";

import { assetIdSchema } from "@/core/shared-assets";

export default async function AdminProjectAssetsIndex({
  params,
}: {
  params: Promise<{ project_id: string }>;
}) {
  const parsed = assetIdSchema.safeParse((await params).project_id);
  if (!parsed.success) notFound();
  redirect(`/admin/projects/${parsed.data}/assets/agents`);
}
