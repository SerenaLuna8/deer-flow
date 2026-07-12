import { redirect } from "next/navigation";

import { ProjectWorkbenchPage } from "@/components/projects/project-workbench-page";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export default function ProjectsPage() {
  if (isStaticWebsiteOnly()) redirect("/workspace");
  return <ProjectWorkbenchPage />;
}
