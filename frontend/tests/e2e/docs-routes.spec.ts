import { expect, test } from "@playwright/test";

const routes = [
  { path: "/en/docs", heading: "Fluva Documentation" },
  { path: "/zh/docs", heading: "Fluva 文档" },
  { path: "/en-US/docs", heading: "Fluva Documentation" },
  { path: "/zh-CN/docs", heading: "Fluva 文档" },
] as const;

for (const route of routes) {
  test(`${route.path} renders its documentation landing page`, async ({
    page,
  }) => {
    const response = await page.goto(route.path);

    expect(response?.status()).toBe(200);
    await expect(
      page.getByRole("heading", { level: 1, name: route.heading }),
    ).toBeVisible();
    await expect(page.locator("#__next_error__")).toHaveCount(0);
  });
}

test("the Harness versus App guide links to the real application docs", async ({
  page,
}) => {
  await page.goto("/en-US/docs/introduction/harness-vs-app");

  const appLink = page.getByRole("link", { name: "Fluva App" }).first();
  await expect(appLink).toHaveAttribute("href", "/en-US/docs/application");
  await appLink.click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Fluva App" }),
  ).toBeVisible();
});
