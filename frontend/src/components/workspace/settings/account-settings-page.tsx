"use client";

import { LogOutIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  AUTH_SUBMIT_TIMEOUT_MS,
  fetchWithAuthTimeout,
  isAbortError,
} from "@/core/auth/request";
import { parseAuthError } from "@/core/auth/types";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

import { SettingsSection } from "./settings-section";

function ProfileField({
  label,
  value,
  className,
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-1 px-4 py-3 sm:flex-row sm:items-baseline sm:gap-6",
        className,
      )}
    >
      <dt className="text-muted-foreground w-24 shrink-0 text-sm">{label}</dt>
      <dd className="min-w-0 text-sm font-medium [overflow-wrap:anywhere]">
        {value}
      </dd>
    </div>
  );
}

function RequiredLabel({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-0.5 text-sm font-medium">
      {label}
      <span className="text-destructive" aria-hidden>
        *
      </span>
    </span>
  );
}

export function AccountSettingsPage() {
  const { user, logout } = useAuth();
  const { t } = useI18n();
  const isSsoUser = Boolean(user?.oauth_provider);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const requestControllerRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      requestControllerRef.current?.abort();
    },
    [],
  );

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setMessage("");

    if (newPassword !== confirmPassword) {
      setError(t.settings.account.passwordMismatch);
      return;
    }
    if (newPassword.length < 8) {
      setError(t.settings.account.passwordTooShort);
      return;
    }

    setLoading(true);
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    try {
      const res = await fetchWithAuthTimeout(
        fetchWithAuth,
        "/api/v1/auth/change-password",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          signal: controller.signal,
          body: JSON.stringify({
            current_password: currentPassword,
            new_password: newPassword,
          }),
        },
        AUTH_SUBMIT_TIMEOUT_MS,
      );

      if (!res.ok) {
        const data: unknown = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      setMessage(t.settings.account.passwordChangedSuccess);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (requestError) {
      if (
        controller.signal.aborted ||
        isAbortError(requestError) ||
        requestError instanceof AuthRequiredError
      ) {
        return;
      }
      setError(t.settings.account.networkError);
    } finally {
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
        setLoading(false);
      }
    }
  };

  const roleLabel = user?.system_role
    ? t.settings.account.roles[user.system_role]
    : "—";

  return (
    <div className="space-y-8">
      <SettingsSection title={t.settings.account.profileTitle}>
        <dl className="divide-border bg-muted/20 max-w-xl divide-y rounded-xl border">
          <ProfileField
            label={t.settings.account.email}
            value={user?.email ?? "—"}
          />
          <ProfileField label={t.settings.account.role} value={roleLabel} />
          {isSsoUser ? (
            <ProfileField
              label={t.settings.account.ssoProvider}
              value={user?.oauth_provider ?? "—"}
            />
          ) : null}
        </dl>
      </SettingsSection>

      {!isSsoUser ? (
        <SettingsSection title={t.settings.account.changePasswordTitle}>
          <form onSubmit={handleChangePassword} className="max-w-sm space-y-4">
            <label className="block space-y-2">
              <RequiredLabel label={t.settings.account.currentPassword} />
              <Input
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
                aria-required="true"
              />
            </label>
            <label className="block space-y-2">
              <RequiredLabel label={t.settings.account.newPassword} />
              <Input
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                aria-required="true"
                minLength={8}
              />
            </label>
            <label className="block space-y-2">
              <RequiredLabel label={t.settings.account.confirmNewPassword} />
              <Input
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                aria-required="true"
                minLength={8}
              />
            </label>
            {error ? <p className="text-destructive text-sm">{error}</p> : null}
            {message ? (
              <p className="text-sm text-emerald-600">{message}</p>
            ) : null}
            <Button
              type="submit"
              variant="outline"
              size="sm"
              disabled={loading}
            >
              {loading
                ? t.settings.account.updating
                : t.settings.account.updatePassword}
            </Button>
          </form>
        </SettingsSection>
      ) : (
        <SettingsSection
          title={t.settings.account.changePasswordTitle}
          description={t.settings.account.ssoPasswordDescription}
        >
          <p className="text-muted-foreground max-w-xl text-sm">
            {t.settings.account.ssoPasswordMessage.replace(
              "{provider}",
              user?.oauth_provider ?? "",
            )}
          </p>
        </SettingsSection>
      )}

      <div className="border-t pt-6">
        <Button
          variant="destructive"
          size="sm"
          onClick={logout}
          className="gap-2"
        >
          <LogOutIcon className="size-4" />
          {t.settings.account.signOut}
        </Button>
      </div>
    </div>
  );
}
