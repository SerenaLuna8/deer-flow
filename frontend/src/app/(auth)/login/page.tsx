"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useRef, useState } from "react";

import { RememberLoginField } from "@/components/auth/remember-login-field";
import { ActWeaveLogo } from "@/components/branding/actweave-logo";
import { Button } from "@/components/ui/button";
import { FlickeringGrid } from "@/components/ui/flickering-grid";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/core/auth/AuthProvider";
import { safeInternalNextPath } from "@/core/auth/next-path";
import { restoreSessionThenNavigate } from "@/core/auth/post-auth-navigation";
import {
  fetchAuthProviders,
  type AuthProviderSummary,
} from "@/core/auth/providers";
import {
  loadRememberLoginPreference,
  saveRememberLoginPreference,
} from "@/core/auth/remember-login";
import {
  buildLocalLoginBody,
  buildOidcLoginUrl,
  buildRememberingCredentialPayload,
} from "@/core/auth/remember-payloads";
import {
  AUTH_SUBMIT_TIMEOUT_MS,
  fetchAuth,
  isAbortError,
} from "@/core/auth/request";
import {
  canCreateRegularAccount,
  fetchSetupStatus,
  type SetupStatusResponse,
} from "@/core/auth/setup";
import { parseAuthError } from "@/core/auth/types";
import { useI18n } from "@/core/i18n/hooks";

