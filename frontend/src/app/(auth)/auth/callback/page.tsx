"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { postAuthRefreshAction } from "@/core/auth/auth-response";
import { useAuth } from "@/core/auth/AuthProvider";
import { safeInternalNextPath } from "@/core/auth/next-path";

export default function AuthCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { refreshUser } = useAuth();
  const [status, setStatus] = useState<
    "loading" | "success" | "error" | "unavailable"
  >("loading");
  const [attempt, setAttempt] = useState(0);
  const next = safeInternalNextPath(searchParams.get("next"));

  useEffect(() => {
    const controller = new AbortController();
    let redirectTimer: ReturnType<typeof setTimeout> | null = null;

    void refreshUser(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      const action = postAuthRefreshAction(result);
      if (action === "complete") {
        setStatus("success");
        redirectTimer = setTimeout(() => router.replace(next), 300);
        return;
      }
      if (action === "retry") {
        setStatus("unavailable");
        return;
      }
      setStatus("error");
      redirectTimer = setTimeout(
        () => router.replace("/login?error=sso_failed"),
        1_500,
      );
    });

    return () => {
      controller.abort();
      if (redirectTimer) clearTimeout(redirectTimer);
    };
  }, [attempt, next, refreshUser, router]);

  return (
    <div className="bg-background relative flex min-h-screen items-center justify-center">
      <div className="text-center">
        {status === "loading" && (
          <>
            <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-current border-t-transparent" />
            <p className="text-muted-foreground">Signing you in...</p>
          </>
        )}
        {status === "success" && (
          <p className="text-muted-foreground">Redirecting...</p>
        )}
        {status === "error" && (
          <p className="text-muted-foreground">
            Authentication failed. Redirecting to login...
          </p>
        )}
        {status === "unavailable" && (
          <div className="space-y-3">
            <p className="text-muted-foreground">
              Authentication service is temporarily unavailable.
            </p>
            <button
              type="button"
              className="rounded-md border px-3 py-2 text-sm"
              onClick={() => {
                setStatus("loading");
                setAttempt((value) => value + 1);
              }}
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
