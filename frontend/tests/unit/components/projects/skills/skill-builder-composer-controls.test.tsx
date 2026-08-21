import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  SkillBuilderComposerAttachments,
  SkillBuilderComposerControls,
  skillBuilderAvailableThinkingModes,
} from "@/components/projects/skills/skill-builder-composer-controls";
import { I18nProvider } from "@/core/i18n/context";
import type { Model } from "@/core/models/types";

const MODEL_ID = "00000000-0000-4000-8000-000000000205";

function renderUi(node: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">{node}</I18nProvider>,
  );
}

function model(overrides: Partial<Model> = {}): Model {
  return {
    name: MODEL_ID,
    model: MODEL_ID,
    display_name: "Doubao Seed 2.0",
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: false,
    supports_vision_bridge: false,
    is_default: true,
    ...overrides,
  };
}

describe("skillBuilderAvailableThinkingModes", () => {
  test("gates modes by the selected model's capabilities", () => {
    expect(skillBuilderAvailableThinkingModes(undefined)).toEqual(["flash"]);
    expect(
      skillBuilderAvailableThinkingModes(
        model({ supports_thinking: false, supports_reasoning_effort: false }),
      ),
    ).toEqual(["flash"]);
    expect(
      skillBuilderAvailableThinkingModes(
        model({ supports_reasoning_effort: false }),
      ),
    ).toEqual(["flash", "thinking"]);
    expect(skillBuilderAvailableThinkingModes(model())).toEqual([
      "flash",
      "thinking",
      "pro",
      "ultra",
    ]);
  });
});

describe("SkillBuilderComposerAttachments", () => {
  test("renders nothing without attachments", () => {
    expect(
      renderUi(
        <SkillBuilderComposerAttachments
          attachments={[]}
          disabled={false}
          onRemove={() => undefined}
        />,
      ),
    ).toBe("");
  });

  test("shows removable chips for queued reference files", () => {
    const html = renderUi(
      <SkillBuilderComposerAttachments
        attachments={[{ name: "接口说明.md", content: "# API" }]}
        disabled={false}
        onRemove={() => undefined}
      />,
    );
    expect(html).toContain("接口说明.md");
    expect(html).toContain('aria-label="移除附件 接口说明.md"');
  });
});

describe("SkillBuilderComposerControls", () => {
  test("offers upload, model, and thinking pickers like the chat composer", () => {
    const html = renderUi(
      <SkillBuilderComposerControls
        attachDisabled={false}
        pickersDisabled={false}
        models={[model()]}
        selectedModel={model()}
        thinkingMode="flash"
        onPickFiles={() => undefined}
        onSelectModel={() => undefined}
        onSelectThinkingMode={() => undefined}
      />,
    );
    expect(html).toContain('aria-label="添加参考文件"');
    expect(html).toContain('aria-label="选择模型"');
    expect(html).toContain("Doubao Seed 2.0");
    expect(html).toContain("闪速");
  });

  test("hides pickers when the catalog or capabilities do not allow them", () => {
    const html = renderUi(
      <SkillBuilderComposerControls
        attachDisabled={false}
        pickersDisabled={false}
        models={[]}
        selectedModel={undefined}
        thinkingMode="flash"
        onPickFiles={() => undefined}
        onSelectModel={() => undefined}
        onSelectThinkingMode={() => undefined}
      />,
    );
    expect(html).toContain('aria-label="添加参考文件"');
    expect(html).not.toContain('aria-label="选择模型"');
    expect(html).not.toContain('aria-label="选择思考强度"');
  });
});
