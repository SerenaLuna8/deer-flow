import { GatewayApiError } from "@/core/api/errors";

export const MEMORY_DREAM_MODEL_UNAVAILABLE_CODE =
  "MEMORY_DREAM_MODEL_UNAVAILABLE";

type MemoryDreamErrorCopy = {
  dreamFailed: string;
  dreamModelUnavailable: string;
};

export function memoryDreamErrorMessage(
  error: unknown,
  copy: MemoryDreamErrorCopy,
): string {
  if (
    error instanceof GatewayApiError &&
    error.code === MEMORY_DREAM_MODEL_UNAVAILABLE_CODE
  ) {
    return copy.dreamModelUnavailable;
  }
  return error instanceof Error && error.message
    ? error.message
    : copy.dreamFailed;
}
