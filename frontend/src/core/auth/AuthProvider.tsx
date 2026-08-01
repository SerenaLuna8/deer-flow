"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter, usePathname } from "next/navigation";
import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";

import { isStaticWebsiteOnly } from "../static-mode";

import { transitionAccountQueries } from "./account-query-client";
import {
  classifyAuthMeResponse,
  type AuthMeResult,
} from "./auth-response";
import {
  createAuthIdentityCoordinator,
  type AuthIdentityCoordinator,
} from "./identity-coordinator";
import {
  currentBrowserReturnPath,
  isPrivateRoutePath,
} from "./private-return-path";
import {
  AUTH_PROBE_TIMEOUT_MS,
  AUTH_SUBMIT_TIMEOUT_MS,
  fetchAuth,
  fetchWithAuthTimeout,
} from "./request";
import { type User, buildLoginUrl } from "./types";

// Re-export for consumers
export type { User };

/**
 * Authentication context provided to consuming components
 */
interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  logout: () => Promise<void>;
  refreshUser: (signal?: AbortSignal) => Promise<AuthMeResult | null>;
  applyUser: (user: User | null) => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

interface AuthProviderProps {
  children: ReactNode;
  initialUser: User | null;
}

/**
 * AuthProvider - Unified authentication context for the application
 *
 * Per RFC-001:
 * - Only holds display information (user), never JWT or tokens
 * - initialUser comes from server-side guard, avoiding client flicker
 * - Provides logout and refresh capabilities
 */
