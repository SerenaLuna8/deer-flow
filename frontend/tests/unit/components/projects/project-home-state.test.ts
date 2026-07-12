import { describe, expect, test } from "@rstest/core";
import { MutationObserver, QueryClient } from "@tanstack/react-query";

import {
  projectHomeIdentityKey,
  projectResultForIdentity,
} from "@/components/projects/project-home-state";
import type { Project } from "@/core/projects/types";

const oldProject = {
  id: "11111111-1111-4111-8111-111111111111",
  role: "admin",
} as Project;

describe("project home identity state", () => {
  test("query cache clearing does not clear an attached mutation observer", async () => {
    const client = new QueryClient();
    const observer = new MutationObserver(client, {
      mutationFn: () => Promise.resolve(oldProject),
    });
    await observer.mutate();
    client.clear();
    expect(observer.getCurrentResult().data).toBe(oldProject);
  });

  test("binds enter results to account, slug, and UUID", () => {
    const oldIdentity = projectHomeIdentityKey("u1", "alpha", oldProject.id)!;
    const newAccountIdentity = projectHomeIdentityKey(
      "u2",
      "alpha",
      oldProject.id,
    )!;
    const newSlugIdentity = projectHomeIdentityKey(
      "u1",
      "beta",
      oldProject.id,
    )!;
    expect(
      projectResultForIdentity(newAccountIdentity, {
        identity: oldIdentity,
        project: oldProject,
      }),
    ).toBeNull();
    expect(
      projectResultForIdentity(newSlugIdentity, {
        identity: oldIdentity,
        project: oldProject,
      }),
    ).toBeNull();
    expect(
      projectResultForIdentity(oldIdentity, {
        identity: oldIdentity,
        project: oldProject,
      }),
    ).toBe(oldProject);
  });
});
