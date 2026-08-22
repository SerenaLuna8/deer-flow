import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { AdminPage } from "@/components/admin/ui/admin-page";
import { Section } from "@/components/landing/section";
import { TodoList } from "@/components/workspace/todo-list";

describe("nested content landmarks", () => {
  test("leaves the page-level main landmark to the owning route", () => {
    const html = renderToStaticMarkup(
      <>
        <Section title="Section title">Section content</Section>
        <TodoList todos={[]} collapsed />
      </>,
    );

    expect(html).not.toMatch(/<main\b/u);
  });

  test("gives the collapsible to-do list native button semantics", () => {
    const html = renderToStaticMarkup(<TodoList todos={[]} collapsed />);

    expect(html).toContain('<button type="button"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toMatch(/aria-controls="[^"]+"/u);
    expect(html).toContain('hidden=""');
  });

  test("makes the Admin skip-link target programmatically focusable", () => {
    const html = renderToStaticMarkup(<AdminPage>Admin content</AdminPage>);

    expect(html).toContain('id="admin-main"');
    expect(html).toContain('tabindex="-1"');
  });
});
