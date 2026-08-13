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
  agentBuilderSlugErrorCode,
  createAgentBuilderIdempotencyRegistry,
  normalizeAgentBuilderSlug,
  useCreateAgentBuilderSession,
} from "@/core/agent-builder";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";

import { useCurrentProject } from "../project-context";

import { agentBuilderSessionPath } from "./agent-builder-workspace";

export function agentBuilderErrorMessage(
  error: unknown,
  copy: Translations["agents"]["builder"]["errors"],
): string {
  if (!(error instanceof AgentBuilderApiError)) {
    return copy.unavailable;
  }
  if (error.code === "AGENT_BUILDER_CONFLICT") {
    return copy.conflict;
  }
  if (error.code === "AGENT_BUILDER_FORBIDDEN") {
    return copy.forbidden;
  }
  if (error.code === "AGENT_BUILDER_NOT_FOUND") {
    return copy.notFound;
  }
  if (error.code === "AGENT_BUILDER_VALIDATION_FAILED") {
    return copy.validationFailed;
  }
  if (error.code === "AGENT_BUILDER_RESPONSE_INVALID") {
    return copy.invalidResponse;
  }
  if (error.code === "AGENT_BUILDER_NETWORK_ERROR") {
    return copy.network;
  }
  return error.message || copy.unavailable;
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
  const { t } = useI18n();
  const copy = t.agents.builder.start;

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
          {copy.title}
        </h1>
        <p className="text-muted-foreground mx-auto mt-2 max-w-md text-sm leading-6">
          {copy.hint}
        </p>

        <form className="mt-8 space-y-3 text-left" onSubmit={submit}>
          <label className="sr-only" htmlFor="agent-builder-name">
            {copy.nameLabel}
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
            placeholder={copy.placeholder}
            disabled={pending}
            onChange={(event) => onNameChange(event.target.value)}
          />
          {normalizedName ? (
            <p
              id="agent-builder-name-preview"
              className="text-muted-foreground px-1 text-xs"
            >
              {copy.savedAs(normalizedName)}
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
            {pending ? copy.creating : copy.continue}
          </Button>
        </form>
      </section>
    </main>
  );
}

export function AgentBuilderStart() {
  const { t } = useI18n();
  const { user } = useAuth();
  const project = useCurrentProject();
  const router = useRouter();
  const [name, setName] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [idempotency] = useState(() => createAgentBuilderIdempotencyRegistry());
  const normalizedName = useMemo(() => normalizeAgentBuilderSlug(name), [name]);
  const localErrorCode =
    submitted || name.length > 0
      ? agentBuilderSlugErrorCode(normalizedName)
      : null;
  const localError =
    localErrorCode === "too-short"
      ? t.agents.builder.start.nameTooShort
      : localErrorCode === "too-long"
        ? t.agents.builder.start.nameTooLong
        : localErrorCode === "invalid"
          ? t.agents.builder.start.nameInvalid
          : null;
  const create = useCreateAgentBuilderSession(user?.id ?? "", project.id);
  const allowed = agentBuilderCanAuthor(project.capabilities);

  function submit() {
    setSubmitted(true);
    if (!user || !allowed || agentBuilderSlugErrorCode(normalizedName)) return;
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
          ? t.agents.builder.start.forbidden
          : (localError ??
            (create.error
              ? agentBuilderErrorMessage(create.error, t.agents.builder.errors)
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
