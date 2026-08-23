import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme: "light" }),
}));

rs.mock("@/components/workspace/messages/context", () => ({
  useThread: () => ({ thread: { isLoading: true, messages: [] } }),
}));

rs.mock("@/components/workspace/artifacts/context", () => ({
  useArtifacts: () => ({
    artifacts: [],
    select: () => undefined,
    setOpen: () => undefined,
    showList: () => undefined,
  }),
}));

rs.mock("@/core/artifacts/hooks", () => ({
  useArtifactContent: () => ({
    content: "const answer: number = 42;",
    error: null,
    isLoading: false,
    url: undefined,
  }),
}));

rs.mock("@/core/private-work/provider", () => ({
  useProjectPrivateWorkScope: () => ({}),
}));

rs.mock("@/core/uploads", () => ({
  useUploadedFiles: () => ({ data: { files: [] } }),
}));

rs.mock("@uiw/react-codemirror", () => ({
  default: ({
    extensions,
    readOnly,
    value,
  }: {
    extensions?: { language?: { name?: string } }[];
    readOnly?: boolean;
    value?: string;
  }) => (
    <div
      data-testid="codemirror"
      data-extension-count={extensions?.length ?? 0}
      data-language={extensions?.[0]?.language?.name ?? "text"}
      data-readonly={readOnly ? "true" : "false"}
    >
      {value}
    </div>
  ),
}));

import { ArtifactFileDetail } from "@/components/workspace/artifacts/artifact-file-detail";
import { CodeEditor } from "@/components/workspace/code-editor";
import { I18nProvider } from "@/core/i18n/context";

function renderEditor(language: string) {
  return renderToStaticMarkup(
    <CodeEditor language={language} readonly value="const answer = 42;" />,
  );
}

describe("CodeEditor language highlighting", () => {
  test("keeps CodeMirror mounted while the Run is loading", () => {
    const html = renderEditor("html");

    expect(html).toContain('data-testid="codemirror"');
    expect(html).toContain('data-readonly="true"');
    expect(html).not.toContain("<textarea");
  });

  test.each([
    ["html", "html"],
    ["css", "css"],
    ["javascript", "javascript"],
    ["jsx", "javascript"],
    ["typescript", "typescript"],
    ["tsx", "typescript"],
    ["json", "json"],
    ["markdown", "markdown"],
    ["python", "python"],
  ])("activates only the %s parser", (language, parser) => {
    const html = renderEditor(language);

    expect(html).toContain('data-extension-count="1"');
    expect(html).toContain(`data-language="${parser}"`);
  });

  test("falls back to plain text for unsupported languages", () => {
    const html = renderEditor("ruby");

    expect(html).toContain('data-extension-count="0"');
    expect(html).toContain('data-language="text"');
  });

  test("uses the artifact file language while a Run is writing the file", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="en-US">
        <ArtifactFileDetail
          filepath="write-file:///outputs/example.ts?tool_call_id=write-1"
          threadId="thread-1"
        />
      </I18nProvider>,
    );

    expect(html).toContain('data-testid="codemirror"');
    expect(html).toContain('data-extension-count="1"');
    expect(html).toContain('data-language="typescript"');
    expect(html).toContain('data-readonly="true"');
  });
});