type SetupStatusPhase = "checking" | "ready" | "unavailable";

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { isAuthenticated, refreshUser } = useAuth();
  const { theme, resolvedTheme } = useTheme();
  const { t } = useI18n();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const [isLogin, setIsLogin] = useState(true);
  const [ssoProviders, setSsoProviders] = useState<AuthProviderSummary[]>([]);
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(
    null,
  );
  const [setupStatusPhase, setSetupStatusPhase] =
    useState<SetupStatusPhase>("checking");

  // Extract error from query params (e.g., ?error=sso_failed)
  const errorParam = searchParams.get("error");
  const [error, setError] = useState(
    errorParam
      ? (t.login.errors[errorParam as keyof typeof t.login.errors] ??
          t.login.authFailed)
      : "",
  );
  // Soft hint shown after a failed login when SSO is configured: an SSO-only
  // account has no local password, so the backend returns a generic
  // "incorrect email or password" (deliberately, to avoid account enumeration).
  // Nudge the user toward the SSO buttons without confirming the account exists.
  const [showSsoHint, setShowSsoHint] = useState(false);
  const [loading, setLoading] = useState(false);
  const localAuthNavigationRef = useRef(false);
  const setupControllerRef = useRef<AbortController | null>(null);
  const submitControllerRef = useRef<AbortController | null>(null);

  // Get next parameter for validated redirect
  const nextParam = searchParams.get("next");
  const redirectPath = safeInternalNextPath(nextParam);
  const regularSignupAllowed = canCreateRegularAccount({
    checked: setupStatusPhase === "ready",
    status: setupStatus,
  });
  const systemNeedsAdminSetup = setupStatus?.needs_setup === true;

  // Redirect if already authenticated (client-side, post-login)
  useEffect(() => {
    if (isAuthenticated && !localAuthNavigationRef.current) {
      router.push(redirectPath);
    }
  }, [isAuthenticated, redirectPath, router]);

  useEffect(() => {
    const preference = loadRememberLoginPreference();
    setRememberMe(preference.rememberMe);
    setEmail(preference.email);
  }, []);

  const checkSetupStatus = useCallback(async () => {
    setupControllerRef.current?.abort();
    const controller = new AbortController();
    setupControllerRef.current = controller;
    setSetupStatusPhase("checking");
    setSetupStatus(null);

    try {
      const data = await fetchSetupStatus(controller.signal);
      if (controller.signal.aborted) return;
      setSetupStatus(data);
      setSetupStatusPhase("ready");
      if (data.needs_setup || !data.registration_enabled) setIsLogin(true);
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      setSetupStatus(null);
      setSetupStatusPhase("unavailable");
    }
  }, []);

  // Fetch setup state and SSO providers with independent bounded requests.
  useEffect(() => {
    void checkSetupStatus();
    const providersController = new AbortController();
    void fetchAuthProviders(providersController.signal)
      .then((providers) => {
        if (!providersController.signal.aborted) setSsoProviders(providers);
      })
      .catch((error: unknown) => {
        if (!providersController.signal.aborted && !isAbortError(error)) {
          setSsoProviders([]);
        }
      });

    return () => {
      setupControllerRef.current?.abort();
      providersController.abort();
      submitControllerRef.current?.abort();
    };
  }, [checkSetupStatus]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setShowSsoHint(false);
    setLoading(true);

    if (!isLogin && !regularSignupAllowed) {
      setError(
        setupStatusPhase === "unavailable"
          ? t.login.registrationUnavailable
          : setupStatus?.needs_setup
            ? t.login.adminSetupRequiredDescription
            : t.login.registrationDisabled,
      );
      setLoading(false);
      return;
    }

    submitControllerRef.current?.abort();
    const controller = new AbortController();
    submitControllerRef.current = controller;

    try {
      const endpoint = isLogin
        ? "/api/v1/auth/login/local"
        : "/api/v1/auth/register";
      const body = isLogin
        ? buildLocalLoginBody({ email, password, rememberMe })
        : JSON.stringify(
            buildRememberingCredentialPayload({
              email,
              password,
              rememberMe,
            }),
          );

      const headers: HeadersInit = isLogin
        ? { "Content-Type": "application/x-www-form-urlencoded" }
        : { "Content-Type": "application/json" };

      const res = await fetchAuth(
        endpoint,
        {
          method: "POST",
          headers,
          body,
          signal: controller.signal,
        },
        AUTH_SUBMIT_TIMEOUT_MS,
      );

      if (!res.ok) {
        const data: unknown = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        // On a failed login with SSO configured, surface a hint pointing at the
        // SSO buttons — the "wrong password" may really mean "this is an SSO account".
        if (isLogin && ssoProviders.length > 0) {
          setShowSsoHint(true);
        }
        return;
      }

      saveRememberLoginPreference({ email, rememberMe });

      // Both login and register set an HttpOnly cookie. Restore that identity
      // in the currently mounted provider, then cross the auth boundary with a
      // document navigation. App Router can otherwise retain an anonymous
      // layout while returning to /invite, preventing redemption until reload.
      localAuthNavigationRef.current = true;
      const navigated = await restoreSessionThenNavigate(refreshUser, () =>
        window.location.replace(redirectPath),
      );
      if (!navigated) {
        localAuthNavigationRef.current = false;
        setError(t.login.networkError);
      }
    } catch (error) {
      if (controller.signal.aborted || isAbortError(error)) return;
      localAuthNavigationRef.current = false;
      setError(t.login.networkError);
    } finally {
      if (submitControllerRef.current === controller) {
        submitControllerRef.current = null;
        setLoading(false);
      }
    }
  };

  const actualTheme = theme === "system" ? resolvedTheme : theme;

  return (
    <div className="bg-background relative flex min-h-screen items-center justify-center overflow-x-hidden overflow-y-auto">
      <FlickeringGrid
        className="absolute inset-0 z-0 [mask-image:radial-gradient(circle_at_center,black_0%,transparent_72%)]"
        squareSize={4}
        gridGap={4}
        color={actualTheme === "dark" ? "white" : "black"}
        maxOpacity={0.3}
        flickerChance={0.25}
      />
      <div className="border-border/20 bg-background/5 relative z-10 w-full max-w-md space-y-6 rounded-3xl border p-8 backdrop-blur-sm">
        <div className="text-center">
          <ActWeaveLogo className="mx-auto mb-4" priority />
          <h1 className="text-foreground font-serif text-3xl">ActWeave</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Weave intelligence into action.
          </p>
          <p className="text-muted-foreground mt-3">
            {isLogin ? t.login.signInTitle : t.login.createAccountTitle}
          </p>
        </div>

        {systemNeedsAdminSetup && (
          <div className="border-l-2 border-blue-500 ps-3 text-sm">
            <p className="font-medium">{t.login.adminSetupRequiredTitle}</p>
            <p className="text-muted-foreground mt-1">
              {t.login.adminSetupRequiredDescription}
            </p>
            <Link
              href="/setup"
              className="mt-2 inline-block font-medium text-blue-500 hover:underline"
            >
              {t.login.createAdminAccount}
            </Link>
          </div>
        )}

        {setupStatusPhase === "checking" && (
          <p className="text-muted-foreground text-center text-sm">
            {t.login.checkingRegistration}
          </p>
        )}

        {setupStatusPhase === "unavailable" && (
          <div
            role="status"
            className="border-border bg-background/70 flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm"
          >
            <span className="text-muted-foreground">
              {t.login.registrationUnavailable}
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void checkSetupStatus()}
            >
              {t.login.retry}
            </Button>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-2">
          <div className="flex flex-col space-y-1">
            <label htmlFor="email" className="text-sm font-medium">
              {t.login.email}
            </label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={t.login.emailPlaceholder}
              required
            />
          </div>
          <div className="flex flex-col space-y-1">
            <label htmlFor="password" className="text-sm font-medium">
              {t.login.password}
            </label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t.login.passwordPlaceholder}
              required
              minLength={isLogin ? 6 : 8}
            />
          </div>

          <RememberLoginField
            checked={rememberMe}
            disabled={loading}
            label={t.login.rememberMe}
            onCheckedChange={setRememberMe}
          />

          {error && <p className="text-sm text-red-500">{error}</p>}

          <Button type="submit" className="w-full" disabled={loading}>
            {loading
              ? t.login.pleaseWait
              : isLogin
                ? t.login.signIn
                : t.login.createAccount}
          </Button>
        </form>

        {ssoProviders.length > 0 && (
          <div className="space-y-2">
            {isLogin && (
              <div className="relative my-4">
                <div className="absolute inset-0 flex items-center">
                  <span className="w-full border-t" />
                </div>
                <div className="relative flex justify-center text-xs uppercase">
                  <span className="bg-background text-muted-foreground px-2">
                    {t.login.orContinueWith}
                  </span>
                </div>
              </div>
            )}
            {showSsoHint && (
              <p className="text-muted-foreground text-center text-sm">
                {t.login.ssoHint}
              </p>
            )}
            {ssoProviders.map((provider) => (
              <Button
                key={provider.id}
                type="button"
                variant="outline"
                className="w-full"
                disabled={loading}
                onClick={() => {
                  saveRememberLoginPreference({ email, rememberMe });
                  window.location.href = buildOidcLoginUrl({
                    next: redirectPath,
                    providerId: provider.id,
                    rememberMe,
                  });
                }}
              >
                {t.login.continueWith(provider.display_name)}
              </Button>
            ))}
          </div>
        )}

        {regularSignupAllowed && (
          <div className="text-center text-sm">
            <button
              type="button"
              onClick={() => {
                setIsLogin(!isLogin);
                setError("");
                setShowSsoHint(false);
              }}
              className="text-blue-500 hover:underline"
            >
              {isLogin ? t.login.noAccountSignUp : t.login.haveAccountSignIn}
            </button>
          </div>
        )}

        {setupStatusPhase === "ready" &&
          setupStatus?.needs_setup === false &&
          !setupStatus.registration_enabled && (
            <p className="text-muted-foreground text-center text-xs">
              {t.login.registrationDisabled}
            </p>
          )}
      </div>
    </div>
  );
}
