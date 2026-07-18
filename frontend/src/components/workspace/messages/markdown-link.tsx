import type { AnchorHTMLAttributes } from "react";

import { useProjectArtifactReferenceURL } from "@/core/private-work/file-hooks";
import { cn } from "@/lib/utils";

import { CitationLink } from "../citations/citation-link";

function isExternalUrl(href: string | undefined): boolean {
  return !!href && /^https?:\/\//.test(href);
}

/**
 * Builds the `a` renderer shared by message content and generic markdown.
 * Passing a `threadId` also resolves `/mnt/` artifact links; without it those
 * links fall through to the default external-link handling.
 */
export function createMarkdownLinkComponent(threadId?: string) {
  if (threadId) {
    return function ProjectMarkdownLink({
      href,
      ...props
    }: AnchorHTMLAttributes<HTMLAnchorElement>) {
      const projectURL = useProjectArtifactReferenceURL(
        threadId,
        href?.startsWith("/mnt/") ? href : "",
      );
      if (href?.startsWith("/mnt/")) {
        return projectURL ? (
          <a
            {...props}
            href={projectURL}
            target="_blank"
            rel="noopener noreferrer"
          />
        ) : (
          <span>{props.children}</span>
        );
      }
      return <DefaultMarkdownLink href={href} {...props} />;
    };
  }
  return DefaultMarkdownLink;
}

function DefaultMarkdownLink({
  href,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (typeof props.children === "string") {
    const match = /^citation:(.+)$/.exec(props.children);
    if (match) {
      const [, text] = match;
      return (
        <CitationLink {...props} href={href}>
          {text}
        </CitationLink>
      );
    }
  }
  const { className, target, rel, ...rest } = props;
  const external = isExternalUrl(href);
  return (
    <a
      {...rest}
      href={href}
      className={cn(
        "text-primary decoration-primary/30 hover:decoration-primary/60 underline underline-offset-2 transition-colors",
        className,
      )}
      target={target ?? (external ? "_blank" : undefined)}
      rel={rel ?? (external ? "noopener noreferrer" : undefined)}
    />
  );
}
