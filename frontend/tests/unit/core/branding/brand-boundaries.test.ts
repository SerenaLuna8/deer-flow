import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test, rs } from "@rstest/core";

import { ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY } from "@/components/admin/operations/admin-operations-shell";
import { THREAD_CHAT_RESET_EVENT } from "@/components/workspace/chats/use-thread-chat";
import { warnUnsupportedStreamModes } from "@/core/api/stream-mode";
import {
  appendHtmlPreviewScrollRestoration,
  HTML_PREVIEW_SCROLL_MESSAGE_SOURCE,
  isHtmlPreviewScrollMessageSource,
} from "@/core/artifacts/preview";

function source(path: string): string {
  return readFileSync(resolve(process.cwd(), path), "utf8");
}

describe("ActWeave frontend brand boundaries", () => {
  test("login, setup, and landing use the shared ActWeave logo instead of deer.svg", () => {
    const brandComponent = source("src/components/branding/actweave-logo.tsx");
    expect(brandComponent).toContain("/images/actweave-logo-concept-v1.png");
    expect(brandComponent).toContain('alt=""');

    for (const path of [
      "src/app/(auth)/login/page.tsx",
      "src/app/(auth)/setup/page.tsx",
      "src/components/landing/hero.tsx",
    ]) {
      const content = source(path);
      expect(content).toContain("<ActWeaveLogo");
      expect(content).not.toContain("/images/deer.svg");
    }

    expect(source("src/app/layout.tsx")).toContain(
      'url: "/images/actweave-logo-concept-v1.png"',
    );
    expect(existsSync(resolve(process.cwd(), "public/images/deer.svg"))).toBe(
      false,
    );
    expect(
      readFileSync(resolve(process.cwd(), "public/favicon.ico")).subarray(0, 4),
    ).toEqual(Buffer.from([0x00, 0x00, 0x01, 0x00]));
  });

  test("welcome copy uses the ActWeave mark without the legacy deer symbol", () => {
    for (const path of [
      "src/core/i18n/locales/en-US.ts",
      "src/core/i18n/locales/zh-CN.ts",
    ]) {
      const content = source(path);
      expect(content).not.toContain("🦌 ActWeave");
      expect(content).toContain("✦ ActWeave");
    }
  });

  test("current safety articles use ActWeave while retaining upstream and SDK references", () => {
    const english = source(
      "src/content/en/posts/provider-safety-termination-in-tool-agents.mdx",
    );
    const chinese = source(
      "src/content/zh/posts/provider-safety-termination-in-tool-agents.mdx",
    );

    expect(english).toContain("configure provider detectors in ActWeave");
    expect(english).toContain("## What ActWeave Does at This Boundary");
    expect(chinese).toContain("如何在 ActWeave 中配置 provider detector");
    expect(chinese).toContain("## ActWeave 在这条边界上做什么");
    for (const content of [english, chinese]) {
      expect(content).toContain(
        "https://github.com/bytedance/deer-flow/pull/3035",
      );
      expect(content).toContain("deerflow.agents.middlewares");
    }
  });

  test("uses ActWeave browser event, UI storage, artifact, and console identifiers", () => {
    expect(ADMIN_NAVIGATION_EXPANDED_STORAGE_KEY).toBe(
      "actweave:admin-navigation-expanded",
    );
    expect(THREAD_CHAT_RESET_EVENT).toBe("actweave:thread-chat-reset");
    expect(HTML_PREVIEW_SCROLL_MESSAGE_SOURCE).toBe(
      "actweave-artifact-preview-scroll",
    );
    expect(appendHtmlPreviewScrollRestoration("<html></html>")).toContain(
      "data-actweave-artifact-scroll-restoration",
    );
    expect(
      isHtmlPreviewScrollMessageSource("deerflow-artifact-preview-scroll"),
    ).toBe(true);
    expect(isHtmlPreviewScrollMessageSource("untrusted-source")).toBe(false);
    const legacyPreview =
      "<html><script data-deerflow-artifact-scroll-restoration></script></html>";
    expect(appendHtmlPreviewScrollRestoration(legacyPreview)).toBe(
      legacyPreview,
    );

    const adminShell = source(
      "src/components/admin/operations/admin-operations-shell.tsx",
    );
    expect(adminShell).toContain('"deer-flow:admin-navigation-expanded"');
    expect(adminShell).toContain("readMigratedStorageValue(");

    const reconnectClient = source("src/core/private-work/api-client.ts");
    expect(reconnectClient).toContain("lg:stream:account:");
    expect(reconnectClient).not.toContain("deerflow:stream");

    const warn = rs.fn();
    warnUnsupportedStreamModes(["brand-prefix-regression"], warn);
    expect(warn).toHaveBeenCalledWith(
      "[act-weave] Dropped unsupported LangGraph stream mode(s): brand-prefix-regression",
    );
  });
});
