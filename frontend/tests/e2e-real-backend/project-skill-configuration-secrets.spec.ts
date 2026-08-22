import { gzipSync } from "node:zlib";

import { expect, test, type Page } from "@playwright/test";

import {
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3000"}`;
const SECRET_NAME = "provider_key";
const TARGET_ENV = "REAL_API_TOKEN";
const FIRST_VALUE = "playwright-first-secret-never-expose";
const SECOND_VALUE = "playwright-second-secret-never-expose";

function writeTarOctal(
  header: Buffer,
  offset: number,
  length: number,
  value: number,
) {
  header.write(
    `${value.toString(8).padStart(length - 1, "0")}\0`,
    offset,
    length,
    "ascii",
  );
}

function skillArchive(): Buffer {
  const content = `---
name: real-secret-browser
description: Exercise domain-owned Skill secrets through the real Gateway.
required-secrets:
  - name: ${SECRET_NAME}
    target_env: ${TARGET_ENV}
    optional: false
secrets-autonomous: true
---

# Real secret browser

Read the declared target environment variable only while the Skill runs.
`;
  const payload = Buffer.from(content, "utf8");
  const header = Buffer.alloc(512);
  header.write("SKILL.md", 0, 100, "utf8");
  writeTarOctal(header, 100, 8, 0o644);
  writeTarOctal(header, 108, 8, 0);
  writeTarOctal(header, 116, 8, 0);
  writeTarOctal(header, 124, 12, payload.length);
  writeTarOctal(header, 136, 12, 0);
  header.fill(0x20, 148, 156);
  header.write("0", 156, 1, "ascii");
  header.write("ustar\0", 257, 6, "ascii");
  header.write("00", 263, 2, "ascii");
  header.write("playwright", 265, 32, "ascii");
  header.write("playwright", 297, 32, "ascii");
  const checksum = header.reduce((total, byte) => total + byte, 0);
  header.write(`${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8, "ascii");
  const padding = Buffer.alloc((512 - (payload.length % 512)) % 512);
  return gzipSync(
    Buffer.concat([header, payload, padding, Buffer.alloc(1024)]),
  );
}

type SkillSecretStatus = {
  revision: number;
  requirements: Array<{
    name: string;
    target_env: string;
    optional: boolean;
    configured: boolean;
    revision: number;
  }>;
};

type SecretMetadataResponse = {
  status(): number;
  headers(): Record<string, string>;
  text(): Promise<string>;
};

async function expectSafeSecretResponse(
  response: SecretMetadataResponse,
  configured: boolean,
  forbiddenValues: readonly string[],
): Promise<SkillSecretStatus> {
  const text = await response.text();
  expect(response.status(), text).toBe(200);
  expect(response.headers()["cache-control"]).toContain("no-store");
  for (const value of forbiddenValues) expect(text).not.toContain(value);
  const body = JSON.parse(text) as Record<string, unknown>;
  expect(Object.keys(body).sort()).toEqual([
    "readiness",
    "request_id",
    "requirements",
    "revision",
    "skill_id",
    "skill_version_id",
  ]);
  const requirements = body.requirements;
  expect(Array.isArray(requirements)).toBe(true);
  const requirement = (requirements as Array<Record<string, unknown>>)[0];
  expect(Object.keys(requirement!).sort()).toEqual([
    "configured",
    "name",
    "optional",
    "revision",
    "target_env",
  ]);
  expect(requirement).toMatchObject({
    name: SECRET_NAME,
    target_env: TARGET_ENV,
    optional: false,
    configured,
  });
  return body as unknown as SkillSecretStatus;
}

async function browserPersistenceDump(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const persisted: unknown[] = [
      [...Array.from({ length: localStorage.length }, (_, index) => index)].map(
        (index) => {
          const key = localStorage.key(index) ?? "";
          return [key, localStorage.getItem(key)];
        },
      ),
      [
        ...Array.from({ length: sessionStorage.length }, (_, index) => index),
      ].map((index) => {
        const key = sessionStorage.key(index) ?? "";
        return [key, sessionStorage.getItem(key)];
      }),
      document.cookie,
    ];
    if ("caches" in globalThis) {
      for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        for (const request of await cache.keys()) {
          persisted.push([
            name,
            request.url,
            await (await cache.match(request))?.text(),
          ]);
        }
      }
    }
    if (typeof indexedDB.databases === "function") {
      for (const info of await indexedDB.databases()) {
        if (!info.name) continue;
        const databaseName = info.name;
        const database = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open(databaseName, info.version);
          request.onsuccess = () => resolve(request.result);
          request.onerror = () =>
            reject(request.error ?? new Error("Unable to inspect IndexedDB"));
        });
        try {
          for (const storeName of database.objectStoreNames) {
            const values = await new Promise<unknown[]>((resolve, reject) => {
              const request = database
                .transaction(storeName, "readonly")
                .objectStore(storeName)
                .getAll();
              request.onsuccess = () => resolve(request.result);
              request.onerror = () =>
                reject(
                  request.error ??
                    new Error("Unable to inspect IndexedDB object store"),
                );
            });
            persisted.push([databaseName, storeName, values]);
          }
        } finally {
          database.close();
        }
      }
    }
    return JSON.stringify(persisted);
  });
}

test.describe("Project Skill Configuration Secrets (real Gateway)", () => {
  let project: ReplayProjectScope;

  test.beforeEach(async ({ context }) => {
    project = await registerReplayProject(context, APP);
  });

  test("removes Credential surfaces and enforces write-only preserve, replace, and confirmed clear", async ({
    page,
    context,
  }) => {
    const retiredApi = await context.request.get(
      `${APP}/api/projects/${project.id}/credentials`,
    );
    expect(retiredApi.status()).toBe(404);

    const retiredPage = await page.goto(
      `/projects/${encodeURIComponent(project.slug)}/credentials`,
    );
    expect(retiredPage?.status()).toBe(404);

    await page.goto(`/projects/${encodeURIComponent(project.slug)}/skills`);
    await expect(page.locator('a[href*="/credentials"]')).toHaveCount(0);
    await page.getByRole("tab", { name: /项目自建/u }).click();
    await page.getByRole("button", { name: "新建 Skill" }).click();
    await page.getByRole("menuitem", { name: "上传压缩包" }).click();
    await page.getByLabel("Skill 压缩包").setInputFiles({
      name: "real-secret-browser.tar.gz",
      mimeType: "application/gzip",
      buffer: skillArchive(),
    });
    const [importResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().endsWith(`/api/projects/${project.id}/skills/import`),
      ),
      page.getByRole("button", { name: "上传并创建" }).click(),
    ]);
    expect(importResponse.status(), await importResponse.text()).toBe(201);
    const imported = (await importResponse.json()) as {
      item?: { id?: unknown };
      version?: { id?: unknown };
    };
    const skillId = imported.item?.id;
    const versionId = imported.version?.id;
    if (typeof skillId !== "string" || typeof versionId !== "string") {
      throw new Error("Skill import response is missing stable IDs");
    }

    await expect(page).toHaveURL(
      `/projects/${encodeURIComponent(project.slug)}/skills?skill_id=${skillId}&skill_version_id=${versionId}&configure_secrets=1`,
    );
    const detail = page.getByRole("dialog", { name: "real-secret-browser" });
    await expect(
      detail.getByRole("tab", { name: /^(?:运行秘密|Runtime secrets)$/u }),
    ).toHaveAttribute("aria-selected", "true");
    const secretInput = detail.getByLabel(`${SECRET_NAME} 秘密值`);
    await expect(secretInput).toHaveValue("");
    await expect(detail.getByText("必需 · 未配置", { exact: true })).toBeVisible();

    await secretInput.fill(FIRST_VALUE);
    const firstResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/skills/${skillId}/versions/${versionId}/secrets`,
          ),
    );
    await detail.getByRole("button", { name: "保存非空秘密值" }).click();
    await expect(secretInput).toHaveValue("");
    const first = await expectSafeSecretResponse(
      await firstResponsePromise,
      true,
      [FIRST_VALUE],
    );
    const firstRevision = first.requirements[0]!.revision;
    await expect(detail.getByText("必需 · 已配置", { exact: true })).toBeVisible();

    const blankPreserveResponse = await context.request.put(
      `${APP}/api/projects/${project.id}/skills/${skillId}/versions/${versionId}/secrets`,
      {
        headers: { "X-CSRF-Token": project.csrf },
        data: { secrets: {} },
      },
    );
    const preserved = await expectSafeSecretResponse(
      blankPreserveResponse,
      true,
      [FIRST_VALUE],
    );
    expect(preserved.requirements[0]!.revision).toBe(firstRevision);

    await secretInput.fill(SECOND_VALUE);
    const replacementResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response
          .url()
          .endsWith(
            `/api/projects/${project.id}/skills/${skillId}/versions/${versionId}/secrets`,
          ),
    );
    await detail.getByRole("button", { name: "保存非空秘密值" }).click();
    await expect(secretInput).toHaveValue("");
    const replaced = await expectSafeSecretResponse(
      await replacementResponsePromise,
      true,
      [FIRST_VALUE, SECOND_VALUE],
    );
    expect(replaced.requirements[0]!.revision).toBe(firstRevision + 1);

    await detail.getByRole("button", { name: "清除" }).click();
    const clearDialog = page.getByRole("dialog", { name: "清除秘密值？" });
    await expect(clearDialog).toBeVisible();
    const clearRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request.url().endsWith(`/${SECRET_NAME}/clear`),
    );
    const clearResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().endsWith(`/${SECRET_NAME}/clear`),
    );
    await clearDialog.getByRole("button", { name: "确认清除" }).click();
    expect((await clearRequestPromise).postDataJSON()).toEqual({
      confirmed: true,
    });
    const cleared = await expectSafeSecretResponse(
      await clearResponsePromise,
      false,
      [FIRST_VALUE, SECOND_VALUE],
    );
    expect(cleared.requirements[0]!.revision).toBe(firstRevision + 2);
    await expect(detail.getByText("必需 · 未配置", { exact: true })).toBeVisible();
    await expect(secretInput).toHaveValue("");

    const persisted = await browserPersistenceDump(page);
    expect(persisted).not.toContain(FIRST_VALUE);
    expect(persisted).not.toContain(SECOND_VALUE);
  });
});
