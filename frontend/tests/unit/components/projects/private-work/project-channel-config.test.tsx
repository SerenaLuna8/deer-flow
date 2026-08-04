import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  buildProjectChannelInstanceInput,
  canManageProjectChannels,
  ChannelInstanceCard,
  channelInstanceStatusLabel,
  PROJECT_CHANNEL_PROVIDER_DESCRIPTORS,
  projectChannelConfigErrorMessage,
  submitProjectChannelInstanceForm,
} from "@/components/projects/private-work/project-channel-config";
import { GatewayApiError } from "@/core/api/errors";

const SECRET = "feishu-secret-sentinel-33f8";

describe("project channel configuration", () => {
  test("shows project configuration only for the server-issued capability", () => {
    expect(
      canManageProjectChannels([
        "private_work.create",
        "project.channels.manage",
      ]),
    ).toBe(true);
    expect(canManageProjectChannels(["private_work.create"])).toBe(false);
  });

  test("defines Feishu public and write-only fields without a secret default", () => {
    expect(PROJECT_CHANNEL_PROVIDER_DESCRIPTORS.feishu).toEqual({
      provider: "feishu",
      displayName: "飞书",
      publicFields: [
        {
          name: "app_id",
          label: "App ID",
          required: true,
          placeholder: "cli_xxxxxxxxxxxxxxxx",
        },
        {
          name: "domain",
          label: "Domain",
          required: false,
          placeholder: "https://open.feishu.cn",
        },
      ],
      secretFields: [
        { name: "app_secret", label: "App Secret", required: true },
      ],
    });
    expect(JSON.stringify(PROJECT_CHANNEL_PROVIDER_DESCRIPTORS)).not.toContain(
      SECRET,
    );
  });

  test("requires App Secret on create and omits a blank replacement secret", () => {
    const create = new FormData();
    create.set("app_id", "cli_public");
    expect(() =>
      buildProjectChannelInstanceInput("feishu", false, create),
    ).toThrow("App Secret");

    const update = new FormData();
    update.set("app_id", "cli_public");
    update.set("domain", "https://open.feishu.cn");
    update.set("app_secret", "");
    expect(buildProjectChannelInstanceInput("feishu", true, update)).toEqual({
      publicConfig: {
        app_id: "cli_public",
        domain: "https://open.feishu.cn",
      },
      enabled: true,
    });
  });

  test("preserves a disabled channel while its configuration is edited", () => {
    const update = new FormData();
    update.set("app_id", "cli_public");

    expect(
      buildProjectChannelInstanceInput("feishu", true, update, false),
    ).toEqual({
      publicConfig: { app_id: "cli_public" },
      enabled: false,
    });
  });

  test("clears write-only inputs before dispatching the imperative request", () => {
    const form = new FormData();
    form.set("app_id", "cli_public");
    form.set("app_secret", SECRET);
    const order: string[] = [];
    const onSubmit = rs.fn(() => {
      order.push("submit");
    });

    void submitProjectChannelInstanceForm({
      provider: "feishu",
      configured: false,
      form,
      clearSecrets: () => order.push("clear"),
      onSubmit,
    });

    expect(order).toEqual(["clear", "submit"]);
    expect(onSubmit).toHaveBeenCalledWith({
      publicConfig: { app_id: "cli_public" },
      credentials: { app_secret: SECRET },
      enabled: true,
    });
  });

  test("turns safe channel error metadata into an actionable localized message", () => {
    expect(
      projectChannelConfigErrorMessage(
        "feishu",
        new GatewayApiError(
          422,
          "CHANNEL_INSTANCE_INVALID",
          "Channel credentials are invalid.",
          ["credentials"],
        ),
      ),
    ).toBe("App Secret 无效，请重新填写后重试。");
    expect(
      projectChannelConfigErrorMessage(
        "feishu",
        new GatewayApiError(
          503,
          "CHANNEL_INSTANCE_UNAVAILABLE",
          "Channel connection storage is unavailable.",
        ),
      ),
    ).toBe("渠道凭据暂时无法保存，请稍后重试。");
    expect(
      projectChannelConfigErrorMessage("feishu", new Error("raw failure")),
    ).toBe("无法保存飞书配置，请稍后重试。");
  });

  test("renders concise state and management actions for a configured provider", () => {
    const html = renderToStaticMarkup(
      <ChannelInstanceCard
        instance={{
          id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          provider: "feishu",
          display_name: "Feishu",
          status: "running",
          enabled: true,
          configured: true,
          credential_configured: true,
          public_config: { app_id: "cli_public" },
          updated_at: "2026-08-03T08:00:00Z",
          last_error: null,
        }}
        manageable
        pendingAction={null}
        onConfigure={rs.fn()}
        onToggle={rs.fn()}
        onDelete={rs.fn()}
      />,
    );

    expect(channelInstanceStatusLabel("running")).toBe("运行正常");
    expect(html).toContain("飞书");
    expect(html).toContain("运行正常");
    expect(html).toContain('role="status"');
    expect(html).toContain('data-status="running"');
    expect(html).toContain('aria-label="渠道状态：运行正常"');
    expect(html).toContain("bg-success");
    expect(html).toContain("text-foreground");
    expect(html).not.toContain('data-slot="badge"');
    expect(html).toContain("cli_public");
    expect(html).toContain("App Secret 已配置");
    expect(html).toContain(">修改<");
    expect(html).toContain(">停用<");
    expect(html).toContain(">删除<");
    expect(html).not.toContain(SECRET);
  });

  test("renders an unconfigured provider without a duplicate credential state", () => {
    const html = renderToStaticMarkup(
      <ChannelInstanceCard
        instance={{
          id: null,
          provider: "slack",
          display_name: "Slack",
          status: "unconfigured",
          enabled: false,
          configured: false,
          credential_configured: false,
          public_config: {},
          updated_at: null,
          last_error: null,
        }}
        manageable
        pendingAction={null}
        onConfigure={rs.fn()}
        onToggle={rs.fn()}
        onDelete={rs.fn()}
      />,
    );

    expect(html).toContain('data-status="unconfigured"');
    expect(html).toContain("未配置");
    expect(html).not.toContain("凭据未配置");
    expect(html).not.toContain('data-slot="badge"');
    expect(html).toContain(">配置<");
    expect(html).not.toContain(">修改<");
    expect(html).not.toContain(">停用<");
    expect(html).not.toContain(">删除<");
  });

  test("keeps safe instance state visible but hides management actions without capability", () => {
    const html = renderToStaticMarkup(
      <ChannelInstanceCard
        instance={{
          id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          provider: "feishu",
          display_name: "Feishu",
          status: "running",
          enabled: true,
          configured: true,
          credential_configured: true,
          public_config: { app_id: "cli_public" },
          updated_at: "2026-08-03T08:00:00Z",
          last_error: null,
        }}
        manageable={false}
        pendingAction={null}
        onConfigure={rs.fn()}
        onToggle={rs.fn()}
        onDelete={rs.fn()}
      />,
    );

    expect(html).toContain("飞书");
    expect(html).toContain("运行正常");
    expect(html).not.toContain(">修改<");
    expect(html).not.toContain(">停用<");
    expect(html).not.toContain(">删除<");
  });
});
