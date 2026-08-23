"use client";

import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/core/i18n/hooks";
import type { RunWorkloadProfileName } from "@/core/private-work/workload-profile";
import { cn } from "@/lib/utils";

export function RunWorkloadProfileBadge({
  profile,
}: {
  profile: RunWorkloadProfileName;
}) {
  const { t } = useI18n();
  return (
    <Badge
      variant="outline"
      className={cn(
        "shrink-0 text-xs font-medium",
        profile === "research" &&
          "border-blue-500/35 bg-blue-500/10 text-blue-700 dark:text-blue-300",
      )}
      data-testid="effective-run-workload-profile"
      data-workload-profile={profile}
    >
      {t.conversation.runWorkloadProfile(profile)}
    </Badge>
  );
}
