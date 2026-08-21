import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ingestSkillBuilderAttachmentFiles,
  SkillBuilderDialogError,
  skillBuilderExecutionPreferenceFor,
} from "@/components/projects/skills/skill-builder-workspace";
import type { SkillBuilderAttachment } from "@/core/skill-builder";

function deferred<T>() {
  let resolve: ((value: T) => void) | undefined;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return {
    promise,
    resolve(value: T) {
      if (!resolve) throw new Error("Deferred promise was not initialized");
      resolve(value);
    },
  };
}

function utf8(value: string): ArrayBuffer {
  return new TextEncoder().encode(value).buffer;
}

describe("Skill Builder workspace actions", () => {
  test("uses the ordinary chat mode resolver and capability defaults", () => {
    expect(
      skillBuilderExecutionPreferenceFor(
        {
          name: "11111111-1111-4111-8111-111111111111",
          supports_thinking: true,
          supports_reasoning_effort: true,
        },
        undefined,
      ),
    ).toEqual({
      model_name: "11111111-1111-4111-8111-111111111111",
      mode: "pro",
      thinking_enabled: true,
      reasoning_effort: "medium",
    });
    expect(
      skillBuilderExecutionPreferenceFor(
        {
          name: "22222222-2222-4222-8222-222222222222",
          supports_thinking: true,
          supports_reasoning_effort: false,
        },
        "ultra",
      ),
    ).toMatchObject({
      mode: "thinking",
      thinking_enabled: true,
      reasoning_effort: null,
    });
    expect(
      skillBuilderExecutionPreferenceFor(
        {
          name: "33333333-3333-4333-8333-333333333333",
          supports_thinking: false,
          supports_reasoning_effort: false,
        },
        "pro",
      ),
    ).toMatchObject({
      mode: "flash",
      thinking_enabled: false,
      reasoning_effort: null,
    });
  });

  test("shows action errors inside confirmation dialogs", () => {
    const html = renderToStaticMarkup(
      <SkillBuilderDialogError message="暂时无法创建 Skill，请稍后重试。" />,
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain("暂时无法创建 Skill，请稍后重试。");
  });

  test("does not resurrect an attachment removed during an async read", async () => {
    const read = deferred<ArrayBuffer>();
    let current: SkillBuilderAttachment[] = [
      { name: "removed.md", content: "old" },
    ];
    const pending = ingestSkillBuilderAttachmentFiles(
      [
        {
          name: "new.md",
          size: 3,
          arrayBuffer: () => read.promise,
        },
      ],
      () => current,
      (next) => {
        current = next;
      },
    );

    current = [];
    read.resolve(utf8("new"));

    await expect(pending).resolves.toEqual({ ok: true });
    expect(current).toEqual([{ name: "new.md", content: "new" }]);
  });

  test("merges concurrent attachment reads instead of last-writer overwriting", async () => {
    const firstRead = deferred<ArrayBuffer>();
    const secondRead = deferred<ArrayBuffer>();
    let current: SkillBuilderAttachment[] = [];
    const commit = (next: SkillBuilderAttachment[]) => {
      current = next;
    };

    const first = ingestSkillBuilderAttachmentFiles(
      [
        {
          name: "first.md",
          size: 5,
          arrayBuffer: () => firstRead.promise,
        },
      ],
      () => current,
      commit,
    );
    const second = ingestSkillBuilderAttachmentFiles(
      [
        {
          name: "second.md",
          size: 6,
          arrayBuffer: () => secondRead.promise,
        },
      ],
      () => current,
      commit,
    );

    secondRead.resolve(utf8("second"));
    await second;
    firstRead.resolve(utf8("first"));
    await first;

    expect(current).toEqual([
      { name: "second.md", content: "second" },
      { name: "first.md", content: "first" },
    ]);
  });
});
