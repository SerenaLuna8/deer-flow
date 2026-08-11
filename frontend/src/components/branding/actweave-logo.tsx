import Image from "next/image";

import { cn } from "@/lib/utils";

export function ActWeaveLogo({
  className,
  priority = false,
}: {
  className?: string;
  priority?: boolean;
}) {
  return (
    <Image
      src="/images/actweave-logo-concept-v1.png"
      alt=""
      aria-hidden="true"
      width={80}
      height={80}
      priority={priority}
      sizes="(min-width: 640px) 7rem, 5rem"
      className={cn(
        "size-20 rounded-2xl bg-[#f8f5ef] object-cover shadow-sm ring-1 ring-black/10 dark:ring-white/15",
        className,
      )}
    />
  );
}
