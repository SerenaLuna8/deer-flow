import { ProjectLifecyclePanel } from "@/components/projects/settings/project-lifecycle-panel";

export default function ProjectSettingsRoute() {
  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
      <h1 className="text-3xl font-semibold tracking-tight">项目设置</h1>
      <p className="text-muted-foreground mt-3 max-w-2xl">
        管理项目生命周期和治理设置。
      </p>
      <ProjectLifecyclePanel />
    </main>
  );
}
