"use client";

import { css } from "@codemirror/lang-css";
import { html } from "@codemirror/lang-html";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { languages } from "@codemirror/language-data";
import { basicLightInit } from "@uiw/codemirror-theme-basic";
import { monokaiInit } from "@uiw/codemirror-theme-monokai";
import CodeMirror from "@uiw/react-codemirror";
import { useTheme } from "next-themes";
import { useMemo } from "react";

import { cn } from "@/lib/utils";

const customDarkTheme = monokaiInit({
  settings: {
    background: "transparent",
    gutterBackground: "transparent",
    gutterForeground: "#555",
    gutterActiveForeground: "#fff",
    fontSize: "var(--text-sm)",
  },
});

const customLightTheme = basicLightInit({
  settings: {
    background: "transparent",
    fontSize: "var(--text-sm)",
  },
});

function languageExtensions(language: string | undefined) {
  switch (language?.toLowerCase()) {
    case "html":
      return [html()];
    case "css":
      return [css()];
    case "javascript":
      return [javascript()];
    case "jsx":
      return [javascript({ jsx: true })];
    case "typescript":
      return [javascript({ typescript: true })];
    case "tsx":
      return [javascript({ jsx: true, typescript: true })];
    case "json":
      return [json()];
    case "markdown":
      return [
        markdown({
          base: markdownLanguage,
          codeLanguages: languages,
        }),
      ];
    case "python":
      return [python()];
    default:
      return [];
  }
}

export function CodeEditor({
  className,
  placeholder,
  value,
  language,
  readonly,
  disabled,
  autoFocus,
  settings,
}: {
  className?: string;
  placeholder?: string;
  value: string;
  language?: string;
  readonly?: boolean;
  disabled?: boolean;
  autoFocus?: boolean;
  settings?: unknown;
}) {
  const { resolvedTheme } = useTheme();

  const extensions = useMemo(() => languageExtensions(language), [language]);

  return (
    <div
      className={cn(
        "flex cursor-text flex-col overflow-hidden rounded-md",
        className,
      )}
    >
      <CodeMirror
        readOnly={readonly ?? disabled}
        placeholder={placeholder}
        className={cn(
          "h-full overflow-auto font-mono [&_.cm-editor]:h-full [&_.cm-focused]:outline-none!",
          "px-2 py-0! [&_.cm-line]:px-2! [&_.cm-line]:py-0!",
        )}
        theme={resolvedTheme === "dark" ? customDarkTheme : customLightTheme}
        extensions={extensions}
        basicSetup={{
          foldGutter:
            (settings as { foldGutter?: boolean })?.foldGutter ?? false,
          highlightActiveLine: false,
          highlightActiveLineGutter: false,
          lineNumbers:
            (settings as { lineNumbers?: boolean })?.lineNumbers ?? false,
        }}
        autoFocus={autoFocus}
        value={value}
      />
    </div>
  );
}
