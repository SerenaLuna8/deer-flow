import { TelescopeIcon } from "lucide-react";

import { PromptInputButton } from "@/components/ai-elements/prompt-input";
import { useI18n } from "@/core/i18n/hooks";
import type { RunWorkloadProfileName } from "@/core/private-work/workload-profile";

import { Tooltip } from "./tooltip";

export function InputBoxWorkloadProfileToggle({
  profile,
  disabled,
  onSelect,
}: {
  profile: RunWorkloadProfileName;
  disabled: boolean;
  onSelect: (profile: RunWorkloadProfileName) => void;
}) {
  const { t } = useI18n();
  const selected = profile === "research";

  return (
    <Tooltip content={t.inputBox.researchWorkloadDescription}>
      <PromptInputButton
        aria-label={`${t.inputBox.researchWorkload}: ${t.inputBox.researchWorkloadDescription}`}
        aria-pressed={selected}
        className="max-w-32 gap-1! px-2!"
        data-testid="research-workload-toggle"
        disabled={disabled}
        variant={selected ? "secondary" : "ghost"}
        onClick={() => onSelect(selected ? "interactive" : "research")}
      >
        <TelescopeIcon className="size-3" />
        <span className="truncate text-xs font-normal">
          {t.inputBox.researchWorkload}
        </span>
      </PromptInputButton>
    </Tooltip>
  );
}
