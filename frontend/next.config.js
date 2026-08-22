/**
 * Run `build` or `dev` with `SKIP_ENV_VALIDATION` to skip env validation. This is especially useful
 * for Docker builds.
 */
const buildMode = process.env.BUILD_MODE?.trim() || "production";
if (!new Set(["production", "static"]).has(buildMode)) {
  throw new Error(
    `Unsupported BUILD_MODE=${JSON.stringify(buildMode)}; expected "production" or "static".`,
  );
}
process.env.NEXT_PUBLIC_BUILD_MODE = buildMode;
await import("./src/env.js");

function getInternalServiceURL(envKey, fallbackURL) {
  const configured = process.env[envKey]?.trim();
  return configured && configured.length > 0
    ? configured.replace(/\/+$/, "")
    : fallbackURL;
}
import nextra from "nextra";

const withNextra = nextra({});

/** @type {import("next").NextConfig} */
const config = {
  experimental: {
    authInterrupts: true,
  },
  distDir: buildMode === "static" ? ".next-static" : ".next",
  env: {
    NEXT_PUBLIC_BUILD_MODE: buildMode,
  },
  output:
    process.env.NEXT_CONFIG_BUILD_OUTPUT === "standalone"
      ? "standalone"
      : undefined,
  i18n: {
    locales: ["en", "zh"],
    defaultLocale: "en",
  },
  devIndicators: false,
  async redirects() {
    return [
      {
        source: "/workspace/projects",
        destination: "/workspace",
        permanent: false,
      },
    ];
  },
  async rewrites() {
    const rewrites = [];
    const gatewayURL = getInternalServiceURL(
      "ACT_WEAVE_INTERNAL_GATEWAY_BASE_URL",
      "http://127.0.0.1:8001",
    );

    if (!process.env.NEXT_PUBLIC_BACKEND_BASE_URL) {
      // Catch-all for gateway API routes that don't have their own
      // NEXT_PUBLIC_* env var toggle.
      rewrites.push({
        source: "/api/:path*",
        destination: `${gatewayURL}/api/:path*`,
      });
    }

    return rewrites;
  },
  webpack(webpackConfig) {
    if (buildMode === "static") {
      webpackConfig.resolve.conditionNames = [
        "deerflow-static",
        ...webpackConfig.resolve.conditionNames,
      ];
    }
    return webpackConfig;
  },
};

export default withNextra(config);
