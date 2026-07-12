"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  useClaimProjectInvitation,
  useRedeemProjectInvitation,
} from "@/core/projects/hooks";
import type { RedeemedProjectInvitation } from "@/core/projects/types";

type RedemptionState =
  | { status: "preparing" }
  | { status: "sign_in" }
  | { status: "redeeming" }
  | { status: "success"; result: RedeemedProjectInvitation }
  | { status: "error" };

export function InvitationRedemption() {
  const { user } = useAuth();
  const router = useRouter();
  const claim = useClaimProjectInvitation();
  const redeem = useRedeemProjectInvitation(user?.id);
  const { mutate: claimInvitation } = claim;
  const { mutate: redeemInvitation } = redeem;
  const handledRef = useRef(false);
  const effectGenerationRef = useRef(0);
  const fragmentTokenRef = useRef<string | null | undefined>(undefined);
  const [state, setState] = useState<RedemptionState>({ status: "preparing" });

  const redeemClaim = useCallback(() => {
    if (!user) {
      setState({ status: "sign_in" });
      router.replace("/login?next=%2Finvite");
      return;
    }
    setState({ status: "redeeming" });
    redeemInvitation(undefined, {
      onSuccess: ({ data: result }) => setState({ status: "success", result }),
      onError: () => setState({ status: "error" }),
    });
  }, [redeemInvitation, router, user]);

  useEffect(() => {
    const generation = effectGenerationRef.current + 1;
    effectGenerationRef.current = generation;
    if (fragmentTokenRef.current === undefined) {
      const fragment = window.location.hash.slice(1);
      fragmentTokenRef.current = new URLSearchParams(fragment).get("token");
      window.history.replaceState(null, "", window.location.pathname);
    }

    queueMicrotask(() => {
      if (effectGenerationRef.current !== generation || handledRef.current)
        return;
      handledRef.current = true;
      const token = fragmentTokenRef.current;
      if (!token) {
        if (user) redeemClaim();
        else setState({ status: "error" });
        return;
      }
      claimInvitation(
        { token },
        {
          onSuccess: () =>
            effectGenerationRef.current === generation && redeemClaim(),
          onError: () =>
            effectGenerationRef.current === generation &&
            setState({ status: "error" }),
        },
      );
    });
    return () => {
      if (effectGenerationRef.current === generation) {
        effectGenerationRef.current += 1;
      }
    };
  }, [claimInvitation, redeemClaim, user]);

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
              邀请不可用或已失效，请向项目管理员索取新链接。
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
