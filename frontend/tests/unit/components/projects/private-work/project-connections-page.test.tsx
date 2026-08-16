import { describe, expect, test } from "@rstest/core";

import { isVisibleProjectChannelProvider } from "@/components/projects/private-work/project-connections-page";

describe("project connections channel visibility", () => {
  test("keeps only Feishu visible in project channel management", () => {
    expect(isVisibleProjectChannelProvider("feishu")).toBe(true);
    expect(isVisibleProjectChannelProvider("slack")).toBe(false);
    expect(isVisibleProjectChannelProvider("telegram")).toBe(false);
    expect(isVisibleProjectChannelProvider("discord")).toBe(false);
    expect(isVisibleProjectChannelProvider("dingtalk")).toBe(false);
    expect(isVisibleProjectChannelProvider("wechat")).toBe(false);
    expect(isVisibleProjectChannelProvider("wecom")).toBe(false);
  });
});
