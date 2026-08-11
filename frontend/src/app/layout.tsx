import "katex/dist/katex.min.css";
import "@/styles/globals.css";

import { type Metadata } from "next";

import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import { I18nProvider } from "@/core/i18n/context";
import { detectLocaleServer } from "@/core/i18n/server";

export const metadata: Metadata = {
  title: "ActWeave — Weave intelligence into action.",
  description:
    "Weave intelligence into action. An open-source super-agent execution platform.",
  icons: {
    icon: [
      {
        url: "/images/actweave-logo-concept-v1.png",
        type: "image/png",
      },
    ],
    apple: [
      {
        url: "/images/actweave-logo-concept-v1.png",
        type: "image/png",
      },
    ],
  },
};

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const locale = await detectLocaleServer();
  return (
    <html lang={locale} suppressContentEditableWarning suppressHydrationWarning>
      <body>
        <ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
          <I18nProvider initialLocale={locale}>{children}</I18nProvider>
          <Toaster />
        </ThemeProvider>
      </body>
    </html>
  );
}
