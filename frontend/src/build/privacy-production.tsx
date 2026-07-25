"use client";

import { PrivacyCenterPage as PrivacyCenterClientPage } from "@/components/projects/privacy-center-page";
import { GatewayOfflineBanner } from "@/components/workspace/gateway-offline-banner";
import { useAuth } from "@/core/auth/AuthProvider";

export function PrivacyPage() {
  const { user } = useAuth();
  if (!user) return <GatewayOfflineBanner gatewayUnavailable />;
  return <PrivacyCenterClientPage key={user.id} accountId={user.id} />;
}
