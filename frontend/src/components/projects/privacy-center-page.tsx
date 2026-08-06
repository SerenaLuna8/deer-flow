"use client";

import {
  ArrowLeftIcon,
  CalendarClockIcon,
  DownloadIcon,
  FolderLockIcon,
  LoaderCircleIcon,
  ShieldCheckIcon,
  Trash2Icon,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  WorkspaceBody,
  WorkspaceContainer,
} from "@/components/workspace/workspace-container";
import { useI18n } from "@/core/i18n/hooks";
import {
  PrivacyCenterApiError,
  privacyExportURL,
} from "@/core/privacy-center/api";
import {
  usePrivacyCases,
  usePrivacyEarlyDelete,
} from "@/core/privacy-center/hooks";
import type { PrivacyCase } from "@/core/privacy-center/types";

export function formatPrivacyDeadline(value: string, locale: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function privacyRemainingDays(
  value: string,
  now: Date = new Date(),
): number {
  const deadline = new Date(value).getTime();
  if (!Number.isFinite(deadline)) return 0;
  return Math.max(0, Math.ceil((deadline - now.getTime()) / 86_400_000));
}

function privacyErrorMessage(error: unknown): string {
  if (!(error instanceof PrivacyCenterApiError)) {
    return "隐私服务暂时不可用，请稍后重试。";
  }
  switch (error.code) {
    case "PRIVACY_CASE_NOT_FOUND":
      return "这项保留记录已失效，数据可能已删除或你已重新加入项目。";
    case "DATABASE_UNAVAILABLE":
    case "PRIVACY_NETWORK_ERROR":
      return "隐私服务暂时不可用，请稍后重试。";
    default:
      return "请求未完成，请刷新页面后重试。";
  }
}

export function PrivacyCaseCard({
  privacyCase,
  exporting,
  deleting = false,
  locale = "zh-CN",
  now = new Date(),
  onExport,
  onEarlyDelete,
}: {
  privacyCase: PrivacyCase;
  exporting: boolean;
  deleting?: boolean;
  locale?: string;
  now?: Date;
  onExport: () => void | Promise<void>;
  onEarlyDelete: () => void | Promise<void>;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const remainingDays = privacyRemainingDays(
    privacyCase.deletion_deadline,
    now,
  );
  const confirmed = confirmation === privacyCase.project_display_name;
  const deletionRequested = privacyCase.early_delete_requested || deleting;

  async function submitEarlyDelete() {
    if (!confirmed || deletionRequested) return;
    await onEarlyDelete();
    setConfirmation("");
    setDialogOpen(false);
  }

  return (
    <Card data-testid={`privacy-case-${privacyCase.project_id}`}>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 space-y-2">
            <CardTitle className="truncate text-lg">
              {privacyCase.project_display_name}
            </CardTitle>
            <CardDescription className="font-mono text-xs">
              {privacyCase.project_slug}
            </CardDescription>
          </div>
          <Badge variant="outline">
            {privacyCase.retention_kind === "project"
              ? "项目删除优先"
              : privacyCase.membership_status === "removed"
                ? "已被移出"
                : "已退出"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="bg-muted/50 flex gap-3 rounded-lg p-4">
          <CalendarClockIcon
            aria-hidden
            className="text-muted-foreground mt-0.5 size-5 shrink-0"
          />
          <div className="min-w-0">
            <p className="text-sm font-medium">
              {remainingDays > 0 ? `剩余 ${remainingDays} 天` : "即将执行删除"}
            </p>
            <p className="text-muted-foreground mt-1 text-sm">
              计划删除：
              <time dateTime={privacyCase.deletion_deadline}>
                {formatPrivacyDeadline(privacyCase.deletion_deadline, locale)}
              </time>
            </p>
          </div>
        </div>
        <p className="text-muted-foreground text-sm leading-6">
          在截止时间前，你可以导出这个项目中归属于自己的冻结数据。提前删除一旦执行无法撤销。
        </p>
      </CardContent>
      <CardFooter className="flex flex-col items-stretch gap-2 border-t sm:flex-row sm:justify-end">
        <Button
          type="button"
          variant="outline"
          disabled={exporting || deletionRequested}
          onClick={() => void onExport()}
        >
          {exporting ? (
            <LoaderCircleIcon aria-hidden className="animate-spin" />
          ) : (
            <DownloadIcon aria-hidden />
          )}
          {exporting ? "正在准备导出" : "导出我的数据"}
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={deletionRequested}
          onClick={() => setDialogOpen(true)}
        >
          {deleting ? (
            <LoaderCircleIcon aria-hidden className="animate-spin" />
          ) : (
            <Trash2Icon aria-hidden />
          )}
          {deletionRequested ? "删除请求已提交" : "提前删除"}
        </Button>
      </CardFooter>

      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (deleting) return;
          setDialogOpen(open);
          if (!open) setConfirmation("");
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认提前删除个人数据</DialogTitle>
            <DialogDescription>
              该操作会提交立即执行的持久化清理任务。执行后无法恢复，也无法再导出。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <label
              htmlFor={`privacy-confirm-${privacyCase.project_id}`}
              className="text-sm"
            >
              输入项目名称
              <span className="font-semibold">
                {` ${privacyCase.project_display_name} `}
              </span>
              以确认
            </label>
            <Input
              id={`privacy-confirm-${privacyCase.project_id}`}
              autoComplete="off"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={deleting}
              onClick={() => setDialogOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={!confirmed || deleting}
              onClick={() => void submitEarlyDelete()}
            >
              {deleting ? "正在提交" : "永久删除我的数据"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function startStreamingDownload(url: string) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "";
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

function PrivacyCenterContent({ accountId }: { accountId: string }) {
  const casesQuery = usePrivacyCases(accountId);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(
    null,
  );
  const [message, setMessage] = useState<string | null>(null);
  function handleExport(privacyCase: PrivacyCase) {
    setMessage(null);
    try {
      startStreamingDownload(
        privacyExportURL(accountId, privacyCase.project_id),
      );
      setMessage(
        `“${privacyCase.project_display_name}”的流式数据导出已开始下载。`,
      );
    } catch (error) {
      setMessage(privacyErrorMessage(error));
    }
  }

  if (casesQuery.isLoading) {
    return (
      <div className="grid gap-5 lg:grid-cols-2" aria-label="正在加载隐私记录">
        <Skeleton className="h-80 rounded-xl" />
        <Skeleton className="h-80 rounded-xl" />
      </div>
    );
  }

  if (casesQuery.error) {
    return (
      <div
        role="alert"
        className="border-destructive/30 bg-destructive/5 rounded-xl border p-6"
      >
        <h2 className="font-semibold">隐私记录加载失败</h2>
        <p className="text-muted-foreground mt-2 text-sm">
          {privacyErrorMessage(casesQuery.error)}
        </p>
        <Button
          type="button"
          className="mt-4"
          variant="outline"
          onClick={() => void casesQuery.refetch()}
        >
          重试
        </Button>
      </div>
    );
  }

  const privacyCases = casesQuery.data ?? [];
  if (privacyCases.length === 0) {
    return (
      <div className="bg-card rounded-xl border px-6 py-14 text-center shadow-sm">
        <ShieldCheckIcon
          aria-hidden
          className="text-muted-foreground mx-auto size-10"
        />
        <h2 className="mt-4 text-lg font-semibold">当前没有待处理的个人数据</h2>
        <p className="text-muted-foreground mx-auto mt-2 max-w-lg text-sm leading-6">
          当你退出项目或被移出项目后，这里会显示保留期限、导出和提前删除选项。
        </p>
      </div>
    );
  }

  return (
    <>
      {message ? (
        <div
          role="status"
          className="bg-muted mb-5 rounded-lg border px-4 py-3 text-sm"
        >
          {message}
        </div>
      ) : null}
      <div className="grid gap-5 lg:grid-cols-2">
        {privacyCases.map((privacyCase) => (
          <PrivacyCaseMutationCard
            key={privacyCase.project_id}
            accountId={accountId}
            privacyCase={privacyCase}
            deleting={deletingProjectId === privacyCase.project_id}
            onExport={() => handleExport(privacyCase)}
            onDeletingChange={(deleting) =>
              setDeletingProjectId(deleting ? privacyCase.project_id : null)
            }
            onMessage={setMessage}
          />
        ))}
      </div>
    </>
  );
}

function PrivacyCaseMutationCard({
  accountId,
  privacyCase,
  deleting,
  onExport,
  onDeletingChange,
  onMessage,
}: {
  accountId: string;
  privacyCase: PrivacyCase;
  deleting: boolean;
  onExport: () => void;
  onDeletingChange: (deleting: boolean) => void;
  onMessage: (message: string) => void;
}) {
  const earlyDelete = usePrivacyEarlyDelete(accountId, privacyCase.project_id);
  const { locale } = useI18n();

  async function handleEarlyDelete() {
    onMessage("");
    onDeletingChange(true);
    try {
      await earlyDelete.mutateAsync();
      onMessage(
        `“${privacyCase.project_display_name}”的删除请求已提交，后台任务将尽快执行。`,
      );
    } catch (error) {
      onMessage(privacyErrorMessage(error));
      throw error;
    } finally {
      onDeletingChange(false);
    }
  }

  return (
    <PrivacyCaseCard
      privacyCase={privacyCase}
      exporting={false}
      deleting={deleting}
      locale={locale}
      onExport={onExport}
      onEarlyDelete={handleEarlyDelete}
    />
  );
}

export function PrivacyCenterPage({ accountId }: { accountId: string }) {
  return (
    <WorkspaceContainer data-testid="privacy-center-page">
      <header className="flex h-16 shrink-0 items-center border-b px-4 sm:px-6">
        <Button asChild variant="ghost" size="sm">
          <Link href="/workspace">
            <ArrowLeftIcon aria-hidden />
            返回工作空间
          </Link>
        </Button>
      </header>
      <WorkspaceBody className="overflow-y-auto">
        <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
          <div className="mb-8 max-w-3xl">
            <div className="text-primary mb-2 flex items-center gap-2 text-sm font-medium">
              <FolderLockIcon aria-hidden className="size-5" />
              账户隐私
            </div>
            <h1 className="text-3xl font-semibold tracking-tight">
              个人数据中心
            </h1>
            <p className="text-muted-foreground mt-3 leading-7">
              查看你已退出或被移出项目后仍处于 30
              天保留期内的数据。你可以在自动清理前导出自己的数据，或提交提前删除请求。
            </p>
          </div>

          <div className="border-primary/20 bg-primary/5 mb-8 flex gap-3 rounded-xl border p-5">
            <ShieldCheckIcon
              aria-hidden
              className="text-primary mt-0.5 size-5 shrink-0"
            />
            <div className="text-sm leading-6">
              <p className="font-medium">删除由后台持久化任务执行</p>
              <p className="text-muted-foreground mt-1">
                即使定时调度服务未启用，到期任务也会由 Worker
                执行。重新加入项目或项目恢复时，过期的删除任务会失效并重新校验。
              </p>
            </div>
          </div>

          <PrivacyCenterContent accountId={accountId} />
        </main>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
