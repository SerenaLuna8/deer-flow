"use client";

import { PromptInputProvider } from "@/components/ai-elements/prompt-input";
import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { SubtasksProvider } from "@/core/tasks/context";

export function ProjectChatProviders({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <SubtasksProvider>
      <StandaloneArtifactsProvider enabled={true}>
        <PromptInputProvider>{children}</PromptInputProvider>
      </StandaloneArtifactsProvider>
    </SubtasksProvider>
  );
}
