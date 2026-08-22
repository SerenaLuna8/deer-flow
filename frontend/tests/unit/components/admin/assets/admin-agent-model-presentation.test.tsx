import { describe, expect, test } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

import { VersionTimeline } from "@/components/admin/assets/admin-asset-page";
import { I18nProvider } from "@/core/i18n/context";
import { modelsQueryKey } from "@/core/models/hooks";
import type { Model, ModelsResponse } from "@/core/models/types";
import type { AssetVersion } from "@/core/shared-assets";

const MODEL_REF = "00000000-0000-4000-8000-000000000451";

const model: Model = {
  name: MODEL_REF,
  model: MODEL_REF,
  display_name: "Visible admin model",
  supports_thinking: true,
  supports_reasoning_effort: true,
  supports_vision: false,
  supports_vision_bridge: false,
  is_default: true,
};

const version: Extract<AssetVersion, { agent_id: string }> = {
  id: "11111111-1111-4111-8111-111111111111",
  agent_id: "22222222-2222-4222-8222-222222222222",
  version_number: 1,
  relation: "current",
  description: "Admin model presentation",
  agents_instructions: "# AGENTS",
  soul: "# SOUL",
  identity: "# IDENTITY",
  user_context: "# USER",
  payload_schema_version: 3,
  model_ref: MODEL_REF,
  model_settings: {},
  tool_groups: [],
  skill_refs: [],
  mcp_version_ids: [],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  created_by_user_id: "33333333-3333-4333-8333-333333333333",
  created_at: "2026-08-16T00:00:00Z",
};

function renderTimeline(models: Model[], locale: "en-US" | "zh-CN" = "zh-CN") {
  const queryClient = new QueryClient();
  queryClient.setQueryData<ModelsResponse>(modelsQueryKey, {
    models,
    token_usage: { enabled: false },
  });
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>
      <QueryClientProvider client={queryClient}>
        <VersionTimeline kind="agents" versions={[version]} />
      </QueryClientProvider>
    </I18nProvider>,
  );
}

describe("admin Agent model presentation", () => {
  test("shows the catalog display name instead of the model UUID", () => {
    const html = renderTimeline([model]);

    expect(html).toContain("Visible admin model");
    expect(html).not.toContain(MODEL_REF);
  });

  test("shows a generic unavailable label when the model left the catalog", () => {
    const html = renderTimeline([]);

    expect(html).toContain("当前引用的模型已不可用");
    expect(html).not.toContain(MODEL_REF);
  });

  test("renders Current Version status without Chinese in English", () => {
    const html = renderTimeline([model], "en-US");

    expect(html).toContain("Current Version");
    expect(html).not.toMatch(/\p{Script=Han}/u);
  });
});
