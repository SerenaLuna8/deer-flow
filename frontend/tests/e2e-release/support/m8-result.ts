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
}

export async function writeBrowserResult(
  result: M8BrowserResult,
): Promise<void> {
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
