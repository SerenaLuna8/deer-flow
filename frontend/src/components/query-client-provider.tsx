"use client";

import { QueryClientProvider as TanStackQueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { createAccountQueryClient } from "@/core/auth/account-query-client";

export function QueryClientProvider({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const [queryClient] = useState(createAccountQueryClient);
  return (
    <TanStackQueryClientProvider client={queryClient}>
      {children}
    </TanStackQueryClientProvider>
  );
}
