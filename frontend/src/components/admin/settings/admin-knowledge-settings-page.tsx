"use client";

import { InfoIcon, Loader2Icon } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import {
  AdminPage,
  AdminPageHeader,
  AdminSection,
} from "@/components/admin/ui/admin-page";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { AdminKnowledgeSettingsApiError } from "@/core/admin-settings/knowledge/api";
import {
  useAdminKnowledgeSettings,
  useSaveAdminKnowledgeSettings,
} from "@/core/admin-settings/knowledge/hooks";
import {
  adminKnowledgeSettingsUpdateSchema,
  knowledgeSettingsDraft,
  type AdminKnowledgeSettings,
  type AdminKnowledgeSettingsDraft,
} from "@/core/admin-settings/knowledge/types";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { useModels } from "@/core/models/hooks";

const FIELD_COPY = {
  "en-US": {
    enabled: [
      "Enable knowledge",
      "Allow projects to upload and search knowledge documents.",
    ],
    etl_type: [
      "Document parser",
      "Local extraction engine used for new previews, uploads, and explicit reparses after services restart.",
    ],
    extraction_cache_enabled: [
      "Cache extraction results",
      "Reuse complete local extraction results when the source and parser profile are identical.",
    ],
    worker_concurrency: [
      "Processing concurrency",
      "Concurrent document processing tasks per Worker (1–16).",
    ],
    task_timeout_seconds: [
      "Task timeout (seconds)",
      "Maximum duration of a document processing task (30–7,200).",
    ],
    upload_max_bytes: [
      "Maximum upload size (bytes)",
      "Per-file upload limit, up to 52,428,800 bytes (50 MiB).",
    ],
    max_knowledge_bases_per_project: [
      "Knowledge bases per project",
      "Maximum number of knowledge bases in a project.",
    ],
    max_documents_per_knowledge_base: [
      "Documents per knowledge base",
      "Maximum number of documents in a knowledge base.",
    ],
    max_segments_per_document: [
      "Segments per document",
      "Maximum number of segments produced from a document (1–5,000).",
    ],
    minio_endpoint: [
      "Storage endpoint",
      "Host and port, without a URL prefix. Changing a configured endpoint requires re-entering the secret key.",
    ],
    minio_bucket: ["Storage bucket", "Bucket for original document files."],
    minio_access_key: [
      "Storage access key",
      "Access key used by the knowledge service.",
    ],
    minio_secret_key: [
      "Storage secret key",
      "Leave blank to keep the saved key. A new key is never displayed after saving.",
    ],
    minio_secure: ["Use TLS", "Connect to document storage over HTTPS."],
    summary_model_name: [
      "Summary model",
      "Active System Model for document summaries. Changes take effect immediately; existing summaries are preserved.",
    ],
    query_cache_enabled: [
      "Cache query embeddings",
      "Reuse identical query embeddings within this Gateway process.",
    ],
    query_cache_max_entries: [
      "Cache entries",
      "Maximum cached query embeddings per Gateway (16–65,536).",
    ],
    query_cache_ttl_seconds: [
      "Cache lifetime (seconds)",
      "Time before a cached query embedding expires (5–86,400).",
    ],
  },
  "zh-CN": {
    enabled: ["启用知识库", "允许项目上传与检索知识库文档。"],
    etl_type: [
      "文档解析器",
      "服务重启后，用于新预览、上传及显式重新解析的本地提取引擎。",
    ],
    extraction_cache_enabled: [
      "缓存提取结果",
      "原文件和解析配置完全一致时，复用已完成的本地提取结果。",
    ],
    worker_concurrency: [
      "处理并发数",
      "每个 Worker 同时处理的文档任务数（1–16）。",
    ],
    task_timeout_seconds: [
      "任务超时（秒）",
      "文档处理任务的最长运行时间（30–7,200）。",
    ],
    upload_max_bytes: [
      "上传大小上限（字节）",
      "单个文件大小限制，最多 52,428,800 字节（50 MiB）。",
    ],
    max_knowledge_bases_per_project: [
      "每项目知识库数",
      "一个项目可创建的知识库数量上限。",
    ],
    max_documents_per_knowledge_base: [
      "每知识库文档数",
      "一个知识库可收录的文档数量上限。",
    ],
    max_segments_per_document: [
      "每文档分段数",
      "单个文档可生成的分段数量上限（1–5,000）。",
    ],
    minio_endpoint: [
      "存储地址",
      "主机与端口，不包含 URL 前缀。更改已配置的地址时需重新输入密钥。",
    ],
    minio_bucket: ["存储桶", "用于保存原始文档文件的存储桶。"],
    minio_access_key: [
      "存储 Access Key",
      "知识库服务连接存储时使用的访问标识。",
    ],
    minio_secret_key: [
      "存储 Secret Key",
      "留空保留已保存的密钥。新密钥保存后不会回显。",
    ],
    minio_secure: ["使用 TLS", "通过 HTTPS 连接文档存储。"],
    summary_model_name: [
      "摘要模型",
      "选择用于文档摘要的活跃系统模型。变更即时生效，保留已有摘要。",
    ],
    query_cache_enabled: [
      "缓存查询向量",
      "在当前 Gateway 进程内复用相同查询的向量。",
    ],
    query_cache_max_entries: [
      "缓存条目数",
      "每个 Gateway 缓存的查询向量数量上限（16–65,536）。",
    ],
    query_cache_ttl_seconds: [
      "缓存有效期（秒）",
      "查询向量缓存的过期时间（5–86,400）。",
    ],
  },
} as const;