export function AuthProvider({ children, initialUser }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(initialUser);
  const [isLoading, setIsLoading] = useState(false);
  const queryClient = useQueryClient();
  const router = useRouter();
  const staticMode = isStaticWebsiteOnly();
  const identityCoordinatorRef = React.useRef<AuthIdentityCoordinator | null>(
    null,
  );
  identityCoordinatorRef.current ??=
    createAuthIdentityCoordinator(setIsLoading);
  const identityCoordinator = identityCoordinatorRef.current;

  const isAuthenticated = user !== null;

  /**
   * Apply a user value supplied by a caller (e.g. banner probe) that has
   * already fetched it. Equivalent to setUser, exposed with a stable name
   * so consumers don't reach into React internals.
   */
  const userRef = React.useRef<User | null>(initialUser);
  const applyAtGeneration = useCallback(
    async (generation: number, next: User | null) => {
      const previousId = userRef.current?.id ?? null;
      const nextId = next?.id ?? null;
      const previousSystemRole = userRef.current?.system_role ?? null;
      const nextSystemRole = next?.system_role ?? null;
      return identityCoordinator.commitAtGeneration(
        generation,
        () =>
          transitionAccountQueries(queryClient, previousId, nextId, {
            previousSystemRole,
            nextSystemRole,
          }),
        () => {
          userRef.current = next;
          setUser(next);
        },
      );
    },
    [identityCoordinator, queryClient],
  );
  const applyUser = useCallback(
    async (next: User | null) => {
      const generation = identityCoordinator.beginIdentityChange();
      await applyAtGeneration(generation, next);
    },
    [applyAtGeneration, identityCoordinator],
  );

  /**
   * Fetch current user from FastAPI
   * Used when initialUser might be stale (e.g., after tab was inactive)
   */
  const refreshUser = useCallback(
    async (externalSignal?: AbortSignal) => {
      if (staticMode) {
        const currentUser = userRef.current;
        return currentUser
          ? ({ type: "authenticated", user: currentUser } as const)
          : ({ type: "unauthenticated" } as const);
      }

      const attempt = identityCoordinator.startRefresh();
      const abortFromCaller = () =>
        attempt.controller.abort(externalSignal?.reason);
      if (externalSignal?.aborted) {
        abortFromCaller();
      } else {
        externalSignal?.addEventListener("abort", abortFromCaller, {
          once: true,
        });
      }
      try {
        const res = await fetchAuth(
          "/api/v1/auth/me",
          {
            cache: "no-store",
            signal: attempt.signal,
          },
          AUTH_PROBE_TIMEOUT_MS,
        );
        if (!identityCoordinator.isCurrent(attempt)) return null;

        const result = await classifyAuthMeResponse(res);
        if (!identityCoordinator.isCurrent(attempt)) return null;
        if (result.type === "authenticated") {
          const applied = await applyAtGeneration(
            attempt.generation,
            result.user,
          );
          return applied ? result : null;
        }
        if (result.type === "unauthenticated") {
          const applied = await applyAtGeneration(attempt.generation, null);
          if (
            applied &&
            typeof window !== "undefined" &&
            isPrivateRoutePath(window.location.pathname)
          ) {
            router.push(
              buildLoginUrl(currentBrowserReturnPath(window.location)),
            );
          }
          return applied ? result : null;
        }
        console.warn(
          "[auth] Retaining the current identity after an unavailable /auth/me response.",
        );
        return result;
      } catch (err) {
        if (
          !identityCoordinator.isCurrent(attempt) ||
          attempt.signal.aborted
        ) {
          return null;
        }
        console.warn(
          "[auth] Retaining the current identity after /auth/me failed:",
          err,
        );
        return { type: "unavailable" } as const;
      } finally {
        externalSignal?.removeEventListener("abort", abortFromCaller);
        identityCoordinator.finishRefresh(attempt);
      }
    },
    [staticMode, router, applyAtGeneration, identityCoordinator],
  );

  /**
   * Logout - call FastAPI logout endpoint and clear local state
   * Per RFC-001: Immediately clear local state, don't wait for server confirmation
   *
   * When the gateway is unreachable the fetch silently fails — the SPA
   * router.push("/") would leave the user on "/" still holding stale
   * React state and any in-flight SSE / fetch / query subscriptions.
   * We therefore fall back to a hard navigation (window.location.href),
   * which discards all client state the same way the legacy form-POST
   * logout used to.
   */
  const logout = useCallback(async () => {
    identityCoordinator.beginIdentityChange();
    const previousId = userRef.current?.id ?? null;
    userRef.current = null;
    void transitionAccountQueries(queryClient, previousId, null, {
      force: true,
    });
    setUser(null);

    if (staticMode) {
      router.push("/");
      return;
    }

    let logoutFailed = false;
    try {
      const res = await fetchWithAuthTimeout(
        fetchWithAuth,
        "/api/v1/auth/logout",
        {
          method: "POST",
          credentials: "include",
        },
        AUTH_SUBMIT_TIMEOUT_MS,
      );
      if (!res.ok) logoutFailed = true;
    } catch (err) {
      console.error("Logout request failed:", err);
      logoutFailed = true;
    }

    if (logoutFailed && typeof window !== "undefined") {
      // Hard navigation ensures every in-flight subscription is torn down,
      // matching the legacy form-POST logout behaviour during a gateway outage.
      window.location.href = "/";
      return;
    }

    // Redirect to home page
    router.push("/");
  }, [staticMode, router, queryClient, identityCoordinator]);

  useEffect(() => {
    identityCoordinator.activate();
    return () => identityCoordinator.dispose();
  }, [identityCoordinator]);

  /**
   * Handle visibility change - refresh user when tab becomes visible again.
   * Throttled to at most once per 60 s to avoid spamming the backend on rapid tab switches.
   */
  const lastCheckRef = React.useRef(0);

  useEffect(() => {
    if (staticMode) return;

    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible" || user === null) return;
      const now = Date.now();
      if (now - lastCheckRef.current < 60_000) return;
      lastCheckRef.current = now;
      void refreshUser();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [staticMode, user, refreshUser]);

  const value: AuthContextType = {
    user,
    isAuthenticated,
    isLoading,
    logout,
    refreshUser,
    applyUser,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * Hook to access authentication context
 * Throws if used outside AuthProvider - this is intentional for proper usage
 */
export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}

/**
 * Hook to require authentication - redirects to login if not authenticated
 * Useful for client-side checks in addition to server-side guards
 */
export function useRequireAuth(): AuthContextType {
  const auth = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (isStaticWebsiteOnly()) return;

    // Only redirect if we're sure user is not authenticated (not just loading)
    if (!auth.isLoading && !auth.isAuthenticated) {
      const returnPath =
        typeof window === "undefined"
          ? pathname || "/workspace"
          : currentBrowserReturnPath(window.location);
      router.push(buildLoginUrl(returnPath));
    }
  }, [auth.isAuthenticated, auth.isLoading, router, pathname]);

  return auth;
}
