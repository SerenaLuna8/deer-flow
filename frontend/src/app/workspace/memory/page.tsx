import { MemorySettingsPage } from "@/components/workspace/settings/memory-settings-page";
import { WorkspaceCapabilityPage } from "@/components/workspace/workspace-capability-page";

export default function MemoryPage() {
  return (
    <WorkspaceCapabilityPage>
      <MemorySettingsPage />
    </WorkspaceCapabilityPage>
  );
}
