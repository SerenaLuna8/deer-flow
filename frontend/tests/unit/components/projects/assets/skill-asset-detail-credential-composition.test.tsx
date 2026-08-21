import { beforeEach, describe, expect, rs, test } from "@rstest/core";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  SkillAssetDetail,
  type SkillAssetVersion,
} from "@/components/projects/assets/skill-asset-detail";
import { I18nProvider } from "@/core/i18n/context";
import type { ProjectAssetItem } from "@/core/shared-assets";

type CapturedWorkbenchProps = {
  credentialBindings?: ReactNode;
};
type CapturedBindingsProps = {
  versionId: string;
  canManage: boolean;
  readOnlyReason?: string;
};

let capturedWorkbenchProps: CapturedWorkbenchProps | null = null;
let capturedBindingsProps: CapturedBindingsProps | null = null;

rs.mock("@/components/projects/assets/skill-version-workbench", () => ({
  SkillVersionWorkbench: (props: CapturedWorkbenchProps) => {
    capturedWorkbenchProps = props;
    return (
      <section data-testid="skill-version-workbench">
        {props.credentialBindings}
      </section>
    );
  },
}));

rs.mock("@/components/projects/assets/skill-credential-bindings", () => ({
  SkillCredentialBindings: (props: CapturedBindingsProps) => {
    capturedBindingsProps = props;
    return <div data-testid="exact-version-credential-bindings" />;
  },
}));

const SKILL_ID = "11111111-1111-4111-8111-111111111111";
const CURRENT_VERSION_ID = "22222222-2222-4222-8222-222222222222";
const CANDIDATE_VERSION_ID = "33333333-3333-4333-8333-333333333333";
const HISTORICAL_VERSION_ID = "44444444-4444-4444-8444-444444444444";
const DESIGN_SESSION_ID = "55555555-5555-4555-8555-555555555555";

const skillFile: SkillAssetVersion["file_views"][number] = {
  path: "SKILL.md",
  media_type: "text/markdown",
  size_bytes: 32,
  sha256: "a".repeat(64),
};

function renderDetail({
  versionId = CURRENT_VERSION_ID,
  relation = "current",
  editing = false,
  credentialBindingsDirty = false,
  currentVersionId = CURRENT_VERSION_ID,
  scope = "project",
  designRecordHref = null,
}: {
  versionId?: string;
  relation?: "current" | "candidate" | "historical";
  editing?: boolean;
  credentialBindingsDirty?: boolean;
  currentVersionId?: string;
  scope?: "project" | "system";
  designRecordHref?: string | null;
} = {}): string {
  const item = {
    id: SKILL_ID,
    scope,
    status: "active",
    current_version_id: currentVersionId,
  } as ProjectAssetItem;

  return renderToStaticMarkup(
    <I18nProvider initialLocale="zh-CN">
      <SkillAssetDetail
        designRecordHref={designRecordHref}
        version={{
          id: versionId,
          skill_id: SKILL_ID,
          version_number: relation === "current" ? 1 : 2,
          relation,
          description: "Credential flow fixture",
          frontmatter: {},
          compatibility: null,
          secret_requirements: [{ name: "API_KEY", optional: false }],
          scan_decision: "allow",
          scan_rule_ids: [],
          scan_summary: {},
          file_views: [skillFile],
          supersedes_version_id: null,
          payload_checksum: "b".repeat(64),
          revoked_at: null,
          revoked_by_user_id: null,
          revocation_reason_code: null,
          governance_status: "active",
          binding_eligible: relation === "current",
          created_by_user_id: "owner-1",
          created_at: "2026-08-19T00:00:00Z",
        }}
        workspace={{
          accountId: "account-1",
          projectId: "project-1",
          item,
          canAuthor: true,
          editing,
          credentialBindingsDirty,
          onEditingChange: () => undefined,
          onDirtyChange: () => undefined,
          onActivationValidityChange: () => undefined,
          onVersionCreated: () => undefined,
          canManageCredentials: true,
          credentialsHref: "/projects/demo/credentials",
          focusCredentials: false,
          onCredentialsFocused: () => undefined,
          onCredentialBindingsDirtyChange: () => undefined,
        }}
      />
    </I18nProvider>,
  );
}

describe("Skill detail running Credential composition", () => {
  beforeEach(() => {
    capturedWorkbenchProps = null;
    capturedBindingsProps = null;
  });

  test("passes Current Version bindings into the workbench tab panel slot", () => {
    const html = renderDetail();

    expect(capturedWorkbenchProps?.credentialBindings).toBeTruthy();
    expect(html).toContain(
      '<section data-testid="skill-version-workbench"><div data-testid="exact-version-credential-bindings"></div></section>',
    );
  });

  test("does not pass Current Version bindings into a new-version editing session", () => {
    const html = renderDetail({ editing: true });

    expect(capturedWorkbenchProps?.credentialBindings).toBeNull();
    expect(html).not.toContain(
      'data-testid="exact-version-credential-bindings"',
    );
  });

  test("mounts a writable exact-version mapping editor for a Candidate Version", () => {
    const html = renderDetail({
      versionId: CANDIDATE_VERSION_ID,
      relation: "candidate",
    });

    expect(html).toContain('data-testid="exact-version-credential-bindings"');
    expect(capturedBindingsProps).toMatchObject({
      versionId: CANDIDATE_VERSION_ID,
      canManage: true,
    });
  });

  test("mounts historical mappings read-only", () => {
    const html = renderDetail({
      versionId: HISTORICAL_VERSION_ID,
      relation: "historical",
    });

    expect(html).toContain('data-testid="exact-version-credential-bindings"');
    expect(capturedBindingsProps).toMatchObject({
      versionId: HISTORICAL_VERSION_ID,
      canManage: false,
    });
  });

  test("keeps a dirty exact-version binding editor mounted after the live pointer moves", () => {
    const html = renderDetail({
      credentialBindingsDirty: true,
      currentVersionId: CANDIDATE_VERSION_ID,
    });

    expect(html).toContain('data-testid="exact-version-credential-bindings"');
  });

  test("keeps current System Skill declarations read-only but mappings writable", () => {
    renderDetail({ scope: "system" });

    expect(capturedBindingsProps).toMatchObject({
      versionId: CURRENT_VERSION_ID,
      canManage: true,
    });
  });

  test("shows the owner-authorized design record entry on the Skill detail", () => {
    const href = `/projects/demo/skills/new/${DESIGN_SESSION_ID}`;
    const html = renderDetail({ designRecordHref: href });

    expect(html).toContain(`href="${href}"`);
    expect(html).toContain("查看设计记录");
  });

  test.each([
    ["version files are being edited", { editing: true }],
    ["Credential bindings are dirty", { credentialBindingsDirty: true }],
  ])("hides the design record entry while %s", (_label, state) => {
    const href = `/projects/demo/skills/new/${DESIGN_SESSION_ID}`;
    const html = renderDetail({ designRecordHref: href, ...state });

    expect(html).not.toContain(`href="${href}"`);
    expect(html).not.toContain("查看设计记录");
  });
});
