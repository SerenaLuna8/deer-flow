export interface SuggestionsConfigResponse {
  enabled: boolean;
}

export async function loadSuggestionsConfig(): Promise<SuggestionsConfigResponse> {
  // Follow-up generation is project-scoped. The removed global configuration
  // endpoint no longer controls whether the project composer requests it.
  return { enabled: true };
}
