const SUPPORTED_DOC_LANGUAGES = new Set(["en", "en-US", "zh", "zh-CN"]);
const UNLOCALIZED_DOCS_PATH = /^(?:\.|)\/docs(?=\/|[?#]|$)/;

export function localizeDocsHref(
  href: string,
  lang: string | undefined,
): string {
  if (!lang || !SUPPORTED_DOC_LANGUAGES.has(lang)) {
    return href;
  }
  if (!UNLOCALIZED_DOCS_PATH.test(href)) {
    return href;
  }
  const absoluteDocsHref = href.startsWith("./") ? href.slice(1) : href;
  return `/${lang}${absoluteDocsHref}`;
}
