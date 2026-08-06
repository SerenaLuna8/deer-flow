export type SkillBuilderIdempotencyChannel =
  | "create"
  | "message-turn"
  | "clarification-turn"
  | "draft-turn"
  | "validate"
  | "commit"
  | "cancel";

type IdempotencyEntry = {
  signature: string;
  value: unknown;
};

export type SkillBuilderIdempotencyRegistry = {
  acquire<T>(
    channel: SkillBuilderIdempotencyChannel,
    signature: string,
    create: (idempotencyKey: string) => T,
  ): T;
  complete(channel: SkillBuilderIdempotencyChannel, signature: string): void;
};

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
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

export function skillBuilderSemanticSignature(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

export function createSkillBuilderIdempotencyRegistry(
  createKey: () => string = () => crypto.randomUUID(),
): SkillBuilderIdempotencyRegistry {
  const entries = new Map<SkillBuilderIdempotencyChannel, IdempotencyEntry>();
  return {
    acquire<T>(
      channel: SkillBuilderIdempotencyChannel,
      signature: string,
      create: (idempotencyKey: string) => T,
    ) {
      const existing = entries.get(channel);
      if (existing?.signature === signature) return existing.value as T;
      const value = create(createKey());
      entries.set(channel, { signature, value });
      return value;
    },
    complete(channel, signature) {
      if (entries.get(channel)?.signature === signature) {
        entries.delete(channel);
      }
    },
  };
}
