import { describe, expect, rs, test } from "@rstest/core";
import { createElement, Fragment, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: ReactNode }) =>
    createElement(Fragment, null, children),
  TooltipTrigger: ({ children }: { children: ReactNode }) =>
    createElement(Fragment, null, children),
  TooltipContent: ({ children }: { children: ReactNode }) =>
    createElement("div", { "data-slot": "tooltip-content" }, children),
}));

import {
  SkillExportButton,
  skillExportBlockReason,
} from "@/components/assets/skill-export-button";
import { I18nProvider } from "@/core/i18n/context";

describe("Skill export availability", () => {
  test("blocks unsaved edits before all other states", () => {
    expect(
      skillExportBlockReason({
        hasVersion: true,
        unsaved: true,
        loading: true,
        revoked: true,
      }),
    ).toBe("unsaved");
  });

  test("blocks revoked versions and permits persisted eligible versions", () => {
    expect(skillExportBlockReason({ hasVersion: true, revoked: true })).toBe(
      "revoked",
    );
    expect(skillExportBlockReason({ hasVersion: true })).toBeNull();
    expect(skillExportBlockReason({ hasVersion: false })).toBe("no-version");
  });

  test("blocks a System Skill version that is not Current", () => {
    expect(skillExportBlockReason({ hasVersion: true, notCurrent: true })).toBe(
      "not-current",
    );
  });

  test("does not render a hover description", () => {
    const exportButton = createElement(SkillExportButton, {
      versionNumber: 1,
      download: async () => ({
        content: new Blob(),
        filename: "example.zip",
      }),
    });
    const html = renderToStaticMarkup(
      createElement(
        I18nProvider,
        { initialLocale: "en-US" } as {
          initialLocale: "en-US";
          children: ReactNode;
        },
        exportButton,
      ),
    );

    expect(html).toContain("Export ZIP");
    expect(html).not.toContain("Export the selected v1");
  });
});