const NUMBER_FIELDS = [
  ["worker_concurrency", 1, 16],
  ["task_timeout_seconds", 30, 7200],
  ["upload_max_bytes", 1, 52_428_800],
  ["max_knowledge_bases_per_project", 1, Number.MAX_SAFE_INTEGER],
  ["max_documents_per_knowledge_base", 1, Number.MAX_SAFE_INTEGER],
  ["max_segments_per_document", 1, 5000],
] as const;
const CACHE_FIELDS = [
  ["query_cache_max_entries", 16, 65_536],
  ["query_cache_ttl_seconds", 5, 86_400],
] as const;

function FieldShell({
  name,
  copy,
  children,
}: {
  name: string;
  copy: readonly [string, string];
  children: ReactNode;
}) {
  return (
    <div className="grid gap-2 border-b px-4 py-3 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_minmax(14rem,22rem)] sm:items-center sm:gap-6">
      <div>
        <label
          htmlFor={`knowledge-${name}`}
          className="text-[13px] font-medium"
        >
          {copy[0]}
        </label>
        <p
          id={`knowledge-${name}-hint`}
          className="text-muted-foreground mt-1 max-w-xl text-xs leading-5"
        >
          {copy[1]}
        </p>
      </div>
      {children}
    </div>
  );
}

