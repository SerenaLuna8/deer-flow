import { ArrowRightIcon, Clock3Icon } from "lucide-react";
import Link from "next/link";

import {
  skillBuilderSessionPath,
  type SkillBuilderSessionSummary,
} from "@/core/skill-builder";

export function SkillBuilderResumeBanner({
  projectSlug,
  sessions,
}: {
  projectSlug: string;
  sessions: SkillBuilderSessionSummary[];
}) {
  const unfinished = sessions.filter(
    (session) =>
      session.status !== "completed" && session.status !== "cancelled",
  );
  if (unfinished.length === 0) return null;

  return (
    <section
      aria-labelledby="skill-builder-resume-title"
      className="border-border/70 bg-muted/20 mb-5 rounded-2xl border p-4 sm:p-5"
    >
      <div className="mb-3 flex items-center gap-2">
        <Clock3Icon aria-hidden className="text-muted-foreground size-4" />
        <h2 id="skill-builder-resume-title" className="text-sm font-semibold">
          继续创建未完成的 Skill
        </h2>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {unfinished.map((session) => (
          <Link
            key={session.id}
            href={skillBuilderSessionPath(projectSlug, session.id)}
            className="bg-background hover:border-foreground/20 focus-visible:ring-ring flex min-h-12 items-center gap-3 rounded-xl border px-4 py-3 transition-colors outline-none focus-visible:ring-2"
          >
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">
                {session.display_name}
              </span>
              <span className="text-muted-foreground mt-0.5 block text-xs">
                上次更新{" "}
                {new Intl.DateTimeFormat("zh-CN", {
                  month: "numeric",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                }).format(new Date(session.updated_at))}
              </span>
            </span>
            <ArrowRightIcon aria-hidden className="size-4 shrink-0" />
          </Link>
        ))}
      </div>
    </section>
  );
}
