export const CHANNEL_PROVIDER_LABELS = {
  dingtalk: "DingTalk",
  discord: "Discord",
  feishu: "Feishu",
  slack: "Slack",
  telegram: "Telegram",
  wechat: "WeChat",
  wecom: "WeCom",
} as const;

export const CHANNEL_PROVIDER_IDS = Object.keys(
  CHANNEL_PROVIDER_LABELS,
) as (keyof typeof CHANNEL_PROVIDER_LABELS)[];

export type KnownChannelProviderId = keyof typeof CHANNEL_PROVIDER_LABELS;
export type ChannelProviderId = KnownChannelProviderId | (string & {});

export function labelOfChannelProvider(provider: string) {
  return (
    CHANNEL_PROVIDER_LABELS[provider as KnownChannelProviderId] ?? provider
  );
}
