"use client";

import Link from "next/link";
import { forwardRef, useId } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type {
  SkillPublishPlanRequirement,
  SkillCredentialRequirement,
} from "@/core/shared-assets";

type EligibleCredential =
  SkillPublishPlanRequirement["eligible_credentials"][number];

export const SkillCredentialOptionSelect = forwardRef<
  HTMLSelectElement,
  {
    name: string;
    optional: boolean;
    options: readonly EligibleCredential[];
    value: string;
    disabled?: boolean;
    error?: boolean;
    allowEmpty?: boolean;
    onChange: (credentialVersionId: string) => void;
    onCreate?: () => void;
    manageHref?: string;
  }
>(function SkillCredentialOptionSelect(
  {
    name,
    optional,
    options,
    value,
    disabled = false,
    error = false,
    allowEmpty = true,
    onChange,
    onCreate,
    manageHref,
  },
  ref,
) {
  const { t } = useI18n();
  const copy = t.skills.secrets;
  const errorId = useId();
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 text-sm font-medium break-all">
          {name}
        </code>
        <Badge variant="secondary">
          {optional ? copy.optional : copy.required}
        </Badge>
      </div>
      <label className="grid gap-1">
        <span className="text-muted-foreground text-xs">
          {copy.credentialLabel}
        </span>
        <select
          ref={ref}
          aria-invalid={error || undefined}
          aria-describedby={error ? errorId : undefined}
          className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        >
          {allowEmpty ? (
            <option value="">
              {options.length === 0
                ? copy.noCompatibleCredential
                : optional
                  ? copy.optionalUnbound
                  : copy.selectCredential}
            </option>
          ) : null}
          {options.map((credential) => (
            <option
              key={credential.credential_version_id}
              value={credential.credential_version_id}
            >
              {copy.credentialVersion(
                credential.display_name,
                credential.version_number,
              )}
            </option>
          ))}
        </select>
      </label>
      {error ? (
        <p id={errorId} role="alert" className="text-destructive text-xs">
          {copy.requiredMissing}
        </p>
      ) : null}
      {options.length === 0 && (onCreate || manageHref) ? (
        <div className="flex flex-wrap gap-2">
          {onCreate ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={disabled}
              onClick={onCreate}
            >
              {copy.createCredential}
            </Button>
          ) : null}
          {manageHref ? (
            <Button asChild type="button" size="sm" variant="ghost">
              <Link href={manageHref}>{copy.manageCredential}</Link>
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});

export function skillCredentialRequirementOptions(
  requirement: SkillCredentialRequirement,
  selectedVersionId: string,
): EligibleCredential[] {
  const options = [...requirement.eligible_credentials];
  if (
    selectedVersionId !== "" &&
    !options.some(
      (credential) => credential.credential_version_id === selectedVersionId,
    ) &&
    requirement.configured &&
    requirement.credential_version_id === selectedVersionId
  ) {
    options.unshift({
      credential_id: requirement.credential_id,
      credential_version_id: requirement.credential_version_id,
      display_name: requirement.credential_display_name,
      version_number: requirement.credential_version_number,
    });
  }
  return options;
}
