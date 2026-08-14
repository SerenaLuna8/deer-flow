import type { Message } from "@langchain/langgraph-sdk";

import type { SidecarContext } from "@/core/sidecar";

function escapeXmlAttribute(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function buildHiddenConversationQuoteMessage({
  contexts,
}: {
  contexts: SidecarContext[];
}): Message {
  return {
    type: "human",
    content: [
      {
        type: "text",
        text: [
          contexts.length === 1
            ? "The user added the following quoted context to this conversation."
            : `The user added the following ${contexts.length} quoted contexts to this conversation.`,
          "Use the referenced_message blocks as reference material for the user's next message.",
          "",
          ...contexts.flatMap((context, index) =>
            [
              `<referenced_message index="${index + 1}" label="${escapeXmlAttribute(
                context.label,
              )}">`,
              `Role: ${context.role === "user" ? "User" : "Assistant"}`,
              context.messageId ? `Message ID: ${context.messageId}` : null,
              "",
              context.content,
              "</referenced_message>",
              "",
            ].filter((line): line is string => line !== null),
          ),
        ]
          .filter((line): line is string => line !== null)
          .join("\n"),
      },
    ],
    additional_kwargs: {
      hide_from_ui: true,
      conversation_quote_context: true,
      // Keep ids/roles/count 1:1 parallel with `contexts` so consumers can zip
      // them safely; do not dedupe ids here.
      referenced_message_ids: contexts.map(
        (context) => context.messageId ?? "",
      ),
      referenced_message_roles: contexts.map((context) => context.role),
      quote_context_count: contexts.length,
    },
  } as Message;
}
