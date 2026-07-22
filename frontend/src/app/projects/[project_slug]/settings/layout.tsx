import { ProjectSettingsShell } from "@/components/projects/settings/project-settings-shell";

export default function ProjectSettingsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <ProjectSettingsShell>{children}</ProjectSettingsShell>;
}
