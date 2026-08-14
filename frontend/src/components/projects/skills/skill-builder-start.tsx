"use client";

import { Loader2Icon, SparklesIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import {
  SkillBuilderApiError,
  createSkillBuilderIdempotencyRegistry,
  normalizeSkillBuilderSlug,
  skillBuilderCanAuthor,
  skillBuilderSemanticSignature,
  skillBuilderSessionPath,
  skillBuilderSlugErrorCode,
  useCreateSkillBuilderSession,
} from "@/core/skill-builder";

import { useCurrentProject } from "../project-context";

export function skillBuilderErrorMessage(
  error: unknown,
  copy: Translations["skills"]["builder"]["errors"],
): string {
  if (!(error instanceof SkillBuilderApiError)) {
    return copy.unavailable;
  }
  if (error.serverCode === "SKILL_BUILDER_MODEL_UNAVAILABLE") {
    return copy.modelUnavailable;
  }
  if (error.serverCode === "SKILL_BUILDER_EFFORT_UNSUPPORTED") {
    return copy.effortUnsupported;
  }
  if (error.code === "SKILL_BUILDER_CONFLICT") {
    return copy.conflict;
  }
  if (error.code === "SKILL_BUILDER_FORBIDDEN") {
    return copy.forbidden;
  }
  if (error.code === "SKILL_BUILDER_NOT_FOUND") {
    return copy.notFound;
  }
  if (error.code === "SKILL_BUILDER_LIMIT_EXCEEDED") {
    return copy.limitExceeded;
  }
  if (error.code === "SKILL_BUILDER_VALIDATION_FAILED") {
    return copy.validationFailed;
  }
  if (error.code === "SKILL_BUILDER_RESPONSE_INVALID") {
    return copy.invalidResponse;
  }
  if (error.code === "SKILL_BUILDER_NETWORK_ERROR") {
    return copy.network;
  }
  if (error.code === "SKILL_BUILDER_UNAVAILABLE") {
    return copy.unavailable;
  }
  return error.message || copy.unavailable;
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
  const { t } = useI18n();
  const copy = t.skills.builder.start;

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
          {copy.title}
        </h1>
        <p className="text-muted-foreground mx-auto mt-2 max-w-md text-sm leading-6">
          {copy.hint}
        </p>

        <form className="mt-8 space-y-3 text-left" onSubmit={submit}>
          <label className="sr-only" htmlFor="skill-builder-name">
            {copy.nameLabel}
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
            placeholder={copy.placeholder}
            disabled={pending}
            onChange={(event) => onNameChange(event.target.value)}
          />
          {normalizedName ? (
            <p
              id="skill-builder-name-preview"
              className="text-muted-foreground px-1 text-xs"
            >
              {copy.savedAs("")}
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
            {pending ? copy.creating : copy.continue}
          </Button>
        </form>
      </section>
    </main>
  );
}

export function SkillBuilderStart() {
  const { user } = useAuth();
  const { t } = useI18n();
  const project = useCurrentProject();
  const router = useRouter();
  const [name, setName] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [idempotency] = useState(() => createSkillBuilderIdempotencyRegistry());
  const normalizedName = useMemo(() => normalizeSkillBuilderSlug(name), [name]);
  const localErrorCode =
    submitted || name.length > 0
      ? skillBuilderSlugErrorCode(normalizedName)
      : null;
  const startCopy = t.skills.builder.start;
  const localError =
    localErrorCode === "too-short"
      ? startCopy.nameTooShort
      : localErrorCode === "too-long"
        ? startCopy.nameTooLong
        : localErrorCode === "invalid"
          ? startCopy.nameInvalid
          : null;
  const create = useCreateSkillBuilderSession(user?.id ?? "", project.id);
  const allowed = skillBuilderCanAuthor(project.capabilities);

  function submit() {
    setSubmitted(true);
    if (!user || !allowed || skillBuilderSlugErrorCode(normalizedName)) return;
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
          ? startCopy.forbidden
          : (localError ??
            (create.error
              ? skillBuilderErrorMessage(create.error, t.skills.builder.errors)
              : null))
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
