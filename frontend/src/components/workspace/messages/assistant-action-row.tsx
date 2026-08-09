import type { ReactNode } from "react";

export function AssistantActionRow({ children }: { children: ReactNode }) {
  return (
    <div className="pointer-events-none mt-2 flex justify-start gap-1 opacity-0 transition-opacity duration-150 group-focus-within/assistant-turn:pointer-events-auto group-focus-within/assistant-turn:opacity-100 group-hover/assistant-turn:pointer-events-auto group-hover/assistant-turn:opacity-100">
      {children}
    </div>
  );
}
