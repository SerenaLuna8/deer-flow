"use client";

import { Loader2Icon, SparklesIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/core/auth/AuthProvider";
import {
  SkillBuilderApiError,
  createSkillBuilderIdempotencyRegistry,
  normalizeSkillBuilderSlug,
  skillBuilderCanAuthor,
  skillBuilderSemanticSignature,
  skillBuilderSessionPath,
  skillBuilderSlugError,
  useCreateSkillBuilderSession,
} from "@/core/skill-builder";

import { useCurrentProject } from "../project-context";

export function skillBuilderErrorMessage(error: unknown): string {
  if (!(error instanceof SkillBuilderApiError)) {
    return "Skill 设计服务暂时不可用，请稍后重试。";
  }
  if (error.code === "SKILL_BUILDER_CONFLICT") {
    return "当前项目中已存在同名 Skill，请换一个名字。";
  }
  if (error.code === "SKILL_BUILDER_FORBIDDEN") {
    return "当前账号没有创建 Skill 的权限。";
  }
  if (error.code === "SKILL_BUILDER_NOT_FOUND") {
    return "这个 Skill 设计会话不存在或已结束。";
  }
  if (error.code === "SKILL_BUILDER_LIMIT_EXCEEDED") {
    return "未完成的 Skill 设计会话已达到上限，请先继续或放弃一个已有会话。";
  }
  if (error.code === "SKILL_BUILDER_VALIDATION_FAILED") {
    return "候选文件未通过检查，请修复后重试。";
  }
  if (error.code === "SKILL_BUILDER_RESPONSE_INVALID") {
    return "Skill 设计服务返回了异常结果，请重试。";
  }
  if (error.code === "SKILL_BUILDER_NETWORK_ERROR") {
    return "无法连接 Skill 设计服务，请检查网络后重试。";
  }
  return error.message || "Skill 设计服务暂时不可用，请稍后重试。";
}

export function SkillBuilderStartView({
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
          <SparklesIcon aria-hidden className="size-7" />
        </span>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight">
          给新 Skill 起个名字
        </h1>
        <p className="text-muted-foreground mx-auto mt-2 max-w-md text-sm leading-6">
          名称会成为 SKILL.md frontmatter 中不可变的
          name，并自动转为小写连字符格式。
        </p>

        <form className="mt-8 space-y-3 text-left" onSubmit={submit}>
          <label className="sr-only" htmlFor="skill-builder-name">
            Skill 名称
          </label>
          <Input
            id="skill-builder-name"
            autoCapitalize="none"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            value={name}
            aria-invalid={Boolean(errorMessage)}
            aria-describedby={
              errorMessage
                ? "skill-builder-name-error"
                : normalizedName
                  ? "skill-builder-name-preview"
                  : undefined
            }
            className="h-12 rounded-xl px-4 text-base"
            placeholder="例如 paper-review"
            disabled={pending}
            onChange={(event) => onNameChange(event.target.value)}
          />
          {normalizedName ? (
            <p
              id="skill-builder-name-preview"
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
              id="skill-builder-name-error"
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

export function SkillBuilderStart() {
  const { user } = useAuth();
  const project = useCurrentProject();
  const router = useRouter();
  const [name, setName] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [idempotency] = useState(() => createSkillBuilderIdempotencyRegistry());
  const normalizedName = useMemo(() => normalizeSkillBuilderSlug(name), [name]);
  const localError =
    submitted || name.length > 0 ? skillBuilderSlugError(normalizedName) : null;
  const create = useCreateSkillBuilderSession(user?.id ?? "", project.id);
  const allowed = skillBuilderCanAuthor(project.capabilities);

  function submit() {
    setSubmitted(true);
    if (!user || !allowed || skillBuilderSlugError(normalizedName)) return;
    const signature = skillBuilderSemanticSignature({
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
        router.push(skillBuilderSessionPath(project.slug, response.data.id));
      },
    });
  }

  if (!user) return null;

  return (
    <SkillBuilderStartView
      name={name}
      normalizedName={normalizedName}
      errorMessage={
        !allowed
          ? "当前账号没有创建 Skill 的权限。"
          : (localError ??
            (create.error ? skillBuilderErrorMessage(create.error) : null))
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
