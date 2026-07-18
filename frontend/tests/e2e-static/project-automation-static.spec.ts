import { expect, test } from "@playwright/test";

test("static workspace stays local and project routes stay absent without API requests", async ({
  page,
}) => {
  const apiPaths: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/")) apiPaths.push(path);
  });
  await page.route("**/api/**", (route) => route.abort());

  await page.goto("/workspace");
  await expect(page).toHaveURL(/\/workspace$/u);
  await expect(page.getByTestId("static-workspace-demo")).toBeVisible();
  await expect(page.getByRole("link", { name: "Automations" })).toHaveCount(0);
  await expect(page.locator('a[href^="/projects/"]')).toHaveCount(0);

  const direct = await page.goto("/projects/demo/automations");
  expect(direct?.status()).toBe(404);
  await expect(page.getByText("This page could not be found.")).toBeVisible();

  expect(apiPaths).toEqual([]);
});
