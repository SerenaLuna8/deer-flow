"use client";

import { useMemo } from "react";

import {
  ScopedChatPage,
  workspaceChatRouteScope,
} from "@/components/workspace/chats/scoped-chat-page";
import { usePrivateWorkAccess } from "@/core/private-work/provider";

export default function ChatPage() {
  const privateWork = usePrivateWorkAccess();
  const scope = useMemo(
    () => workspaceChatRouteScope(privateWork.client),
    [privateWork.client],
  );
  return <ScopedChatPage scope={scope} />;
}
