import { expect, test } from "@playwright/test";

import {
  assertCredentialResponsesAreSafe,
  assertPrivateFixtureHidden,
  assertSharedAssetsAreSafe,
  assertViewerBoundaries,
  bindExecutableSystemAgent,
  changeMemberRole,
  createPinnedLiveAgent,
  createProject,
  createPrivateFixture,
  enterProject,
  expectCapabilities,
  expectPrivateRunNotFound,
  expectProjectNotFound,
  expectRole,
  expectRunTerminal,
  expectToolResultVisible,
  initializeAdmin,
  inviteRole,
  liveBrowserResult,
  listProjects,
  registerAccount,
  reloadAndResumeFromLastCursor,
  runRecoveryBrowserProbe,
  submitSyntheticToolPrompt,
  submitRecoveryAuthority,
} from "./support/m8-api";
import {
  type M8LiveBrowserResult,
  writeBrowserResult,
  writeRecoveryBrowserResult,
} from "./support/m8-result";

test("host release boundaries survive account and project transitions", async ({
  browser,
}) => {
  if (process.env.M8_RECOVERY_PROBE === "1") {
    const recovery = await runRecoveryBrowserProbe(browser);
    await writeRecoveryBrowserResult({
      schema_version: 1,
      phase: recovery.phase,
      boundaries_passed: recovery.boundariesPassed,
      failures: 0,
    });
    return;
  }
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
  let liveModel: M8LiveBrowserResult | null = null;
  let liveAuthority: {
    projectId: string;
    threadId: string;
    runId: string;
    artifactId: string;
  } | null = null;
  try {
    await initializeAdmin(systemAdmin);
    const accountAdminCredentials = await registerAccount(
      accountAdmin,
      "project-admin",
    );
    const outsiderCredentials = await registerAccount(outsider, "outsider");
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

    if (process.env.M8_DEEPSEEK_LIVE === "1") {
      const modelRef = process.env.M8_LOGICAL_MODEL_NAME;
      expect(modelRef).toBeTruthy();
      const live = await createPinnedLiveAgent(accountAdmin, projectB, {
        modelRef: modelRef!,
        toolGroups: ["file:read", "file:write"],
      });
      const livePage = await accountAdmin.newPage();
      await submitSyntheticToolPrompt(livePage, live);
      await expectRunTerminal(livePage, live);
      await expectToolResultVisible(livePage, live);
      await reloadAndResumeFromLastCursor(livePage, live);
      live.privateDenials += await expectPrivateRunNotFound(
        outsider,
        live.publicHandle,
      );
      liveModel = liveBrowserResult(live);
      liveAuthority = live.publicHandle;
      await livePage.close();
    }

    if (process.env.M8_CAPTURE_RECOVERY_AUTHORITY === "1") {
      expect(liveAuthority).not.toBeNull();
      await submitRecoveryAuthority({
        admin: {
          user_id: accountAdminCredentials.userId,
          email: accountAdminCredentials.email,
          password: accountAdminCredentials.password,
        },
        outsider: {
          user_id: outsiderCredentials.userId,
          email: outsiderCredentials.email,
          password: outsiderCredentials.password,
        },
        purge_project: { project_id: projectA.id, slug: projectA.slug },
        purge_thread_id: adminPrivate.threadId,
        purge_file_id: adminPrivate.fileId,
        live_project: { project_id: projectB.id, slug: projectB.slug },
        live: {
          project_id: liveAuthority!.projectId,
          thread_id: liveAuthority!.threadId,
          run_id: liveAuthority!.runId,
          artifact_id: liveAuthority!.artifactId,
        },
      });
    }

    await writeBrowserResult({
      schema_version: 1,
      boundaries_passed: 32,
      failures: 0,
      contexts: contexts.length,
      projects: 2,
      private_denials: 12,
      live_model: liveModel,
    });
  } finally {
    await Promise.all(contexts.map((context) => context.close()));
  }
});
