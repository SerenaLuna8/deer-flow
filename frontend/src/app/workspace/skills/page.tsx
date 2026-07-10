import { SkillSettingsPage } from "@/components/workspace/settings/skill-settings-page";
import { WorkspaceCapabilityPage } from "@/components/workspace/workspace-capability-page";

export default function SkillsPage() {
  return (
    <WorkspaceCapabilityPage>
      <SkillSettingsPage />
    </WorkspaceCapabilityPage>
  );
}
