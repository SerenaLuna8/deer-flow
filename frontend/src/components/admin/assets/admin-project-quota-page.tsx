"use client";

import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import { ProjectQuotaLimitsEditor } from "@/components/projects/governance/project-quota-limits-editor";
import { ProjectUsageStateView } from "@/components/projects/governance/project-usage-page";
import { usageViewCopy } from "@/components/projects/governance/project-usage-view-model";
import {
  useAdminProjectUsage,
  useUpdateAdminProjectQuotaLimits,
} from "@/core/admin-operations/api";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";

function AuthorizedAdminProjectQuotaPage({
  accountId,
  projectId,
}: {
  accountId: string;
  projectId: string;
}) {
  const { locale, t } = useI18n();
  const labels = t.project.governance.usage;
  const copy = usageViewCopy[locale];
  const usage = useAdminProjectUsage(accountId, projectId);
  const update = useUpdateAdminProjectQuotaLimits(accountId, projectId);

  const state = usage.isLoading
    ? ({ status: "loading" } as const)
    : usage.error || !usage.data
      ? ({ status: "error" } as const)
      : ({ status: "ready", data: usage.data } as const);

  return (
    <AdminPage data-testid="admin-project-quota-page">
      <AdminPageHeader
        title={labels.tightenTitle}
        description={labels.settingsDescription}
      />
      <AdminSection>
        {state.status !== "ready" || !usage.data ? (
          <ProjectUsageStateView
            state={state.status === "ready" ? { status: "error" } : state}
            onRetry={() => void usage.refetch()}
          />
        ) : (
          <div className="space-y-4">
            <p className="text-muted-foreground max-w-2xl text-sm leading-6">
              {copy.editorDescription}
            </p>
            <ProjectQuotaLimitsEditor
              data={usage.data}
              pending={update.isPending}
              error={Boolean(update.error)}
              onSubmit={(input) => update.mutate(input)}
            />
          </div>
        )}
      </AdminSection>
    </AdminPage>
  );
}

export function AdminProjectQuotaPage({ projectId }: { projectId: string }) {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
  return (
    <AuthorizedAdminProjectQuotaPage
      accountId={user.id}
      projectId={projectId}
    />
  );
}
