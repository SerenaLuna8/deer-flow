import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

function source(path: string) {
  return readFileSync(resolve(process.cwd(), path), "utf8");
}

describe("workspace model selector popover", () => {
  test("opens above its trigger without a modal overlay", () => {
    const popover = source(
      "src/components/workspace/model-selector-popover.tsx",
    );

    expect(popover).toContain("DropdownMenu");
    expect(popover).toContain("modal = false");
    expect(popover).toContain('side = "top"');
    expect(popover).toContain('align = "end"');
    expect(popover).toContain('className={cn("w-70"');
    expect(popover).toContain("setOpen(false)");
    expect(popover).not.toContain("DialogContent");
  });

  test("is used by the primary and sidecar composers", () => {
    const primary = source("src/components/workspace/input-box.tsx");
    const sidecar = source(
      "src/components/workspace/sidecar/sidecar-panel.tsx",
    );

    expect(primary).toContain('from "./model-selector-popover"');
    expect(sidecar).toContain('from "../model-selector-popover"');
    expect(primary).not.toContain("<ModelSelectorInput");
    expect(sidecar).not.toContain("<ModelSelectorInput");
  });
});
