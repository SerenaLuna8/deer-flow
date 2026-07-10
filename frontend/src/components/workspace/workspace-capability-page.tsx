import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";

export function WorkspaceCapabilityPage({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div className="w-full max-w-5xl p-6 md:p-8">{children}</div>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
