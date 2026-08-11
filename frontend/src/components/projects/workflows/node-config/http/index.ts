import type { WorkflowNodeConfigPanel } from "@/components/projects/workflows/node-config/contracts";

import { HttpRequestNodeConfigPanel } from "./http-request-node-config-panel";

export { HttpCurlImportDialog } from "./http-curl-import-dialog";
export {
  applyHttpCurlDialog,
  buildHttpRequestNodeConfigUpdate,
  closeHttpCurlDialog,
  createHttpCurlDialogState,
  httpBaseOriginIsSafe,
  httpCredentialSlotIds,
  httpCredentialSlotState,
  httpHeaderNameIsSafe,
  httpMethodIsWrite,
  httpPathTemplateIsSafe,
  httpQueryNameIsSafe,
  httpRequestConfigIssues,
  previewHttpCurlDialog,
  safeHttpMethod,
  selectHttpEndpointConfig,
  selectHttpInjectionProfileAuth,
} from "./http-node-config-helpers";
export { HttpRequestNodeConfigPanel } from "./http-request-node-config-panel";

export const HTTP_WORKFLOW_NODE_CONFIG_PANELS = Object.freeze({
  http_request: HttpRequestNodeConfigPanel,
} satisfies Record<"http_request", WorkflowNodeConfigPanel>);
