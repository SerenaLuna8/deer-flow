"use client";

import { AlertTriangleIcon, RefreshCwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";

export function AdminGatewayUnavailable() {
  const { t } = useI18n();
  const labels = t.adminOperations.gatewayUnavailable;

  return (
    <main
      id="admin-main"
      className="bg-muted/20 flex min-h-dvh items-center justify-center px-4 py-10 sm:px-6"
    >
      <section
        data-slot="admin-gateway-unavailable"
        role="alert"
        aria-labelledby="admin-gateway-unavailable-title"
        aria-describedby="admin-gateway-unavailable-description"
        className="border-border/80 bg-card w-full max-w-xl rounded-xl border px-6 py-8 text-center shadow-sm sm:px-10 sm:py-10"
      >
        <span className="border-destructive/20 bg-destructive/10 text-destructive mx-auto flex size-11 items-center justify-center rounded-full border">
          <AlertTriangleIcon aria-hidden className="size-5" />
        </span>
        <h1
          id="admin-gateway-unavailable-title"
          className="mt-5 text-xl font-semibold tracking-tight"
        >
          {labels.title}
        </h1>
        <p
          id="admin-gateway-unavailable-description"
          className="text-muted-foreground mx-auto mt-2 max-w-md text-sm leading-6"
        >
          {labels.description}
        </p>
        <Button
          className="mt-6"
          type="button"
          variant="outline"
          onClick={() => window.location.reload()}
        >
          <RefreshCwIcon aria-hidden className="size-4" />
          {labels.reload}
        </Button>
      </section>
    </main>
  );
}
