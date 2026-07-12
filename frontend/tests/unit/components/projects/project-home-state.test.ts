import { describe, expect, test } from "@rstest/core";
import { MutationObserver, QueryClient } from "@tanstack/react-query";

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

  test("restarts an enter attempt after Strict Effects cleanup", () => {
    const identity = projectHomeIdentityKey("u1", "alpha", oldProject.id)!;
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
    const identity = projectHomeIdentityKey("u1", "alpha", oldProject.id)!;
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
});
