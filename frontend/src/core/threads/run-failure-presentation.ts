import type { Translations } from "@/core/i18n/locales/types";
import {
  CURRENT_UPLOAD_UNAVAILABLE,
  LLM_AUTHENTICATION_FAILED,
  LLM_CIRCUIT_OPEN,
  LLM_PROVIDER_BUSY,
  LLM_PROVIDER_UNAVAILABLE,
  LLM_QUOTA_EXCEEDED,
  LLM_REQUEST_FAILED,
  LOOP_FINALIZATION_FAILED,
  LOOP_SAFETY_LIMIT,
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  RUN_POLICY_STALE,
  SIDE_EFFECT_STATE_UNKNOWN,
  TOOL_CALL_CONTROL_STATE_INVALID,
  TOOL_EXECUTION_FAILED,
  type ProjectRunFailureCode,
} from "@/core/private-work/api-client";

export type RunFailureCopy = {
  title: string;
  description: string;
};

export function resolveRunFailureCopy(
  conversation: Translations["conversation"],
  failureCode: ProjectRunFailureCode | null,
): RunFailureCopy {
  switch (failureCode) {
    case MODEL_OUTPUT_LIMIT:
      return {
        title: conversation.modelOutputLimitTitle,
        description: conversation.modelOutputLimitDescription,
      };
    case LOOP_SAFETY_LIMIT:
      return {
        title: conversation.loopSafetyLimitTitle,
        description: conversation.loopSafetyLimitDescription,
      };
    case LOOP_FINALIZATION_FAILED:
      return {
        title: conversation.loopFinalizationFailedTitle,
        description: conversation.loopFinalizationFailedDescription,
      };
    case OUTPUT_DELIVERY_INCOMPLETE:
      return {
        title: conversation.outputDeliveryIncompleteTitle,
        description: conversation.outputDeliveryIncompleteDescription,
      };
    case SIDE_EFFECT_STATE_UNKNOWN:
      return {
        title: conversation.sideEffectStateUnknownTitle,
        description: conversation.sideEffectStateUnknownDescription,
      };
    case CURRENT_UPLOAD_UNAVAILABLE:
      return {
        title: conversation.currentUploadUnavailableTitle,
        description: conversation.currentUploadUnavailableDescription,
      };
    case LLM_QUOTA_EXCEEDED:
      return {
        title: conversation.modelQuotaExceededTitle,
        description: conversation.modelQuotaExceededDescription,
      };
    case LLM_AUTHENTICATION_FAILED:
      return {
        title: conversation.modelAuthenticationFailedTitle,
        description: conversation.modelAuthenticationFailedDescription,
      };
    case LLM_PROVIDER_BUSY:
      return {
        title: conversation.modelProviderBusyTitle,
        description: conversation.modelProviderBusyDescription,
      };
    case LLM_PROVIDER_UNAVAILABLE:
      return {
        title: conversation.providerUnavailableTitle,
        description: conversation.providerUnavailableDescription,
      };
    case LLM_CIRCUIT_OPEN:
      return {
        title: conversation.modelCircuitOpenTitle,
        description: conversation.modelCircuitOpenDescription,
      };
    case LLM_REQUEST_FAILED:
      return {
        title: conversation.modelRequestFailedTitle,
        description: conversation.modelRequestFailedDescription,
      };
    case TOOL_EXECUTION_FAILED:
      return {
        title: conversation.toolExecutionFailedTitle,
        description: conversation.toolExecutionFailedDescription,
      };
    case RUN_POLICY_STALE:
      return {
        title: conversation.runPolicyStaleTitle,
        description: conversation.runPolicyStaleDescription,
      };
    case TOOL_CALL_CONTROL_STATE_INVALID:
      return {
        title: conversation.toolCallControlStateInvalidTitle,
        description: conversation.toolCallControlStateInvalidDescription,
      };
    default:
      return {
        title: conversation.runFailedTitle,
        description: conversation.runFailedDescription,
      };
  }
}
