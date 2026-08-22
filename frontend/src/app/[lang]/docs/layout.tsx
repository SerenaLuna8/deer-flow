import { notFound } from "next/navigation";
import { getPageMap } from "nextra/page-map";
import { Layout } from "nextra-theme-docs";

import { Footer } from "@/components/landing/footer";
import { Header } from "@/components/landing/header";
import { buildDocsPageMap, resolveDocsLanguage } from "@/core/docs/routing";
import "nextra-theme-docs/style.css";

export default async function DocLayout({ children, params }) {
  const { lang } = await params;
  const docsLanguage = resolveDocsLanguage(lang);
  if (!docsLanguage) {
    notFound();
  }

  const pages = await getPageMap(`/${docsLanguage.contentLang}`);
  const pageMap = buildDocsPageMap(`/${lang}/docs`, pages);

  return (
    <Layout
      navbar={
        <Header
          className="sticky max-w-full px-10"
          homeURL="/"
          locale={docsLanguage.locale}
        />
      }
      pageMap={pageMap}
      footer={<Footer className="mt-0" />}
      i18n={docsLanguage.localeOptions}
    >
      {children}
    </Layout>
  );
}
