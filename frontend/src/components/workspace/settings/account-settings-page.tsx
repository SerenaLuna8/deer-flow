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

import { SettingsSection } from "./settings-section";

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

  return (
    <div className="space-y-8">
      <SettingsSection title={t.settings.account.profileTitle}>
        <div className="space-y-2">
          <div className="grid grid-cols-[max-content_max-content] items-center gap-4">
            <span className="text-muted-foreground text-sm">
              {t.settings.account.email}
            </span>
            <span className="text-sm font-medium">{user?.email ?? "—"}</span>
            <span className="text-muted-foreground text-sm">
              {t.settings.account.role}
            </span>
            <span className="text-sm font-medium capitalize">
              {user?.system_role ?? "—"}
            </span>
            {isSsoUser && (
              <>
                <span className="text-muted-foreground text-sm">
                  {t.settings.account.ssoProvider}
                </span>
                <span className="text-sm font-medium capitalize">
                  {user?.oauth_provider}
                </span>
              </>
            )}
          </div>
        </div>
      </SettingsSection>

      {!isSsoUser ? (
        <SettingsSection
          title={t.settings.account.changePasswordTitle}
          description={t.settings.account.changePasswordDescription}
        >
          <form onSubmit={handleChangePassword} className="max-w-sm space-y-3">
            <Input
              type="password"
              placeholder={t.settings.account.currentPassword}
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
            />
            <Input
              type="password"
              placeholder={t.settings.account.newPassword}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
              minLength={8}
            />
            <Input
              type="password"
              placeholder={t.settings.account.confirmNewPassword}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={8}
            />
            {error && <p className="text-sm text-red-500">{error}</p>}
            {message && <p className="text-sm text-green-500">{message}</p>}
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
          <p className="text-muted-foreground text-sm">
            {t.settings.account.ssoPasswordMessage.replace(
              "{provider}",
              user?.oauth_provider ?? "",
            )}
          </p>
        </SettingsSection>
      )}

      <SettingsSection title="" description="">
        <Button
          variant="destructive"
          size="sm"
          onClick={logout}
          className="gap-2"
        >
          <LogOutIcon className="size-4" />
          {t.settings.account.signOut}
        </Button>
      </SettingsSection>
    </div>
  );
}
