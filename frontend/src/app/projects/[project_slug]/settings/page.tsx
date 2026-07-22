import { ProjectGeneralSettings } from "@/components/projects/settings/project-general-settings";
import { ProjectLifecyclePanel } from "@/components/projects/settings/project-lifecycle-panel";

export default function ProjectSettingsRoute() {
  return (
    <div className="space-y-8">
      <ProjectGeneralSettings />
      <ProjectLifecyclePanel />
    </div>
  );
}
