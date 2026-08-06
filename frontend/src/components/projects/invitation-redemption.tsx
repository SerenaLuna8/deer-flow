"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { ProjectApiError } from "@/core/projects/api";
import {
  useClaimProjectInvitation,
  useRedeemProjectInvitation,
} from "@/core/projects/hooks";
import type { RedeemedProjectInvitation } from "@/core/projects/types";

import {
  createInvitationRedemptionCoordinator,
  type InvitationRedemptionAttempt,
} from "./invitation-redemption-state";
import { projectErrorMessage } from "./project-view-model";

const INVITATION_UNAVAILABLE_MESSAGE =
  "邀请不可用或已失效，请向项目管理员索取新链接。";

type RedemptionState =
  | { status: "preparing" }
  | { status: "sign_in" }
  | { status: "redeeming" }
  | { status: "success"; result: RedeemedProjectInvitation }
  | { status: "error"; message: string };

function invitationRedemptionErrorMessage(error: unknown): string {
  if (
    error instanceof ProjectApiError &&
    error.code === "PROJECT_MEMBER_QUOTA_EXCEEDED"
  ) {
    return projectErrorMessage(error);
  }
  return INVITATION_UNAVAILABLE_MESSAGE;
}

function consumeInvitationFragmentToken() {
  const fragment = window.location.hash.slice(1);
  const token = new URLSearchParams(fragment).get("token");
  if (fragment) {
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}`,
    );
  }
  return token;
}

export function InvitationRedemption() {
  const { user } = useAuth();
  const router = useRouter();
  const claim = useClaimProjectInvitation();
  const redeem = useRedeemProjectInvitation(user?.id);
  const { mutate: claimInvitation } = claim;
  const { reset: resetClaim } = claim;
  const { mutate: redeemInvitation } = redeem;
  const { reset: resetRedeem } = redeem;
  const handledRef = useRef(false);
  const handledUserRef = useRef<string | null>(user?.id ?? null);
  const currentUserRef = useRef<string | null>(user?.id ?? null);
  const coordinatorRef = useRef(createInvitationRedemptionCoordinator());
  const activeAttemptRef = useRef<InvitationRedemptionAttempt | null>(null);
  const [state, setState] = useState<RedemptionState>({ status: "preparing" });
  currentUserRef.current = user?.id ?? null;

  const redeemClaim = useCallback(
    (attempt: InvitationRedemptionAttempt) => {
      if (!coordinatorRef.current.isCurrent(attempt, currentUserRef.current)) {
        return;
      }
      if (!user) {
        setState({ status: "sign_in" });
        router.replace("/login?next=%2Finvite");
        return;
      }
      setState({ status: "redeeming" });
      redeemInvitation(undefined, {
        onSuccess: (result) => {
          if (
            coordinatorRef.current.isCurrent(attempt, currentUserRef.current)
          ) {
            setState({ status: "success", result });
          }
          queueMicrotask(resetRedeem);
        },
        onError: (error) => {
          if (
            coordinatorRef.current.isCurrent(attempt, currentUserRef.current)
          ) {
            setState({
              status: "error",
              message: invitationRedemptionErrorMessage(error),
            });
          }
          queueMicrotask(resetRedeem);
        },
      });
    },
    [redeemInvitation, resetRedeem, router, user],
  );

  const beginAttempt = useCallback(
    (token: string | null) => {
      const coordinator = coordinatorRef.current;
      const previousAttempt = activeAttemptRef.current;
      if (previousAttempt) {
        coordinator.dispose(previousAttempt);
      }
      const attempt = coordinator.begin(currentUserRef.current);
      activeAttemptRef.current = attempt;
      setState({ status: "preparing" });

      queueMicrotask(() => {
        if (!coordinator.isCurrent(attempt, currentUserRef.current)) {
          return;
        }
        if (!token) {
          if (currentUserRef.current) redeemClaim(attempt);
          else
            setState({
              status: "error",
              message: INVITATION_UNAVAILABLE_MESSAGE,
            });
          return;
        }
        claimInvitation(
          { token },
          {
            onSuccess: () => {
              if (
                coordinatorRef.current.isCurrent(
                  attempt,
                  currentUserRef.current,
                )
              ) {
                redeemClaim(attempt);
              }
              queueMicrotask(resetClaim);
            },
            onError: () => {
              if (
                coordinatorRef.current.isCurrent(
                  attempt,
                  currentUserRef.current,
                )
              ) {
                setState({
                  status: "error",
                  message: INVITATION_UNAVAILABLE_MESSAGE,
                });
              }
              queueMicrotask(resetClaim);
            },
          },
        );
      });
    },
    [claimInvitation, redeemClaim, resetClaim],
  );

  useEffect(() => {
    const userId = user?.id ?? null;
    if (handledUserRef.current !== userId) {
      handledUserRef.current = userId;
      handledRef.current = false;
    }
    if (handledRef.current) return;
    handledRef.current = true;
    beginAttempt(consumeInvitationFragmentToken());
  }, [beginAttempt, user?.id]);

  useEffect(() => {
    const handleHashChange = () => {
      const token = consumeInvitationFragmentToken();
      if (!token) return;
      resetClaim();
      resetRedeem();
      beginAttempt(token);
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, [beginAttempt, resetClaim, resetRedeem]);

  useEffect(
    () => () => {
      const attempt = activeAttemptRef.current;
      if (attempt) coordinatorRef.current.dispose(attempt);
    },
    [],
  );

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-lg items-center px-6 py-12">
      <section className="border-border/70 bg-card w-full rounded-3xl border p-8 text-center shadow-sm">
        {state.status === "preparing" || state.status === "redeeming" ? (
          <>
            <h1 className="text-2xl font-semibold">正在处理项目邀请</h1>
            <p className="text-muted-foreground mt-3 text-sm">
              请稍候，不要关闭此页面。
            </p>
            <Skeleton className="mx-auto mt-6 h-2 w-40" />
          </>
        ) : state.status === "sign_in" ? (
          <>
            <h1 className="text-2xl font-semibold">请登录后接受邀请</h1>
            <p className="text-muted-foreground mt-3 text-sm">
              登录后会自动返回并继续接受邀请。
            </p>
            <Button asChild className="mt-6">
              <Link href="/login?next=%2Finvite">前往登录</Link>
            </Button>
          </>
        ) : state.status === "success" ? (
          <>
            <h1 className="text-2xl font-semibold">邀请已接受</h1>
            <p className="text-muted-foreground mt-3 text-sm">
              你现在可以进入项目。
            </p>
            <Button asChild className="mt-6">
              <Link
                href={`/projects/${encodeURIComponent(state.result.project_slug)}`}
              >
                进入项目
              </Link>
            </Button>
          </>
        ) : (
          <>
            <h1 className="text-2xl font-semibold">无法接受邀请</h1>
            <p role="alert" className="text-muted-foreground mt-3 text-sm">
              {state.message}
            </p>
            <Button asChild variant="outline" className="mt-6">
              <Link href="/workspace">返回工作空间</Link>
            </Button>
          </>
        )}
      </section>
    </main>
  );
}
