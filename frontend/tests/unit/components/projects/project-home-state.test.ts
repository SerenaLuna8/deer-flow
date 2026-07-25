import { describe, expect, test } from "@rstest/core";

import {
  commitProjectHomeAttempt,
  createProjectHomeAttemptCoordinator,
  projectHomeIdentityKey,
  projectResultForIdentity,
} from "@/components/projects/project-home-state";
import type { Project } from "@/core/projects/types";

const oldProject = {
  id: "11111111-1111-4111-8111-111111111111",
  role: "admin",
} as Project;

describe("project home identity state", () => {
  test("binds enter results to account, slug, and UUID", () => {
    const oldIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      oldProject.id,
      1,
    )!;
    const newAccountIdentity = projectHomeIdentityKey(
      "u2",
      "alpha",
      oldProject.id,
      1,
    )!;
    const newSlugIdentity = projectHomeIdentityKey(
      "u1",
      "beta",
      oldProject.id,
      1,
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

  test("starts fresh attempts for real account, slug, and UUID changes while rejecting stale results", () => {
    const attempts = createProjectHomeAttemptCoordinator();
    const identities = [
      projectHomeIdentityKey("u1", "alpha", oldProject.id, 1)!,
      projectHomeIdentityKey("u2", "alpha", oldProject.id, 1)!,
      projectHomeIdentityKey("u2", "beta", oldProject.id, 1)!,
      projectHomeIdentityKey(
        "u2",
        "beta",
        "22222222-2222-4222-8222-222222222222",
        1,
      )!,
      projectHomeIdentityKey(
        "u2",
        "beta",
        "22222222-2222-4222-8222-222222222222",
        2,
      )!,
    ];

    const tokens = identities.map((identity) => {
      attempts.activate(identity);
      return attempts.start(identity)!;
    });

    for (const staleToken of tokens.slice(0, -1)) {
      expect(attempts.complete(staleToken)).toBe(false);
    }
    expect(attempts.complete(tokens.at(-1)!)).toBe(true);
    expect(
      projectResultForIdentity(identities.at(-1)!, {
        identity: identities[0]!,
        project: oldProject,
      }),
    ).toBeNull();
  });

  test("treats membership version as identity but ignores response request IDs", () => {
    const versionOne = projectHomeIdentityKey("u1", "alpha", oldProject.id, 1);
    const sameVersionAfterRefetch = projectHomeIdentityKey(
      "u1",
      "alpha",
      oldProject.id,
      1,
    );
    const versionTwo = projectHomeIdentityKey("u1", "alpha", oldProject.id, 2);

    expect(sameVersionAfterRefetch).toBe(versionOne);
    expect(versionTwo).not.toBe(versionOne);
  });

  test("restarts an enter attempt after Strict Effects cleanup", () => {
    const identity = projectHomeIdentityKey("u1", "alpha", oldProject.id, 1)!;
    const attempts = createProjectHomeAttemptCoordinator();

    const first = attempts.start(identity);
    expect(first).not.toBeNull();
    attempts.dispose(first!);

    const second = attempts.start(identity);
    expect(second).not.toBeNull();
    expect(second).not.toEqual(first);
    expect(attempts.fail(first!)).toBe(false);
    expect(attempts.complete(first!)).toBe(false);
    expect(attempts.complete(second!)).toBe(true);
    expect(attempts.start(identity)).toBeNull();
    attempts.activate("other-account-and-slug");
    expect(attempts.start(identity)).not.toBeNull();
  });

  test("commits the fresh project returned by enter instead of the lookup snapshot", () => {
    const identity = projectHomeIdentityKey("u1", "alpha", oldProject.id, 1)!;
    const attempts = createProjectHomeAttemptCoordinator();
    const token = attempts.start(identity)!;
    const enteredProject = {
      ...oldProject,
      role: "editor",
      last_entered_at: "2026-07-12T12:34:56Z",
    } as Project;

    const enteredResult = commitProjectHomeAttempt(
      attempts,
      token,
      identity,
      identity,
      enteredProject,
    );
    const renderedProject = projectResultForIdentity(identity, enteredResult);

    expect(renderedProject).toBe(enteredProject);
    expect(renderedProject).not.toBe(oldProject);
  });

  test("changes identity when the project summary changes so enter-project refreshes the whole snapshot", () => {
    const firstLookup = {
      ...oldProject,
      member_count: 1,
      agent_count: 0,
      skill_count: 0,
      mcp_count: 0,
    } as Project;
    const firstIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      firstLookup.id,
      1,
      firstLookup,
    );
    const refreshedIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      firstLookup.id,
      1,
      { ...firstLookup, agent_count: 1 },
    );

    expect(refreshedIdentity).not.toBe(firstIdentity);
    expect(
      projectResultForIdentity(refreshedIdentity, {
        identity: firstIdentity!,
        project: { ...firstLookup, agent_count: 1 },
      }),
    ).toBeNull();
  });

  test("changes identity when editable project metadata changes", () => {
    const firstLookup = {
      ...oldProject,
      display_name: "Alpha",
      description: "First description",
      icon: "folder",
      member_count: 1,
      agent_count: 0,
      skill_count: 0,
      mcp_count: 0,
    } as Project;
    const firstIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      firstLookup.id,
      1,
      firstLookup,
    );
    const renamedIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      firstLookup.id,
      1,
      { ...firstLookup, display_name: "Alpha Renamed" },
    );
    const describedIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      firstLookup.id,
      1,
      { ...firstLookup, description: "Updated description" },
    );
    const reiconedIdentity = projectHomeIdentityKey(
      "u1",
      "alpha",
      firstLookup.id,
      1,
      { ...firstLookup, icon: "sparkles" },
    );

    expect(renamedIdentity).not.toBe(firstIdentity);
    expect(describedIdentity).not.toBe(firstIdentity);
    expect(reiconedIdentity).not.toBe(firstIdentity);
  });
});
