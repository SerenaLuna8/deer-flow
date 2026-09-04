"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { useKnowledgeModelOptions } from "@/core/knowledge/hooks";
import type { KnowledgeBaseItem } from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";

/**
 * Read-only summary of a configured base's embedding model, retrieval mode,
 * and reranker. Shown wherever a document is processed under the base's
 * saved configuration (uploads into an existing base, document reparse) so
 * the user sees what applies without being offered controls that only the
 * Settings page owns.
 */
export function KnowledgeBaseConfigurationSummary({
  scope,
  base,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const options = useKnowledgeModelOptions(scope, true);

  const embeddingOption = options.data?.embedding_models.find(
    (option) => option.id === base.embedding_model_id,
  );
  const modelDisplayName = embeddingOption
    ? `${embeddingOption.provider_name} · ${embeddingOption.model_name}`
    : labels.wizard.configuredModelUnavailable;
  const rerankerOption = options.data?.reranker_models.find(
    (option) => option.id === base.reranker_model_id,
  );
  const rerankerDisplayName = rerankerOption
    ? `${rerankerOption.provider_name} · ${rerankerOption.model_name}`
    : base.reranker_model_id
      ? labels.wizard.configuredModelUnavailable
      : labels.bases.rerankerNone;

  return (
    <section
      className="border-border/60 space-y-3 border-t pt-5"
      data-testid="knowledge-base-configuration-summary"
    >
      <p className="text-muted-foreground text-xs leading-5">
        {labels.wizard.existingConfigurationHint}
      </p>
      <dl className="grid gap-3 text-[13px]">
        <div className="grid min-w-0 gap-1.5">
          <dt className="font-medium">{labels.bases.modelLabel}</dt>
          {options.isLoading ? (
            <dd>
              <Skeleton className="h-9 rounded-lg" />
            </dd>
          ) : (
            <dd
              className="bg-muted/60 truncate rounded-lg px-3 py-2"
              title={modelDisplayName}
            >
              {modelDisplayName}
            </dd>
          )}
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="grid min-w-0 gap-1.5">
            <dt className="font-medium">{labels.bases.retrievalModeLabel}</dt>
            <dd className="bg-muted/60 rounded-lg px-3 py-2">
              {labels.bases.retrievalModes[base.retrieval_mode]}
            </dd>
          </div>
          <div className="grid min-w-0 gap-1.5">
            <dt className="font-medium">{labels.bases.rerankerLabel}</dt>
            {options.isLoading ? (
              <dd>
                <Skeleton className="h-9 rounded-lg" />
              </dd>
            ) : (
              <dd
                className="bg-muted/60 truncate rounded-lg px-3 py-2"
                title={rerankerDisplayName}
              >
                {rerankerDisplayName}
              </dd>
            )}
          </div>
        </div>
      </dl>
      {options.error ? (
        <p role="alert" className="text-destructive text-xs">
          {labels.bases.modelsLoadFailed}
        </p>
      ) : null}
    </section>
  );
}
