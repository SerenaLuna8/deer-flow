import { CalendarClock } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export function ThreadScheduledTasksLink({
  href,
  label,
}: {
  href: string;
  label: string;
}) {
  return (
    <Button variant="outline" size="sm" asChild>
      <Link aria-label={label} href={href}>
        <CalendarClock aria-hidden />
        <span className="hidden sm:inline">{label}</span>
      </Link>
    </Button>
  );
}
