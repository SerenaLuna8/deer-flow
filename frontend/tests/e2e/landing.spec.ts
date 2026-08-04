import { expect, test } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

test.describe("Root entry", () => {
  test("opens the authenticated workspace without rendering the landing page", async ({
    page,
  }) => {
    mockLangGraphAPI(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/workspace$/);
    await expect(page.getByTestId("project-workbench")).toBeVisible();
    await expect(page.getByRole("link", { name: /get started/i })).toHaveCount(
      0,
    );
  });
});
