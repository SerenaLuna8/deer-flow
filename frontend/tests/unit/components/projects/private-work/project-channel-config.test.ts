import { describe, expect, test } from "@rstest/core";

import { buildProjectChannelInstanceInput } from "@/components/projects/private-work/project-channel-config";

describe("Project Channel write-only form", () => {
  test("confirmed clear permits a later public identity edit without a bundle", () => {
    const form = new FormData();
    form.set("client_id", "client-after-clear");
    expect(buildProjectChannelInstanceInput("dingtalk", true, form)).toEqual({
      publicConfig: { client_id: "client-after-clear" },
      enabled: true,
    });
  });

  test("a new instance can save public configuration as unready", () => {
    const form = new FormData();
    form.set("client_id", "new-client");
    expect(buildProjectChannelInstanceInput("dingtalk", false, form)).toEqual({
      publicConfig: { client_id: "new-client" },
      enabled: false,
    });
  });

  test("a new instance with a secret may be enabled immediately", () => {
    const form = new FormData();
    form.set("client_id", "new-client");
    form.set("client_secret", "new-secret");

    expect(buildProjectChannelInstanceInput("dingtalk", false, form)).toEqual({
      publicConfig: { client_id: "new-client" },
      secrets: { client_secret: "new-secret" },
      enabled: true,
    });
  });
});
