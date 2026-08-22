import { describe, expect, rs, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/shared-assets", () => ({
  useClearProjectSkillSecret: () => ({ mutateAsync: rs.fn() }),
  useProjectSkillSecrets: () => ({
    isLoading: false,
    data: {
      requirements: [
        {
          name: "TEXT_ROUTE_DB_HOST",
          target_env: "TEXT_ROUTE_DB_HOST",
          optional: false,
          configured: false,
          revision: 0,
        },
      ],
    },
    refetch: rs.fn(),
  }),
  useReplaceProjectSkillSecrets: () => ({ execute: rs.fn() }),
}));

import { SkillSecretConfiguration } from "@/components/projects/assets/skill-secret-configuration";

describe("Skill runtime-secret layout", () => {
  test("keeps each declared secret in one desktop row", () => {
    const html = renderToStaticMarkup(
      <SkillSecretConfiguration
        accountId="11111111-1111-4111-8111-111111111111"
        projectId="22222222-2222-4222-8222-222222222222"
        skillId="33333333-3333-4333-8333-333333333333"
        versionId="44444444-4444-4444-8444-444444444444"
        canReplace
        canClear
        onDirtyChange={() => undefined}
      />,
    );

    expect(html).toContain(
      "md:grid-cols-[minmax(14rem,0.8fr)_minmax(0,1fr)_auto]",
    );
    expect(html).toContain("TEXT_ROUTE_DB_HOST");
    expect(html).toContain('aria-label="TEXT_ROUTE_DB_HOST 秘密值"');
    expect(html).toContain('aria-label="显示 TEXT_ROUTE_DB_HOST 秘密值"');
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain("清除");
  });
});
