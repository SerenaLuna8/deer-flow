"use client";

import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useRef, useState } from "react";

import { RememberLoginField } from "@/components/auth/remember-login-field";
import { Button } from "@/components/ui/button";
import { FlickeringGrid } from "@/components/ui/flickering-grid";
import { Input } from "@/components/ui/input";
import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  loadRememberLoginPreference,
  saveRememberLoginPreference,
} from "@/core/auth/remember-login";
import {
  buildRememberingCredentialPayload,
  buildSetupPasswordChangePayload,
} from "@/core/auth/remember-payloads";
import {
  AUTH_SUBMIT_TIMEOUT_MS,
  fetchAuth,
  fetchWithAuthTimeout,
  isAbortError,
} from "@/core/auth/request";
import {
  fetchSetupStatus,
  isSystemAlreadyInitializedError,
} from "@/core/auth/setup";
import { parseAuthError, userSchema } from "@/core/auth/types";
import { parseUsername } from "@/core/auth/username";
import { useI18n } from "@/core/i18n/hooks";

type SetupMode = "loading" | "unavailable" | "init_admin" | "change_password";

export default function SetupPage() {
  const router = useRouter();
  const { user, isAuthenticated, applyUser } = useAuth();
  const { theme, resolvedTheme } = useTheme();
  const { t } = useI18n();
  const [mode, setMode] = useState<SetupMode>("loading");

  // --- Shared state ---
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const probeControllerRef = useRef<AbortController | null>(null);
  const submitControllerRef = useRef<AbortController | null>(null);

  // --- Change-password mode only ---
  const [currentPassword, setCurrentPassword] = useState("");

  useEffect(() => {
    const preference = loadRememberLoginPreference();
    setRememberMe(preference.rememberMe);
    if (preference.email) setEmail(preference.email);
  }, []);

  const checkSetupStatus = useCallback(async () => {
    probeControllerRef.current?.abort();
    const controller = new AbortController();
    probeControllerRef.current = controller;
    setMode("loading");

    try {
      const data = await fetchSetupStatus(controller.signal);
      if (controller.signal.aborted) return;
      if (data.needs_setup) {
        setMode("init_admin");
      } else {
        router.replace("/login");
      }
    } catch (requestError) {
      if (controller.signal.aborted || isAbortError(requestError)) return;
      setMode("unavailable");
    }
  }, [router]);

  useEffect(() => {
    if (isAuthenticated && user?.needs_setup) {
      probeControllerRef.current?.abort();
      setMode("change_password");
    } else if (!isAuthenticated) {
      void checkSetupStatus();
    } else {
      probeControllerRef.current?.abort();
      router.replace("/workspace");
    }
    return () => {
      probeControllerRef.current?.abort();
      submitControllerRef.current?.abort();
    };
  }, [checkSetupStatus, isAuthenticated, user, router]);

  // ── Init-admin handler ─────────────────────────────────────────────
  const handleInitAdmin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError(t.setup.passwordMismatch);
      return;
    }
    if (!parseUsername(username)) {
      setError(t.setup.usernameInvalid);
      return;
    }

    setLoading(true);
    submitControllerRef.current?.abort();
    const controller = new AbortController();
    submitControllerRef.current = controller;
    try {
      const res = await fetchAuth(
        "/api/v1/auth/initialize",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: controller.signal,
          body: JSON.stringify(
            buildRememberingCredentialPayload({
              email,
              username,
              password: newPassword,
              rememberMe,
            }),
          ),
        },
        AUTH_SUBMIT_TIMEOUT_MS,
      );

      if (!res.ok) {
        const data: unknown = await res.json();
        if (isSystemAlreadyInitializedError(data)) {
          router.replace("/login");
          return;
        }
        const authError = parseAuthError(data);
        setError(
          authError.code === "username_already_exists"
            ? t.login.usernameTaken
            : authError.code === "invalid_username"
              ? t.setup.usernameInvalid
              : authError.code === "email_already_exists"
                ? t.login.emailTaken
                : authError.message,
        );
        return;
      }

      const parsed = userSchema.safeParse(await res.json());
      if (!parsed.success) {
        setError(t.setup.networkError);
        return;
      }
      saveRememberLoginPreference({ email, rememberMe });
      await applyUser(parsed.data);
      window.location.replace("/workspace");
    } catch (requestError) {
      if (controller.signal.aborted || isAbortError(requestError)) return;
      setError(t.setup.networkError);
    } finally {
      if (submitControllerRef.current === controller) {
        submitControllerRef.current = null;
        setLoading(false);
      }
    }
  };

  // ── Change-password handler ────────────────────────────────────────
  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError(t.setup.passwordMismatch);
      return;
    }
    if (newPassword.length < 8) {
      setError(t.setup.passwordTooShort);
      return;
    }

    setLoading(true);
    submitControllerRef.current?.abort();
    const controller = new AbortController();
    submitControllerRef.current = controller;
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
          body: JSON.stringify(
            buildSetupPasswordChangePayload({
              currentPassword,
              email,
              newPassword,
              rememberMe,
            }),
          ),
        },
        AUTH_SUBMIT_TIMEOUT_MS,
      );

      if (!res.ok) {
        const data: unknown = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      saveRememberLoginPreference({
        email: email.length > 0 ? email : (user?.email ?? ""),
        rememberMe,
      });
      // The mutation already rotated every old sid and installed the fresh
      // access/CSRF cookie pair. Cross the boundary with a document navigation
      // so a transient follow-up /me probe can never invite resubmission of the
      // now-invalid old password.
      window.location.replace("/workspace");
    } catch (requestError) {
      if (
        controller.signal.aborted ||
        isAbortError(requestError) ||
        requestError instanceof AuthRequiredError
      ) {
        return;
      }
      setError(t.setup.networkError);
    } finally {
      if (submitControllerRef.current === controller) {
        submitControllerRef.current = null;
        setLoading(false);
      }
    }
  };

  const actualTheme = theme === "system" ? resolvedTheme : theme;

  if (mode === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-muted-foreground text-sm">{t.setup.loading}</p>
      </div>
    );
  }

  if (mode === "unavailable") {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 px-6 text-center">
        <p className="text-muted-foreground text-sm">
          {t.login.registrationUnavailable}
        </p>
        <Button
          type="button"
          variant="outline"
          onClick={() => void checkSetupStatus()}
        >
          {t.login.retry}
        </Button>
      </div>
    );
  }

  // ── Admin initialization form ──────────────────────────────────────
  if (mode === "init_admin") {
    return (
      <div className="bg-background flex min-h-screen items-center justify-center">
        <FlickeringGrid
          className="absolute inset-0 z-0 mask-[url(/images/deer.svg)] mask-size-[100vw] mask-center mask-no-repeat md:mask-size-[72vh]"
          squareSize={4}
          gridGap={4}
          color={actualTheme === "dark" ? "white" : "black"}
          maxOpacity={0.3}
          flickerChance={0.25}
        />
        <div className="border-border/20 bg-background/5 w-full max-w-md space-y-6 rounded-3xl border p-8 backdrop-blur-sm">
          <div className="text-center">
            <h1 className="font-serif text-3xl">Fluva</h1>
            <p className="text-muted-foreground mt-1 text-sm">
              Weave intelligence into action.
            </p>
            <p className="text-muted-foreground mt-3">
              {t.setup.initAdminTitle}
            </p>
            <p className="text-muted-foreground mt-1 text-xs">
              {t.setup.initAdminDescription}
            </p>
          </div>
          <form onSubmit={handleInitAdmin} className="space-y-2">
            <div className="flex flex-col space-y-1">
              <label htmlFor="username" className="text-sm font-medium">
                {t.setup.username}
              </label>
              <Input
                id="username"
                type="text"
                autoComplete="username"
                placeholder={t.setup.usernamePlaceholder}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                minLength={3}
                maxLength={32}
                pattern="[A-Za-z][A-Za-z0-9_]{2,31}"
              />
              <p className="text-muted-foreground text-xs">
                {t.setup.usernameHint}
              </p>
            </div>
            <div className="flex flex-col space-y-1">
              <label htmlFor="email" className="text-sm font-medium">
                {t.setup.email}
              </label>
              <Input
                id="email"
                type="email"
                placeholder={t.setup.emailPlaceholder}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="flex flex-col space-y-1">
              <label htmlFor="password" className="text-sm font-medium">
                {t.setup.password}
              </label>
              <Input
                id="password"
                type="password"
                placeholder={t.setup.passwordPlaceholder}
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                minLength={8}
              />
            </div>
            <div className="flex flex-col space-y-1">
              <label htmlFor="confirmPassword" className="text-sm font-medium">
                {t.setup.confirmPassword}
              </label>
              <Input
                id="confirmPassword"
                type="password"
                placeholder={t.setup.confirmPasswordPlaceholder}
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                minLength={8}
              />
            </div>
            <RememberLoginField
              checked={rememberMe}
              disabled={loading}
              label={t.login.rememberMe}
              onCheckedChange={setRememberMe}
            />
            {error && <p className="ms-1 text-sm text-red-500">{error}</p>}
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? t.setup.creatingAccount : t.setup.createAdminAccount}
            </Button>
          </form>
        </div>
      </div>
    );
  }

  // ── Change-password form (needs_setup after login) ─────────────────
  return (
    <div className="bg-background flex min-h-screen items-center justify-center">
      <FlickeringGrid
        className="absolute inset-0 z-0 mask-[url(/images/deer.svg)] mask-size-[100vw] mask-center mask-no-repeat md:mask-size-[72vh]"
        squareSize={4}
        gridGap={4}
        color={actualTheme === "dark" ? "white" : "black"}
        maxOpacity={0.3}
        flickerChance={0.25}
      />
      <div className="border-border/20 bg-background/5 w-full max-w-md space-y-6 rounded-3xl border p-8 backdrop-blur-sm">
        <div className="text-center">
          <h1 className="font-serif text-3xl">Fluva</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Weave intelligence into action.
          </p>
          <p className="text-muted-foreground mt-3">
            {t.setup.completeAdminTitle}
          </p>
          <p className="text-muted-foreground mt-1 text-xs">
            {t.setup.completeAdminDescription}
          </p>
        </div>
        <form onSubmit={handleChangePassword} className="space-y-4">
          <Input
            type="email"
            placeholder={t.setup.yourEmailPlaceholder}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <Input
            type="password"
            placeholder={t.setup.currentPassword}
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
          />
          <Input
            type="password"
            placeholder={t.setup.newPassword}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            minLength={8}
          />
          <Input
            type="password"
            placeholder={t.setup.confirmNewPassword}
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            minLength={8}
          />
          <RememberLoginField
            checked={rememberMe}
            disabled={loading}
            label={t.login.rememberMe}
            onCheckedChange={setRememberMe}
          />
          {error && <p className="text-sm text-red-500">{error}</p>}
          <Button type="submit" className="w-full" disabled={loading}>
            {loading ? t.setup.settingUp : t.setup.completeSetup}
          </Button>
        </form>
      </div>
    </div>
  );
}
