import { expect, test } from "@playwright/test";

import {
  assertCredentialResponsesAreSafe,
  assertPrivateFixtureHidden,
  assertSharedAssetsAreSafe,
  assertViewerBoundaries,
  bindExecutableSystemAgent,
  changeMemberRole,
  createProject,
  createPrivateFixture,
  enterProject,
  expectCapabilities,
  expectProjectNotFound,
  expectRole,
  initializeAdmin,
  inviteRole,
  listProjects,
  registerAccount,
} from "./support/m8-api";
import { writeBrowserResult } from "./support/m8-result";

test("host release boundaries survive account and project transitions", async ({
  browser,
}) => {
  const systemAdmin = await browser.newContext();
  const accountAdmin = await browser.newContext();
  const editor = await browser.newContext();
  const runner = await browser.newContext();
  const viewer = await browser.newContext();
  const outsider = await browser.newContext();
  const contexts = [
    systemAdmin,
    accountAdmin,
    editor,
    runner,
    viewer,
    outsider,
  ];
  try {
    await initializeAdmin(systemAdmin);
    await registerAccount(accountAdmin, "project-admin");
    await registerAccount(outsider, "outsider");
    const projectA = await createProject(accountAdmin, "alpha");
    const projectB = await createProject(accountAdmin, "beta");
    const agentId = await bindExecutableSystemAgent(accountAdmin, projectA.id);
    await inviteRole(accountAdmin, editor, projectA, "editor", "editor");
    await inviteRole(accountAdmin, runner, projectA, "runner", "runner");
    const viewerCredentials = await inviteRole(
      accountAdmin,
      viewer,
      projectA,
      "runner",
      "viewer-owner",
    );
    const adminPrivate = await createPrivateFixture(
      accountAdmin,
      projectA.id,
      agentId,
    );
    const viewerPrivate = await createPrivateFixture(
      viewer,
      projectA.id,
      agentId,
    );
    await changeMemberRole(
      accountAdmin,
      projectA.id,
      viewerCredentials.email,
      "viewer",
    );

    expect((await listProjects(accountAdmin)).map(({ id }) => id)).toEqual(
      expect.arrayContaining([projectA.id, projectB.id]),
    );
    await expectRole(editor, projectA.id, "editor");
    await expectRole(runner, projectA.id, "runner");
    await expectRole(viewer, projectA.id, "viewer");
    await expectCapabilities(
      accountAdmin,
      projectA.id,
      ["project.members.manage", "private_work.create", "shared_assets.edit"],
      [],
    );
    await expectCapabilities(
      editor,
      projectA.id,
      ["private_work.create", "shared_assets.edit"],
      ["project.members.manage", "mcp.credentials.approve"],
    );
    await expectCapabilities(
      runner,
      projectA.id,
      ["private_work.create", "shared_assets.execute"],
      ["shared_assets.edit", "project.members.manage"],
    );
    await expectCapabilities(
      viewer,
      projectA.id,
      ["private_work.read_own", "shared_assets.read"],
      ["private_work.create", "shared_assets.execute"],
    );
    await Promise.all(
      [accountAdmin, editor, runner, viewer].map((context) =>
        enterProject(context, projectA.id),
      ),
    );
    await expectProjectNotFound(outsider, projectA.id);
    await assertPrivateFixtureHidden(editor, projectA.id, adminPrivate);
    await assertPrivateFixtureHidden(runner, projectA.id, adminPrivate);
    await assertPrivateFixtureHidden(accountAdmin, projectB.id, adminPrivate);
    await Promise.all(
      [accountAdmin, editor, runner, viewer].map((context) =>
        assertSharedAssetsAreSafe(context, projectA.id),
      ),
    );
    await assertCredentialResponsesAreSafe(accountAdmin, projectA.id);
    await assertCredentialResponsesAreSafe(viewer, projectA.id);
    await assertViewerBoundaries(viewer, projectA.id, viewerPrivate);

    const adminPage = await systemAdmin.newPage();
    const userPage = await accountAdmin.newPage();
    expect((await userPage.goto("/workspace"))?.status()).toBe(200);
    expect((await adminPage.goto("/admin/assets/agents"))?.status()).toBe(200);
    expect((await userPage.goto("/admin/assets/agents"))?.status()).toBe(404);
    await userPage.goto(`/projects/${projectA.slug}`);
    await expect(userPage).toHaveURL(
      new RegExp(`/projects/${projectA.slug}$`, "u"),
    );
    await userPage.goto(`/projects/${projectB.slug}`);
    await expect(userPage).toHaveURL(
      new RegExp(`/projects/${projectB.slug}$`, "u"),
    );

    await writeBrowserResult({
      schema_version: 1,
      boundaries_passed: 32,
      failures: 0,
      contexts: contexts.length,
      projects: 2,
      private_denials: 12,
    });
  } finally {
    await Promise.all(contexts.map((context) => context.close()));
  }
});
