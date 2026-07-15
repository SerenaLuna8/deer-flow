import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  PrivateWorkProvider,
  usePrivateWorkAccess,
} from "@/core/private-work/provider";
import { createPrivateWorkScopeRegistry } from "@/core/private-work/scope-registry";

const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

function AccessConsumer() {
  const access = usePrivateWorkAccess();
  return createElement(
    "span",
    null,
    `${access.scope?.accountId}:${access.scope?.projectId}:${access.apiBaseURL}`,
  );
}

describe("private-work provider", () => {
  test("provides the project client and scope to nested hooks", () => {
    const registry = createPrivateWorkScopeRegistry();
    const access = registry.acquire(scope);
    const html = renderToStaticMarkup(
      <PrivateWorkProvider access={access}>
        <AccessConsumer />
      </PrivateWorkProvider>,
    );

    expect(html).toContain(scope.accountId);
    expect(html).toContain(scope.projectId);
    expect(html).toContain(`/api/projects/${scope.projectId}/private-work`);
  });

  test("threads and uploads resolve their client and scope from the provider", () => {
    const threads = readFileSync(
      resolve(process.cwd(), "src/core/threads/hooks.ts"),
      "utf8",
    );
    const uploads = readFileSync(
      resolve(process.cwd(), "src/core/uploads/hooks.ts"),
      "utf8",
    );

    expect(threads).toContain("usePrivateWorkAccess");
    expect(threads).toContain("reconnectOnMount: privateWork.reconnectOnMount");
    expect(uploads).toContain("usePrivateWorkAccess");
  });
});
