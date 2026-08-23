import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  QueryClient,
  QueryClientProvider,
  QueryObserver,
  type UseMutationResult,
} from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/admin-settings/system/api", () => ({
  fetchAdminSystemSettings: rs.fn(),
  replaceAdminSystemSettingsSection: rs.fn(async () => ({
    section: "agent_runtime",
  })),
  runAbortableAdminSystemSettingsMutation: rs.fn(
    async (_accountId: string, operation: (signal: AbortSignal) => unknown) =>
      operation(new AbortController().signal),
  ),
}));

import type {
  ReplaceSystemSettingsSectionInput,
  SystemSettingsMutationResponse,
} from "@/core/admin-settings/system";
import { useReplaceAdminSystemSettingsSection } from "@/core/admin-settings/system/hooks";
import { createAccountAgentRuntimeCacheHintListener } from "@/core/private-work/memory-freshness";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import { threadContextUsageQueryKey } from "@/core/threads/context-usage";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ID = "22222222-2222-4222-8222-222222222222";
const THREAD_ID = "33333333-3333-4333-8333-333333333333";
const SIBLING_TAB_ID = "44444444-4444-4444-8444-444444444444";

class FakeBroadcastChannel {
  static readonly channels = new Set<FakeBroadcastChannel>();

  readonly listeners = new Set<(event: { data: unknown }) => void>();

  constructor(readonly name: string) {
    FakeBroadcastChannel.channels.add(this);
  }

  postMessage(message: unknown): void {
    for (const channel of FakeBroadcastChannel.channels) {
      if (channel !== this && channel.name === this.name) {
        for (const listener of channel.listeners) listener({ data: message });
      }
    }
  }

  addEventListener(
    _type: "message",
    listener: (event: { data: unknown }) => void,
  ): void {
    this.listeners.add(listener);
  }

  removeEventListener(
    _type: "message",
    listener: (event: { data: unknown }) => void,
  ): void {
    this.listeners.delete(listener);
  }

  close(): void {
    this.listeners.clear();
    FakeBroadcastChannel.channels.delete(this);
  }
}

type ReplaceMutation = UseMutationResult<
  SystemSettingsMutationResponse,
  Error,
  ReplaceSystemSettingsSectionInput
>;

function HookProbe({ onReady }: { onReady: (value: ReplaceMutation) => void }) {
  onReady(useReplaceAdminSystemSettingsSection(ACCOUNT_ID));
  return null;
}

afterEach(() => {
  rs.clearAllMocks();
  rs.unstubAllGlobals();
  FakeBroadcastChannel.channels.clear();
});

describe("admin system settings mutation freshness", () => {
  test("refetches an active Thread context-usage query after agent_runtime is saved", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = privateWorkQueryKey(
      { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
      ...threadContextUsageQueryKey(THREAD_ID),
    );
    queryClient.setQueryData(queryKey, { estimated_tokens: 10 });
    const refetch = rs.fn(async () => ({ estimated_tokens: 20 }));
    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn: refetch,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    let mutation: ReplaceMutation | undefined;
    renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <HookProbe onReady={(value) => (mutation = value)} />
      </QueryClientProvider>,
    );

    await mutation?.mutateAsync({
      section: "agent_runtime",
      input: {
        expected_revision: 1,
        value: {} as never,
      },
    });

    expect(refetch).toHaveBeenCalledTimes(1);

    unsubscribe();
    queryClient.clear();
  });

  test("refreshes an active context-usage query in a sibling tab after agent_runtime is saved", async () => {
    rs.stubGlobal("BroadcastChannel", FakeBroadcastChannel);
    const sourceQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const siblingQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const queryKey = privateWorkQueryKey(
      { accountId: ACCOUNT_ID, projectId: PROJECT_ID },
      ...threadContextUsageQueryKey(THREAD_ID),
    );
    siblingQueryClient.setQueryData(queryKey, { estimated_tokens: 10 });
    const refetch = rs.fn(async () => ({ estimated_tokens: 20 }));
    const observer = new QueryObserver(siblingQueryClient, {
      queryKey,
      queryFn: refetch,
      staleTime: Number.POSITIVE_INFINITY,
    });
    const unsubscribe = observer.subscribe(() => undefined);
    const siblingListener = createAccountAgentRuntimeCacheHintListener({
      queryClient: siblingQueryClient,
      accountId: ACCOUNT_ID,
      tabId: SIBLING_TAB_ID,
    });
    let mutation: ReplaceMutation | undefined;
    renderToStaticMarkup(
      <QueryClientProvider client={sourceQueryClient}>
        <HookProbe onReady={(value) => (mutation = value)} />
      </QueryClientProvider>,
    );

    await mutation?.mutateAsync({
      section: "agent_runtime",
      input: {
        expected_revision: 1,
        value: {} as never,
      },
    });
    await Promise.resolve();

    expect(refetch).toHaveBeenCalledTimes(1);

    siblingListener.dispose();
    unsubscribe();
    sourceQueryClient.clear();
    siblingQueryClient.clear();
  });
});
