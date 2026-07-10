import { ToolSettingsPage } from "@/components/workspace/settings/tool-settings-page";
import { WorkspaceCapabilityPage } from "@/components/workspace/workspace-capability-page";

export default function ToolsPage() {
  return (
    <WorkspaceCapabilityPage>
      <ToolSettingsPage />
    </WorkspaceCapabilityPage>
  );
}
