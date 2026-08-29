import { AdminKnowledgeApiError } from "@/core/admin-settings/knowledge/api";
import type { Translations } from "@/core/i18n/locales/types";
import { KnowledgeApiError } from "@/core/knowledge/api";

/**
 * Backend Knowledge errors already carry a user-facing message in the error
 * envelope; prefer it and fall back to a generic translation otherwise.
 */
export function knowledgeErrorMessage(
  error: unknown,
  messages: Translations["knowledge"]["errors"],
): string {
  if (
    error instanceof KnowledgeApiError ||
    error instanceof AdminKnowledgeApiError
  ) {
    if (error.serverMessage) return error.serverMessage;
    if (error.code === "NETWORK_ERROR") return messages.network;
    if (error.code === "INVALID_RESPONSE") return messages.invalidResponse;
  }
  return messages.generic;
}
