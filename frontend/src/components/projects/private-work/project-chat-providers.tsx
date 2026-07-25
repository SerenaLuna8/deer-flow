"use client";

import { useCallback } from "react";

import { PromptInputProvider } from "@/components/ai-elements/prompt-input";
import { useProjectDesktopNavigation } from "@/components/projects/project-shell";
import { StandaloneArtifactsProvider } from "@/components/workspace/artifacts";
import { SubtasksProvider } from "@/core/tasks/context";

export function ProjectChatProviders({
  children,
}: {
  children: React.ReactNode;
}) {
  const { setCollapsed } = useProjectDesktopNavigation();
  const handleNavigationOpenChange = useCallback(
    (open: boolean) => setCollapsed(!open),
    [setCollapsed],
  );

  return (
    <SubtasksProvider>
      <StandaloneArtifactsProvider
        enabled={true}
        onNavigationOpenChange={handleNavigationOpenChange}
      >
        <PromptInputProvider>{children}</PromptInputProvider>
      </StandaloneArtifactsProvider>
    </SubtasksProvider>
  );
}
