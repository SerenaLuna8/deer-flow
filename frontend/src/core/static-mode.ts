import { env } from "@/env";

export function isStaticWebsiteOnly() {
  return env.NEXT_PUBLIC_BUILD_MODE === "static";
}
