import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";
import { cn } from "@/lib/utils";

export function WorkspaceCapabilityPage({
  children,
  width = "default",
}: Readonly<{
  children: React.ReactNode;
  width?: "default" | "wide";
}>) {
  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="overflow-y-auto">
        <div
          className={cn(
            "w-full p-4 sm:p-6 md:p-8",
            width === "wide" ? "max-w-[1440px]" : "max-w-5xl",
          )}
        >
          {children}
        </div>
      </WorkspaceBody>
    </WorkspaceContainer>
  );
}
