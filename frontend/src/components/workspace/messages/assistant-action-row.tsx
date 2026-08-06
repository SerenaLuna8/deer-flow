import type { ReactNode } from "react";

export function AssistantActionRow({ children }: { children: ReactNode }) {
  return (
    <div className="sr-only mt-2 flex justify-start gap-1 group-focus-within/assistant-turn:not-sr-only group-hover/assistant-turn:not-sr-only">
      {children}
    </div>
  );
}
