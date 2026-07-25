import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

describe("next-themes React 19 compatibility", () => {
  test("renders the bootstrap script only on the server", async () => {
    const { ThemeProvider } = await import("next-themes");
    const serverHtml = renderToStaticMarkup(
      <ThemeProvider scriptProps={{ "data-theme-bootstrap": "true" }}>
        <span>content</span>
      </ThemeProvider>,
    );

    expect(serverHtml).toContain('<script data-theme-bootstrap="true"');
    expect(serverHtml).toContain("content");

    const previousWindow = Object.getOwnPropertyDescriptor(
      globalThis,
      "window",
    );
    const previousLocalStorage = Object.getOwnPropertyDescriptor(
      globalThis,
      "localStorage",
    );

    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: {
        matchMedia: () => ({ matches: false }),
      },
    });
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: () => null,
      },
    });

    try {
      const clientHtml = renderToStaticMarkup(
        <ThemeProvider scriptProps={{ "data-theme-bootstrap": "true" }}>
          <span>content</span>
        </ThemeProvider>,
      );

      expect(clientHtml).toContain("content");
      expect(clientHtml).not.toContain("<script");
    } finally {
      if (previousWindow) {
        Object.defineProperty(globalThis, "window", previousWindow);
      } else {
        delete (globalThis as { window?: Window }).window;
      }
      if (previousLocalStorage) {
        Object.defineProperty(globalThis, "localStorage", previousLocalStorage);
      } else {
        delete (globalThis as { localStorage?: Storage }).localStorage;
      }
    }
  });
});
