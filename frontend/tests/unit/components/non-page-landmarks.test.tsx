import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { TodoList } from "@/components/workspace/todo-list";

describe("nested content landmarks", () => {
  test("leaves the page-level main landmark to the owning route", () => {
    const html = renderToStaticMarkup(<TodoList todos={[]} collapsed />);

    expect(html).not.toMatch(/<main\b/u);
  });

  test("gives the collapsible to-do list native button semantics", () => {
    const html = renderToStaticMarkup(<TodoList todos={[]} collapsed />);

    expect(html).toContain('<button type="button"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toMatch(/aria-controls="[^"]+"/u);
    expect(html).toContain('hidden=""');
  });
});
