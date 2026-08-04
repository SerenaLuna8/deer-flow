import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

const LIVE_WORKSPACE_MARKERS = [
  "AuthProvider",
  "QueryClientProvider",
  "project-workbench-page",
  "workspace-live-layout",
  "/api/v1/auth/me",
] as const;

test("static workspace build excludes the live authenticated module graph", () => {
  const dist = resolve(process.cwd(), ".next-static");
  const trace = readFileSync(
    resolve(dist, "server/app/workspace/page.js.nft.json"),
    "utf8",
  );
  const clientManifest = readFileSync(
    resolve(dist, "server/app/workspace/page_client-reference-manifest.js"),
    "utf8",
  );
  const manifest = JSON.parse(
    clientManifest.slice(
      clientManifest.indexOf('={"moduleLoading"') + 1,
      clientManifest.lastIndexOf(";"),
    ),
  ) as {
    clientModules: Record<string, { chunks: string[] }>;
  };
  const workspaceChunks = readdirSync(
    resolve(dist, "static/chunks/app/workspace"),
  )
    .filter((filename) => filename.endsWith(".js"))
    .map((filename) =>
      readFileSync(
        resolve(dist, "static/chunks/app/workspace", filename),
        "utf8",
      ),
    )
    .join("\n");

  for (const marker of LIVE_WORKSPACE_MARKERS) {
    expect(trace, `trace contains ${marker}`).not.toContain(marker);
    expect(workspaceChunks, `workspace chunks contain ${marker}`).not.toContain(
      marker,
    );

    const workspaceClientChunks = Object.entries(manifest.clientModules)
      .filter(([modulePath]) => modulePath.includes(marker))
      .flatMap(([, entry]) => entry.chunks)
      .filter((chunk) => chunk.includes("static/chunks/app/workspace/"));
    expect(
      workspaceClientChunks,
      `client manifest maps ${marker} into workspace chunks`,
    ).toEqual([]);
  }
});

test("static workspace stays local and project routes stay absent without API requests", async ({
  page,
}) => {
  const apiPaths: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/")) apiPaths.push(path);
  });
  await page.route("**/api/**", (route) => route.abort());

  await page.goto("/");
  await expect(page).toHaveURL(/\/workspace$/u);
  await expect(page.getByTestId("static-workspace-demo")).toBeVisible();
  await expect(page.getByRole("link", { name: "Automations" })).toHaveCount(0);
  await expect(page.locator('a[href^="/projects/"]')).toHaveCount(0);

  const direct = await page.goto("/projects/demo/automations");
  expect(direct?.status()).toBe(404);
  await expect(page.getByText("This page could not be found.")).toBeVisible();

  expect(apiPaths).toEqual([]);
});
