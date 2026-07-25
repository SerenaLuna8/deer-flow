import { cookies } from "next/headers";
import { forbidden, notFound } from "next/navigation";

import { getGatewayConfig } from "@/core/auth/gateway-config";

import { projectPageSchema, type Capability, type Project } from "./types";

const PROJECT_CAPABILITY_TIMEOUT_MS = 5_000;

type ServerProjectLookup =
  | { status: "ready"; project: Project }
  | { status: "not_found" }
  | { status: "unavailable" };

export async function lookupServerProjectBySlug(
  slug: string,
): Promise<ServerProjectLookup> {
  const normalizedSlug = slug.trim();
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(normalizedSlug)) {
    return { status: "not_found" };
  }

  let gatewayUrl: string;
  try {
    gatewayUrl = getGatewayConfig().internalGatewayUrl;
  } catch {
    return { status: "unavailable" };
  }

  const cookieStore = await cookies();
  const sessionCookie = cookieStore.get("access_token");
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    PROJECT_CAPABILITY_TIMEOUT_MS,
  );
  try {
    const params = new URLSearchParams({
      query: normalizedSlug,
      limit: "100",
    });
    const response = await fetch(`${gatewayUrl}/api/projects?${params}`, {
      headers: sessionCookie
        ? { Cookie: `access_token=${sessionCookie.value}` }
        : undefined,
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401 || response.status === 403) {
      return { status: "not_found" };
    }
    if (!response.ok) return { status: "unavailable" };
    const parsed = projectPageSchema.safeParse(await response.json());
    if (!parsed.success) return { status: "unavailable" };
    const project = parsed.data.items.find(
      (item) => item.slug === normalizedSlug,
    );
    return project ? { status: "ready", project } : { status: "not_found" };
  } catch {
    return { status: "unavailable" };
  } finally {
    clearTimeout(timeout);
  }
}

export async function requireServerProjectCapability(
  slug: string,
  capabilities: Capability | readonly Capability[],
  options: { match?: "any" | "all" } = {},
): Promise<void> {
  const lookup = await lookupServerProjectBySlug(slug);
  if (lookup.status === "unavailable") return;
  if (lookup.status === "not_found") notFound();

  const required = Array.isArray(capabilities) ? capabilities : [capabilities];
  const allowed =
    options.match === "all"
      ? required.every((capability) =>
          lookup.project.capabilities.includes(capability),
        )
      : required.some((capability) =>
          lookup.project.capabilities.includes(capability),
        );
  if (!allowed) forbidden();
}
