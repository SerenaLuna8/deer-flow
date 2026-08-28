import Link from "next/link";

import type { Locale } from "@/core/i18n/locale";
import { getI18n } from "@/core/i18n/server";
import { cn } from "@/lib/utils";

import { MobileNav } from "./mobile-nav";

export type HeaderProps = {
  className?: string;
  homeURL?: string;
  locale?: Locale;
};

export async function Header({ className, homeURL, locale }: HeaderProps) {
  const { locale: resolvedLocale, t } = await getI18n(locale);
  const lang = resolvedLocale.substring(0, 2);
  const links = [{ href: `/${lang}/docs`, label: t.home.docs }];
  return (
    <header
      className={cn(
        "container-md fixed top-0 right-0 left-0 z-20 mx-auto flex h-16 items-center justify-between gap-3 px-4 backdrop-blur-xs",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-6">
        <Link
          href={homeURL ?? "/"}
          className="font-serif text-xl whitespace-nowrap"
        >
          Fluva
        </Link>
      </div>
      <nav className="ml-auto hidden items-center gap-5 text-sm font-medium sm:flex md:mr-8 md:gap-8">
        {links.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className="text-secondary-foreground hover:text-foreground transition-colors"
          >
            {link.label}
          </Link>
        ))}
      </nav>
      <MobileNav links={links} />
      <hr className="from-border/0 via-border/70 to-border/0 absolute top-16 right-0 left-0 z-10 m-0 h-px w-full border-none bg-linear-to-r" />
    </header>
  );
}
