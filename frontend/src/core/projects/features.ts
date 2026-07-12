export const PROJECT_PRIVATE_WORKSPACE = false as const;
export const PROJECT_FIRST_MODE = true as const;

export function workspaceLandingPath(
  staticMode: boolean,
  demoThreadId: string | null,
): string {
  if (staticMode) {
    return demoThreadId
      ? `/workspace/chats/${demoThreadId}`
      : "/workspace/chats/new";
  }
  return PROJECT_FIRST_MODE ? "/workspace" : "/workspace/chats/new";
}