function KnowledgeSettingsEditor({
  accountId,
  settings,
  refresh,
}: {
  accountId: string;
  settings: AdminKnowledgeSettings;
  refresh: () => Promise<AdminKnowledgeSettings | undefined>;
}) {
  const { locale, t } = useI18n();
  const labels = t.adminKnowledgeSettings;
  const copy = FIELD_COPY[locale === "zh-CN" ? "zh-CN" : "en-US"];
  const modelCatalog = useModels();
  const save = useSaveAdminKnowledgeSettings(accountId);
  const [base, setBase] = useState(settings);
  const [draft, setDraft] = useState(() => knowledgeSettingsDraft(settings));
  const [secret, setSecret] = useState("");
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<{
    error: boolean;
    message: string;
  } | null>(null);
  const [conflictNeedsRefresh, setConflictNeedsRefresh] = useState(false);
  const activeRequest = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const dirty =
    secret.length > 0 ||
    JSON.stringify(draft) !== JSON.stringify(knowledgeSettingsDraft(base));
  const writable = adminKnowledgeSettingsUpdateSchema.safeParse({
    ...draft,
    expected_revision: base.revision,
  });
  const endpointChanged =
    base.secret_key_configured &&
    draft.minio_endpoint?.trim() !== base.minio_endpoint;
  const valid =
    writable.success &&
    (!endpointChanged || secret.trim().length > 0) &&
    (!draft.enabled ||
      base.secret_key_configured ||
      secret.trim().length > 0) &&
    (secret.length === 0 || secret.trim().length > 0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      activeRequest.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!dirty && !pending && settings.revision !== base.revision) {
      setBase(settings);
      setDraft(knowledgeSettingsDraft(settings));
    }
  }, [settings, base.revision, dirty, pending]);

  useEffect(() => {
    if (!dirty) return;
    const preventUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", preventUnload);
    return () => window.removeEventListener("beforeunload", preventUnload);
  }, [dirty]);

  function change<K extends keyof AdminKnowledgeSettingsDraft>(
    key: K,
    value: AdminKnowledgeSettingsDraft[K],
  ) {
    setDraft((current) => ({ ...current, [key]: value }));
    setFeedback(null);
  }

  async function refreshConflict() {
    setPending(true);
    const latest = await refresh();
    if (!mounted.current) return;
    setPending(false);
    if (latest) setBase(latest);
    setConflictNeedsRefresh(!latest);
    setFeedback({
      error: true,
      message: latest ? labels.conflict : labels.conflictRefreshFailed,
    });
  }

  async function submit() {
    if (!valid || !dirty || pending || conflictNeedsRefresh) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setPending(true);
    setFeedback(null);
    try {
      const result = await save(
        {
          ...draft,
          expected_revision: base.revision,
          ...(secret.length ? { minio_secret_key: secret } : {}),
        },
        controller.signal,
      );
      if (!mounted.current || controller.signal.aborted) return;
      setBase(result);
      setDraft(knowledgeSettingsDraft(result));
      setFeedback({ error: false, message: labels.saved });
    } catch (error) {
      if (!mounted.current || controller.signal.aborted) return;
      if (
        error instanceof AdminKnowledgeSettingsApiError &&
        error.status === 409
      ) {
        await refreshConflict();
      } else {
        const message =
          error instanceof AdminKnowledgeSettingsApiError
            ? error.status === 401
              ? labels.authRequired
              : error.status === 422
                ? (error.publicMessage ?? labels.invalid)
                : error.status === 503
                  ? labels.unavailable
                  : labels.generic
            : labels.generic;
        setFeedback({ error: true, message });
      }
    } finally {
      if (mounted.current) {
        setSecret("");
        setPending(false);
      }
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }

  function booleanField(
    name:
      | "enabled"
      | "extraction_cache_enabled"
      | "minio_secure"
      | "query_cache_enabled",
  ) {
    return (
      <FieldShell name={name} copy={copy[name]}>
        <Switch
          id={`knowledge-${name}`}
          aria-describedby={`knowledge-${name}-hint`}
          checked={draft[name]}
          onCheckedChange={(checked) => change(name, checked)}
          disabled={pending}
        />
      </FieldShell>
    );
  }
  function numberField([name, minimum, maximum]:
    | (typeof NUMBER_FIELDS)[number]
    | (typeof CACHE_FIELDS)[number]) {
    return (
      <FieldShell key={name} name={name} copy={copy[name]}>
        <Input
          id={`knowledge-${name}`}
          aria-describedby={`knowledge-${name}-hint`}
          className="h-9 text-[13px]"
          type="number"
          min={minimum}
          max={maximum}
          step={1}
          required
          value={Number.isNaN(draft[name]) ? "" : draft[name]}
          disabled={pending}
          onChange={(event) =>
            change(
              name,
              event.target.value === ""
                ? Number.NaN
                : Number(event.target.value),
            )
          }
        />
      </FieldShell>
    );
  }
  const missingModel =
    draft.summary_model_name !== null &&
    !modelCatalog.models.some(
      (model) => model.name === draft.summary_model_name,
    );

  const visibleFeedback =
    feedback ??
    (conflictNeedsRefresh
      ? { error: true, message: labels.conflictRefreshFailed }
      : null);

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      data-testid="admin-knowledge-settings-form"
    >
      <AdminSection
        title={labels.sectionTitle}
        contentClassName="p-0"
        actions={
          <>
            <span
              className="text-muted-foreground text-xs"
              data-testid="knowledge-settings-revision"
            >
              {labels.revision} {base.revision}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 text-xs"
              disabled={pending || !dirty}
              onClick={() => {
                setDraft(knowledgeSettingsDraft(base));
                setSecret("");
                setFeedback(null);
              }}
            >
              {labels.reset}
            </Button>
            <Button
              type="submit"
              size="sm"
              className="h-8 text-xs"
              disabled={pending || !dirty || !valid || conflictNeedsRefresh}
            >
              {pending && (
                <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
              )}
              {pending ? labels.saving : labels.save}
            </Button>
          </>
        }
      >
        {visibleFeedback && (
          <div
            role={visibleFeedback.error ? "alert" : "status"}
            className={`border-b px-4 py-3 text-[13px] ${visibleFeedback.error ? "text-destructive bg-destructive/5" : "text-foreground bg-muted/40"}`}
          >
            {visibleFeedback.message}
            {conflictNeedsRefresh && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="ml-3 h-7 text-xs"
                disabled={pending}
                onClick={() => void refreshConflict()}
              >
                {labels.retry}
              </Button>
            )}
          </div>
        )}
        {dirty && (
          <p className="text-muted-foreground border-b px-4 py-2 text-xs">
            {labels.dirty}
          </p>
        )}
        {booleanField("enabled")}
        <FieldShell name="etl_type" copy={copy.etl_type}>
          <Select
            value={draft.etl_type}
            onValueChange={(value) =>
              change(
                "etl_type",
                value as AdminKnowledgeSettingsDraft["etl_type"],
              )
            }
            disabled={pending}
          >
            <SelectTrigger
              id="knowledge-etl_type"
              aria-describedby="knowledge-etl_type-hint"
              className="h-9 w-full text-[13px]"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="dify">Dify</SelectItem>
              <SelectItem value="unstructured_local">
                {locale === "zh-CN"
                  ? "本地 Unstructured"
                  : "Local Unstructured"}
              </SelectItem>
            </SelectContent>
          </Select>
        </FieldShell>
        {booleanField("extraction_cache_enabled")}
        {NUMBER_FIELDS.slice(0, 2).map(numberField)}
        {(["minio_endpoint", "minio_bucket", "minio_access_key"] as const).map(
          (name) => (
            <FieldShell key={name} name={name} copy={copy[name]}>
              <Input
                id={`knowledge-${name}`}
                aria-describedby={`knowledge-${name}-hint`}
                className="h-9 text-[13px]"
                autoComplete="off"
                spellCheck={false}
                maxLength={name === "minio_bucket" ? 255 : 512}
                value={draft[name] ?? ""}
                disabled={pending}
                onChange={(event) => change(name, event.target.value || null)}
              />
            </FieldShell>
          ),
        )}
        <FieldShell name="minio_secret_key" copy={copy.minio_secret_key}>
          <Input
            id="knowledge-minio_secret_key"
            aria-describedby="knowledge-minio_secret_key-hint"
            className="h-9 text-[13px]"
            type="password"
            autoComplete="new-password"
            maxLength={65_536}
            value={secret}
            placeholder={
              base.secret_key_configured
                ? labels.secretConfigured
                : labels.secretUnset
            }
            disabled={pending}
            onChange={(event) => {
              setSecret(event.target.value);
              setFeedback(null);
            }}
          />
        </FieldShell>
        {booleanField("minio_secure")}
        {NUMBER_FIELDS.slice(2).map(numberField)}
        <FieldShell name="summary_model_name" copy={copy.summary_model_name}>
          <div className="min-w-0 space-y-2">
            <Select
              value={draft.summary_model_name ?? "none"}
              onValueChange={(value) =>
                change("summary_model_name", value === "none" ? null : value)
              }
              disabled={pending}
            >
              <SelectTrigger
                id="knowledge-summary_model_name"
                aria-describedby="knowledge-summary_model_name-hint"
                className="h-9 w-full text-[13px]"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">{labels.noSummaryModel}</SelectItem>
                {missingModel && (
                  <SelectItem value={draft.summary_model_name!} disabled>
                    {base.summary_model?.display_name ??
                      labels.unavailableModel}
                  </SelectItem>
                )}
                {modelCatalog.models.map((model) => (
                  <SelectItem key={model.name} value={model.name}>
                    {model.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {modelCatalog.error && (
              <p role="status" className="text-muted-foreground text-xs">
                {labels.modelsUnavailable}{" "}
                <button
                  type="button"
                  className="underline underline-offset-2"
                  onClick={() => void modelCatalog.refetch()}
                >
                  {labels.retry}
                </button>
              </p>
            )}
          </div>
        </FieldShell>
        {booleanField("query_cache_enabled")}
        {CACHE_FIELDS.map(numberField)}
      </AdminSection>
    </form>
  );
}

function AuthorizedKnowledgeSettingsPage({ accountId }: { accountId: string }) {
  const { t } = useI18n();
  const labels = t.adminKnowledgeSettings;
  const query = useAdminKnowledgeSettings(accountId);
  return (
    <AdminPage>
      <AdminPageHeader title={labels.title} description={labels.description} />
      <div
        role="note"
        data-testid="knowledge-settings-restart-banner"
        className="bg-muted/40 text-muted-foreground flex items-start gap-2 rounded-lg border px-4 py-3 text-[13px] leading-5"
      >
        <InfoIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
        {labels.restartNotice}
      </div>
      {query.data ? (
        <KnowledgeSettingsEditor
          accountId={accountId}
          settings={query.data}
          refresh={async () => {
            const result = await query.refetch();
            return result.isSuccess ? result.data : undefined;
          }}
        />
      ) : query.isLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          {labels.loading}
        </p>
      ) : (
        <div role="alert" className="space-y-3 rounded-lg border p-4 text-sm">
          <p>{labels.unavailable}</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            {labels.retry}
          </Button>
        </div>
      )}
    </AdminPage>
  );
}

export function AdminKnowledgeSettingsPage() {
  const { user } = useAuth();
  if (user?.system_role !== "system_admin") return null;
  return <AuthorizedKnowledgeSettingsPage key={user.id} accountId={user.id} />;
}
