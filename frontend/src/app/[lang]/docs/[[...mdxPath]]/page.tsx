import { notFound } from "next/navigation";
import { generateStaticParamsFor, importPage } from "nextra/pages";

import { resolveDocsLanguage } from "@/core/docs/routing";

import { useMDXComponents as getMDXComponents } from "../../../../mdx-components";

export const generateStaticParams = generateStaticParamsFor("mdxPath");

export async function generateMetadata(props) {
  const params = await props.params;
  const docsLanguage = resolveDocsLanguage(params.lang);
  if (!docsLanguage) {
    notFound();
  }
  const { metadata } = await importPage(
    params.mdxPath,
    docsLanguage.contentLang,
  );
  return metadata;
}

// eslint-disable-next-line @typescript-eslint/unbound-method
const Wrapper = getMDXComponents().wrapper;

export default async function Page(props) {
  const params = await props.params;
  const docsLanguage = resolveDocsLanguage(params.lang);
  if (!docsLanguage) {
    notFound();
  }
  const {
    default: MDXContent,
    toc,
    metadata,
    sourceCode,
  } = await importPage(params.mdxPath, docsLanguage.contentLang);
  return (
    <Wrapper toc={toc} metadata={metadata} sourceCode={sourceCode}>
      <MDXContent {...props} params={params} />
    </Wrapper>
  );
}
