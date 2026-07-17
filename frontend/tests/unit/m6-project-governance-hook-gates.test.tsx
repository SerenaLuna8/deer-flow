import { beforeEach, describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  notFound: rs.fn(() => {
    throw new Error("NOT_FOUND");
  }),
}));
rs.mock("@/components/projects/project-context", () => ({
  useCurrentProject: rs.fn(),
}));
rs.mock("@/core/private-work/readiness", () => ({
  projectPrivateWorkEntryEnabled: (
    featureEnabled: boolean,
    allowed: boolean,
    status?: string,
  ) => featureEnabled && allowed && status === "ready",
  useProjectPrivateWorkReadiness: rs.fn(() => ({})),
}));
rs.mock("@/core/project-automations/readiness", () => ({
  useProjectAutomationReadiness: rs.fn(() => ({})),
}));
rs.mock("@/core/project-governance/usage", () => ({
  useProjectUsage: rs.fn(() => ({ isSuccess: true })),
  useUpdateProjectQuotaLimits: rs.fn(() => ({})),
}));
rs.mock("@/core/project-governance/audit", () => ({
  useProjectAudit: rs.fn(() => ({ isSuccess: true })),
}));
rs.mock("@/core/static-mode", () => ({
  isStaticWebsiteOnly: rs.fn(() => false),
}));

import { ProjectAuditPage } from "@/components/projects/governance/project-audit-page";
import { ProjectUsagePage } from "@/components/projects/governance/project-usage-page";
import { useCurrentProject } from "@/components/projects/project-context";
import { ProjectDesktopNav } from "@/components/projects/project-nav";
import { I18nProvider } from "@/core/i18n/context";
import { PrivateWorkProvider } from "@/core/private-work/provider";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import { useProjectAudit } from "@/core/project-governance/audit";
import {
  useProjectUsage,
  useUpdateProjectQuotaLimits,
} from "@/core/project-governance/usage";
import type { Project } from "@/core/projects/types";
import { isStaticWebsiteOnly } from "@/core/static-mode";

const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const access: PrivateWorkAccess = {
  scope,
  client: {} as never,
  apiBaseURL: "/api/projects/alpha/private-work",
  queryKeyPrefix: [],
  reconnectOnMount: true,
};
const project: Project = {
  id: scope.projectId,
  slug: "alpha",
  display_name: "Alpha",
  description: "",
  icon: "folder",
  role: "admin",
  capabilities: ["project.read"],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

function render(children: React.ReactNode) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale="en-US">
      <PrivateWorkProvider access={access}>{children}</PrivateWorkProvider>
    </I18nProvider>,
  );
}

describe("M6 project governance hook gates", () => {
  beforeEach(() => {
    rs.clearAllMocks();
    rs.mocked(isStaticWebsiteOnly).mockReturnValue(false);
  });

  test("denied usage page mounts no usage query or mutation hook", () => {
    rs.mocked(useCurrentProject).mockReturnValue(project);
    expect(() => render(<ProjectUsagePage />)).toThrow("NOT_FOUND");
    expect(useProjectUsage).not.toHaveBeenCalled();
    expect(useUpdateProjectQuotaLimits).not.toHaveBeenCalled();
  });

  test("denied audit page mounts no audit query hook", () => {
    rs.mocked(useCurrentProject).mockReturnValue(project);
    expect(() => render(<ProjectAuditPage />)).toThrow("NOT_FOUND");
    expect(useProjectAudit).not.toHaveBeenCalled();
  });

  test("navigation mounts no governance hook without either capability", () => {
    render(
      <ProjectDesktopNav project={project} footer={<span>footer</span>} />,
    );
    expect(useProjectUsage).not.toHaveBeenCalled();
    expect(useProjectAudit).not.toHaveBeenCalled();
  });

  test("navigation mounts only the hook for its exact capability", () => {
    render(
      <ProjectDesktopNav
        project={{
          ...project,
          capabilities: ["project.read", "project.usage.read"],
        }}
        footer={<span>footer</span>}
      />,
    );
    expect(useProjectUsage).toHaveBeenCalledTimes(1);
    expect(useProjectAudit).not.toHaveBeenCalled();
  });
});
