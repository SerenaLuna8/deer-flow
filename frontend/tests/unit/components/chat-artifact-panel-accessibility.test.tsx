import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { ArtifactPanelCloseButton } from "@/components/workspace/chats/chat-box";

describe("artifact panel controls", () => {
  test("gives the icon-only close control an accessible name", () => {
    const html = renderToStaticMarkup(
      <ArtifactPanelCloseButton
        label="Close artifacts"
        onClose={() => undefined}
      />,
    );

    expect(html).toContain('aria-label="Close artifacts"');
  });
});
