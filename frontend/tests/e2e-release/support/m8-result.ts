import { constants } from "node:fs";
import { open } from "node:fs/promises";
import path from "node:path";

export interface M8BrowserResult {
  schema_version: 1;
  boundaries_passed: number;
  failures: number;
  contexts: number;
  projects: number;
  private_denials: number;
  live_model: M8LiveBrowserResult | null;
}

export interface M8LiveModelSummary {
  kind?: "live_model";
  provider: "deepseek";
  logical_model_name: string;
  provider_model_id: "deepseek-v4-pro";
  outcome: "completed" | "provider_rejected" | "failed";
  frame_count: number;
  tool_call_count: number;
  terminal_count: number;
  cursor_count: number;
  duration_ms: number;
}

export interface M8LiveBrowserResult {
  summary: M8LiveModelSummary;
  replay_passed: true;
  private_denials: number;
}

async function writeOwnedResult(result: object): Promise<void> {
  const rootValue = process.env.M8_BROWSER_OUTPUT_ROOT;
  const targetValue = process.env.M8_BROWSER_RESULT_PATH;
  if (!rootValue || !targetValue) {
    throw new Error("M8_BROWSER_RESULT_PATH_REQUIRED");
  }
  const root = path.resolve(rootValue);
  const target = path.resolve(targetValue);
  const relative = path.relative(root, target);
  if (
    relative === "" ||
    relative.startsWith("..") ||
    path.isAbsolute(relative)
  ) {
    throw new Error("M8_BROWSER_RESULT_PATH_INVALID");
  }
  const handle = await open(
    target,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  try {
    await handle.writeFile(`${JSON.stringify(result)}\n`, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
}

export async function writeBrowserResult(
  result: M8BrowserResult,
): Promise<void> {
  await writeOwnedResult(result);
}
