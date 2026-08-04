import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  createGroupBindingChallenge,
  findCompletedGroupBinding,
  groupBindingChallengeExpiryLabel,
  projectChannelGroupBindingErrorMessage,
  ProjectChannelGroupBindingRows,
  ProjectChannelGroupBindingStartControl,
} from "@/components/projects/private-work/project-channel-group-bindings";
import { GatewayApiError } from "@/core/api/errors";

const agentAssetId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const binding = {
  id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  provider: "feishu",
  display_name: "产品讨论群",
  status: "active" as const,
  agent_asset_id: agentAssetId,
  agent_scope: "project" as const,
  last_activity_at: "2026-08-03T09:00:00+00:00",
  revision: 3,
  created_at: "2026-08-03T08:00:00+00:00",
  updated_at: "2026-08-03T09:00:00+00:00",
};

describe("project channel group bindings", () => {
  test("posts an unbound Main challenge without browser-side runtime preparation", async () => {
    const calls: string[] = [];
    const main = {
      id: "55555555-5555-4555-8555-555555555555",
      scope: "system" as const,
      displayName: "Main",
      available: true,
    };
    const challenge = {
      provider: "feishu",
      code: "challenge-code",
      command: "/bind-project challenge-code",
      expires_at: "2026-08-03T09:10:00+00:00",
      expires_in: 600,
    };
    const createChallenge = rs.fn(async () => {
      calls.push("post");
      return challenge;
    });

    await expect(
      createGroupBindingChallenge({
        provider: "feishu",
        agent: main,
        createChallenge,
      }),
    ).resolves.toEqual(challenge);
    expect(calls).toEqual(["post"]);
    expect(createChallenge).toHaveBeenCalledWith({
      provider: "feishu",
      agentAssetId: main.id,
      agentScope: "system",
    });
  });

  test("renders group name, Agent, green active state, recent activity, and concise actions", () => {
    const html = renderToStaticMarkup(
      <ProjectChannelGroupBindingRows
        bindings={[binding]}
        agents={[
          {
            id: agentAssetId,
            scope: "project",
            displayName: "客户支持 Agent",
            available: true,
          },
        ]}
        manageable
        pendingBindingId={null}
        onEditAgent={rs.fn()}
        onToggle={rs.fn()}
        onDelete={rs.fn()}
      />,
    );

    expect(html).toContain("产品讨论群");
    expect(html).toContain("客户支持 Agent");
    expect(html).toContain("运行中");
    expect(html).toContain('role="status"');
    expect(html).toContain("bg-success");
    expect(html).toContain("最近活动");
    expect(html).toContain("修改 Agent");
    expect(html).toContain("停用");
    expect(html).toContain("删除");
    expect(html).not.toContain("chat_id");
    expect(html).not.toContain("channel_instance_id");
    expect(html).not.toContain('data-slot="badge"');
  });

  test("keeps state visible but hides management actions without capability", () => {
    const html = renderToStaticMarkup(
      <ProjectChannelGroupBindingRows
        bindings={[
          {
            id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            provider: "feishu",
            display_name: "设计评审群",
            status: "disabled",
            agent_asset_id: agentAssetId,
            agent_scope: "system",
            last_activity_at: null,
            revision: 1,
            created_at: "2026-08-03T08:00:00+00:00",
            updated_at: "2026-08-03T08:00:00+00:00",
          },
        ]}
        agents={[]}
        manageable={false}
        pendingBindingId={null}
        onEditAgent={rs.fn()}
        onToggle={rs.fn()}
        onDelete={rs.fn()}
      />,
    );

    expect(html).toContain("设计评审群");
    expect(html).toContain("已停用");
    expect(html).toContain("Agent 不可用");
    expect(html).toContain("暂无活动");
    expect(html).not.toContain("修改 Agent");
    expect(html).not.toContain(">启用<");
    expect(html).not.toContain(">删除<");
  });

  test("uses clear messages for not-bound, expired, and unavailable-Agent outcomes", () => {
    expect(
      projectChannelGroupBindingErrorMessage(
        new GatewayApiError(
          404,
          "CHANNEL_GROUP_BINDING_NOT_FOUND",
          "not found",
        ),
      ),
    ).toBe("尚未检测到群聊连接，请在飞书群发送命令后重试。");
    expect(
      projectChannelGroupBindingErrorMessage(
        new GatewayApiError(
          410,
          "CHANNEL_GROUP_BINDING_CHALLENGE_EXPIRED",
          "expired",
        ),
      ),
    ).toBe("绑定命令已失效，请重新生成。");
    expect(
      projectChannelGroupBindingErrorMessage(
        new GatewayApiError(
          409,
          "CHANNEL_GROUP_BINDING_AGENT_UNAVAILABLE",
          "agent unavailable",
        ),
      ),
    ).toBe("所选 Agent 当前不可用，请选择其他 Agent。");
    expect(groupBindingChallengeExpiryLabel(0)).toBe("绑定命令已失效");
    expect(groupBindingChallengeExpiryLabel(75)).toBe("2 分钟内有效");
  });

  test("disables new binding with a short channel-state error", () => {
    const html = renderToStaticMarkup(
      <ProjectChannelGroupBindingStartControl
        manageable
        enabled={false}
        blockedReason="请先启用飞书渠道。"
        resumable={false}
        onStart={rs.fn()}
      />,
    );

    expect(html).toContain("绑定群聊");
    expect(html).toContain("disabled");
    expect(html).toContain("请先启用飞书渠道。");
    expect(html).not.toContain("一次填写");
    expect(html).not.toContain("等待项目 Admin 审批");
  });

  test("detects both a new group and a successfully rebound existing group", () => {
    const guide = {
      challenge: {
        provider: "feishu",
        code: "challenge-code",
        command: "/bind-project challenge-code",
        expires_at: "2026-08-03T09:10:00+00:00",
        expires_in: 600,
      },
      agent: {
        id: agentAssetId,
        scope: "project" as const,
        displayName: "客户支持 Agent",
        available: true,
      },
      initialBindings: [{ id: binding.id, revision: binding.revision }],
    };

    expect(findCompletedGroupBinding([binding], guide)).toBeUndefined();
    expect(
      findCompletedGroupBinding(
        [{ ...binding, revision: binding.revision + 1 }],
        guide,
      )?.id,
    ).toBe(binding.id);
    expect(
      findCompletedGroupBinding(
        [
          binding,
          {
            ...binding,
            id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            display_name: "新群",
            revision: 1,
          },
        ],
        guide,
      )?.display_name,
    ).toBe("新群");
  });
});
