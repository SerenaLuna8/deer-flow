export type AgentBuilderIdempotencyChannel =
  | "create"
  | "message-turn"
  | "clarification-turn"
  | "blueprint-turn"
  | "commit"
  | "cancel";

type IdempotencyEntry = {
  signature: string;
  value: unknown;
};

export type AgentBuilderIdempotencyRegistry = {
  acquire<T>(
    channel: AgentBuilderIdempotencyChannel,
    signature: string,
    create: (idempotencyKey: string) => T,
  ): T;
  complete(channel: AgentBuilderIdempotencyChannel, signature: string): void;
};

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .filter(([, entry]) => entry !== undefined)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, entry]) => [key, canonicalize(entry)]),
    );
  }
  return value;
}

export function agentBuilderSemanticSignature(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

export function createAgentBuilderIdempotencyRegistry(
  createKey: () => string = () => crypto.randomUUID(),
): AgentBuilderIdempotencyRegistry {
  const entries = new Map<AgentBuilderIdempotencyChannel, IdempotencyEntry>();

  return {
    acquire<T>(
      channel: AgentBuilderIdempotencyChannel,
      signature: string,
      create: (idempotencyKey: string) => T,
    ): T {
      const existing = entries.get(channel);
      if (existing?.signature === signature) {
        return existing.value as T;
      }

      const value = create(createKey());
      entries.set(channel, { signature, value });
      return value;
    },
    complete(channel: AgentBuilderIdempotencyChannel, signature: string) {
      if (entries.get(channel)?.signature === signature) {
        entries.delete(channel);
      }
    },
  };
}
