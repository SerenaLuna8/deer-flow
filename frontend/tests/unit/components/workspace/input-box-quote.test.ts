import { expect, test } from "@rstest/core";

import { buildHiddenConversationQuoteMessage } from "@/components/workspace/input-box-quote";

test("builds a hidden quote message and escapes its XML label", () => {
  expect(
    buildHiddenConversationQuoteMessage({
      contexts: [
        {
          type: "referenced_message",
          label: 'Plan & review "<draft>"',
          messageId: "message-1",
          role: "user",
          content: "Quoted <body>",
        },
      ],
    }),
  ).toEqual({
    type: "human",
    content: [
      {
        type: "text",
        text: [
          "The user added the following quoted context to this conversation.",
          "Use the referenced_message blocks as reference material for the user's next message.",
          "",
          '<referenced_message index="1" label="Plan &amp; review &quot;&lt;draft&gt;&quot;">',
          "Role: User",
          "Message ID: message-1",
          "",
          "Quoted <body>",
          "</referenced_message>",
          "",
        ].join("\n"),
      },
    ],
    additional_kwargs: {
      hide_from_ui: true,
      conversation_quote_context: true,
      referenced_message_ids: ["message-1"],
      referenced_message_roles: ["user"],
      quote_context_count: 1,
    },
  });
});

test("keeps quote metadata parallel without deduplicating message ids", () => {
  const message = buildHiddenConversationQuoteMessage({
    contexts: [
      {
        type: "referenced_message",
        label: "First",
        messageId: "shared-message",
        role: "assistant",
        content: "First quote",
      },
      {
        type: "referenced_message",
        label: "Second",
        role: "user",
        content: "Second quote",
      },
      {
        type: "referenced_message",
        label: "Third",
        messageId: "shared-message",
        role: "assistant",
        content: "Third quote",
      },
    ],
  });

  expect(message.additional_kwargs).toEqual({
    hide_from_ui: true,
    conversation_quote_context: true,
    referenced_message_ids: ["shared-message", "", "shared-message"],
    referenced_message_roles: ["assistant", "user", "assistant"],
    quote_context_count: 3,
  });
  expect(message.content).toEqual([
    {
      type: "text",
      text: expect.stringContaining(
        "The user added the following 3 quoted contexts to this conversation.",
      ),
    },
  ]);
});
