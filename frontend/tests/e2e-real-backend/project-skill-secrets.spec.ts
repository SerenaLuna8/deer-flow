import { gzipSync } from "node:zlib";

import { expect, test } from "@playwright/test";

import {
  registerReplayProject,
  type ReplayProjectScope,
} from "./project-fixture";

const APP =
  process.env.E2E_APP_URL ??
  `http://localhost:${process.env.E2E_FRONTEND_PORT ?? "3000"}`;
const REQUIRED_SECRET = "REAL_API_TOKEN";
const OPTIONAL_SECRET = "REAL_ANALYTICS_KEY";
const SECRET_VALUE = "playwright-secret-value-never-expose";

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

function gzipSingleFileTar(path: string, content: string): Buffer {
  const payload = Buffer.from(content, "utf8");
  const header = Buffer.alloc(512);
  header.write(path, 0, 100, "utf8");
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

function skillArchive(): Buffer {
  return gzipSingleFileTar(
    "SKILL.md",
    `---
name: real-secret-browser
description: Exercise Skill secret declarations through the real Gateway.
required-secrets:
  - name: ${REQUIRED_SECRET}
    optional: false
secrets-autonomous: true
---

# Real secret browser

Use the bound project Credential without exposing its value.
`,
  );
}

test.describe("project Skill secrets (real Gateway, no external model)", () => {
  let project: ReplayProjectScope;

  test.beforeEach(async ({ context }) => {
    project = await registerReplayProject(context, APP);
  });

  test("uploads, maps exact Credential fields, patches SKILL.md, and activates with CAS", async ({
    page,
    context,
  }) => {
    const credentialResponse = await context.request.post(
      `${APP}/api/projects/${project.id}/credentials`,
      {
        headers: { "X-CSRF-Token": project.csrf },
        data: {
          name: "real-skill-token",
          display_name: "Real Skill token",
          credential_type: "skill_auth",
          payload: { env: { [REQUIRED_SECRET]: SECRET_VALUE } },
        },
      },
    );
    expect(credentialResponse.status(), await credentialResponse.text()).toBe(
      201,
    );
    const credential = (await credentialResponse.json()) as {
      item?: { current_version_id?: unknown };
    };
    const credentialVersionId = credential.item?.current_version_id;
    expect(typeof credentialVersionId).toBe("string");
    if (typeof credentialVersionId !== "string") {
      throw new Error(
        "Credential create response is missing current_version_id",
      );
    }

    await page.goto(`/projects/${encodeURIComponent(project.slug)}/skills`);
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
    const initialVersionId = imported.version?.id;
    expect(typeof skillId).toBe("string");
    expect(typeof initialVersionId).toBe("string");
    if (typeof skillId !== "string" || typeof initialVersionId !== "string") {
      throw new Error("Skill import response is missing stable IDs");
    }

    await expect(page).toHaveURL(
      `/projects/${encodeURIComponent(project.slug)}/skills?skill_id=${skillId}&skill_version_id=${initialVersionId}&configure_credentials=1`,
    );
    const detail = page.getByRole("dialog", { name: "real-secret-browser" });
    await expect(detail).toBeVisible();
    await expect(
      detail.getByRole("tab", { name: "Runtime credentials" }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(
      detail.getByRole("heading", { name: "2. Project Credential mappings" }),
    ).toBeVisible();
    const bindingSelect = detail.getByLabel(
      `Project Credential · ${REQUIRED_SECRET}`,
    );
    await expect(bindingSelect).toHaveValue("");
    await bindingSelect.selectOption(credentialVersionId);
    const sourceFieldSelect = detail.getByLabel(
      `Source environment variable · ${REQUIRED_SECRET}`,
    );
    await sourceFieldSelect.selectOption(REQUIRED_SECRET);
    const [bindingResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "PUT" &&
          response
            .url()
            .endsWith(
              `/api/projects/${project.id}/skills/${skillId}/versions/${initialVersionId}/credential-bindings`,
            ),
      ),
      detail.getByRole("button", { name: "Save mappings" }).click(),
    ]);
    expect(bindingResponse.status(), await bindingResponse.text()).toBe(200);
    await expect(bindingSelect).toHaveValue(credentialVersionId);
    await expect(sourceFieldSelect).toHaveValue(REQUIRED_SECRET);

    await detail.getByRole("button", { name: "创建新版本" }).click();
    await detail.getByRole("tab", { name: "Runtime credentials" }).click();
    await expect(
      detail.getByRole("heading", {
        name: "1. Environment variable declarations",
      }),
    ).toBeVisible();
    await expect(
      detail.getByText("当前正在编辑 SKILL.md。", { exact: false }),
    ).toBeVisible();
    await expect(
      detail.getByLabel(new RegExp("Project Credential", "u")),
    ).toHaveCount(0);
    await detail.getByLabel("Variable name").fill(OPTIONAL_SECRET);
    await detail.getByLabel("Make the new variable optional").click();
    await detail.getByRole("button", { name: "Add" }).click();
    await detail.getByRole("button", { name: "View SKILL.md" }).click();
    const source = detail.getByLabel("编辑 SKILL.md");
    await expect(source).toHaveValue(
      new RegExp(`required-secrets:[\\s\\S]*name: "${OPTIONAL_SECRET}"`, "u"),
    );

    const [forkResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response
            .url()
            .endsWith(
              `/api/projects/${project.id}/skills/${skillId}/versions/${initialVersionId}/fork`,
            ),
      ),
      detail.getByRole("button", { name: "保存为新版本" }).click(),
    ]);
    expect(forkResponse.status(), await forkResponse.text()).toBe(201);
    const forked = (await forkResponse.json()) as { data?: { id?: unknown } };
    const candidateVersionId = forked.data?.id;
    expect(typeof candidateVersionId).toBe("string");
    if (typeof candidateVersionId !== "string") {
      throw new Error(
        "Skill fork response is missing the Candidate Version ID",
      );
    }

    await expect(
      detail.getByRole("tab", { name: "Runtime credentials" }),
    ).toHaveAttribute("aria-selected", "true");
    const candidateCredentialSelect = detail.getByLabel(
      `Project Credential · ${REQUIRED_SECRET}`,
    );
    const candidateSourceFieldSelect = detail.getByLabel(
      `Source environment variable · ${REQUIRED_SECRET}`,
    );
    await expect(candidateCredentialSelect).toHaveValue(credentialVersionId);
    await expect(candidateSourceFieldSelect).toHaveValue(REQUIRED_SECRET);
    await expect(
      detail.getByRole("button", { name: "Save mappings" }),
    ).toBeDisabled();

    const activationButton = detail.getByRole("button", { name: "激活版本" });
    await expect(activationButton).toBeEnabled();
    await activationButton.click();
    const activationDialog = page.getByRole("dialog", {
      name: "Activate Skill Candidate Version",
    });
    await expect(activationDialog).toBeVisible();
    await expect(
      activationDialog.getByLabel(new RegExp("Project Credential", "u")),
    ).toHaveCount(0);

    const [activationRequest, activationResponse] = await Promise.all([
      page.waitForRequest(
        (request) =>
          request.method() === "POST" &&
          request
            .url()
            .endsWith(
              `/api/projects/${project.id}/skills/${skillId}/versions/${candidateVersionId}/activate`,
            ),
      ),
      page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response
            .url()
            .endsWith(
              `/api/projects/${project.id}/skills/${skillId}/versions/${candidateVersionId}/activate`,
            ),
      ),
      activationDialog
        .getByRole("button", { name: "Activate version" })
        .click(),
    ]);
    expect(activationResponse.status(), await activationResponse.text()).toBe(
      200,
    );
    const activationBody = activationRequest.postDataJSON() as Record<
      string,
      unknown
    >;
    expect(activationBody).not.toHaveProperty("credential_bindings");
    expect(JSON.stringify(activationBody)).not.toContain(SECRET_VALUE);
    expect(Object.keys(activationBody).sort()).toEqual([
      "expected_binding_revision",
      "expected_payload_checksum",
      "expected_revision",
    ]);
    await expect(activationDialog).toHaveCount(0, { timeout: 30_000 });
  });
});
