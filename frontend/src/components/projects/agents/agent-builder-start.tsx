"use client";

import { BotIcon, Loader2Icon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AgentBuilderApiError,
  agentBuilderCanAuthor,
  agentBuilderSemanticSignature,
  agentBuilderSlugError,
  createAgentBuilderIdempotencyRegistry,
  normalizeAgentBuilderSlug,
  useCreateAgentBuilderSession,
} from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";

import { useCurrentProject } from "../project-context";

import { agentBuilderSessionPath } from "./agent-builder-workspace";

export function agentBuilderErrorMessage(error: unknown): string {
  if (!(error instanceof AgentBuilderApiError)) {
    return "Agent 设计服务暂时不可用，请稍后重试。";
  }
  if (error.code === "AGENT_BUILDER_CONFLICT") {
    return "当前项目中已存在同名 Agent，请换一个名字。";
  }
  if (error.code === "AGENT_BUILDER_FORBIDDEN") {
    return "当前账号没有创建 Agent 的权限。";
  }
  if (error.code === "AGENT_BUILDER_NOT_FOUND") {
    return "这个 Agent 设计会话不存在或已结束。";
  }
  if (error.code === "AGENT_BUILDER_VALIDATION_FAILED") {
    return "提交内容不符合要求，请检查后重试。";
  }
  if (error.code === "AGENT_BUILDER_RESPONSE_INVALID") {
    return "模型生成结果格式异常，请重试本次操作。";
  }
  if (error.code === "AGENT_BUILDER_NETWORK_ERROR") {
    return "无法连接 Agent 设计服务，请检查网络后重试。";
  }
  return error.message || "Agent 设计服务暂时不可用，请稍后重试。";
}

export function AgentBuilderStartView({
  name,
  normalizedName,
  errorMessage,
  pending,
  onNameChange,
  onSubmit,
}: {
  name: string;
  normalizedName: string;
  errorMessage: string | null;
  pending: boolean;
  onNameChange: (value: string) => void;
  onSubmit: () => void;
}) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit();
  }

  return (
    <main className="flex min-h-[calc(100svh-3.5rem)] items-center justify-center px-4 py-10 md:min-h-screen">
      <section className="w-full max-w-xl text-center">
        <span className="bg-muted mx-auto flex size-16 items-center justify-center rounded-full">
          <BotIcon aria-hidden className="size-7" />
        </span>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight">
          给新 Agent 起个名字
        </h1>
        <p className="text-muted-foreground mx-auto mt-2 max-w-md text-sm leading-6">
          仅支持字母、数字和连字符，输入内容会自动转为小写（例如
          code-reviewer）。
        </p>

        <form className="mt-8 space-y-3 text-left" onSubmit={submit}>
          <label className="sr-only" htmlFor="agent-builder-name">
            Agent 名称
          </label>
          <Input
            id="agent-builder-name"
            autoCapitalize="none"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            value={name}
            aria-invalid={Boolean(errorMessage)}
            aria-describedby={
              errorMessage
                ? "agent-builder-name-error"
                : normalizedName
                  ? "agent-builder-name-preview"
                  : undefined
            }
            className="h-12 rounded-xl px-4 text-base"
            placeholder="例如 code-reviewer"
            disabled={pending}
            onChange={(event) => onNameChange(event.target.value)}
          />
          {normalizedName ? (
            <p
              id="agent-builder-name-preview"
              className="text-muted-foreground px-1 text-xs"
            >
              将保存为{" "}
              <span className="text-foreground font-mono">
                {normalizedName}
              </span>
            </p>
          ) : null}
          {errorMessage ? (
            <p
              id="agent-builder-name-error"
              role="alert"
              className="text-destructive px-1 text-sm"
            >
              {errorMessage}
            </p>
          ) : null}
          <Button
            type="submit"
            className="min-h-12 w-full rounded-xl"
            disabled={pending || !normalizedName || Boolean(errorMessage)}
          >
            {pending ? (
              <Loader2Icon aria-hidden className="size-4 animate-spin" />
            ) : null}
            {pending ? "正在创建…" : "继续"}
          </Button>
        </form>
      </section>
    </main>
  );
}

export function AgentBuilderStart() {
  const { user } = useAuth();
  const project = useCurrentProject();
  const router = useRouter();
  const [name, setName] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [idempotency] = useState(() => createAgentBuilderIdempotencyRegistry());
  const normalizedName = useMemo(() => normalizeAgentBuilderSlug(name), [name]);
  const localError =
    submitted || name.length > 0 ? agentBuilderSlugError(normalizedName) : null;
  const create = useCreateAgentBuilderSession(user?.id ?? "", project.id);
  const allowed = agentBuilderCanAuthor(project.capabilities);

  function submit() {
    setSubmitted(true);
    if (!user || !allowed || agentBuilderSlugError(normalizedName)) return;
    const signature = agentBuilderSemanticSignature({
      slug: normalizedName,
      display_name: normalizedName,
    });
    const command = idempotency.acquire("create", signature, (key) => ({
      slug: normalizedName,
      display_name: normalizedName,
      idempotency_key: key,
    }));
    create.mutate(command, {
      onSuccess: (response) => {
        idempotency.complete("create", signature);
        router.push(agentBuilderSessionPath(project.slug, response.data.id));
      },
    });
  }

  if (!user) return null;

  return (
    <AgentBuilderStartView
      name={name}
      normalizedName={normalizedName}
      errorMessage={
        !allowed
          ? "当前账号没有创建 Agent 的权限。"
          : (localError ??
            (create.error ? agentBuilderErrorMessage(create.error) : null))
      }
      pending={create.isPending}
      onNameChange={(value) => {
        create.reset();
        setName(value);
      }}
      onSubmit={submit}
    />
  );
}
