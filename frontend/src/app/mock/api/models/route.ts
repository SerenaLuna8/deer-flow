export function GET() {
  return Response.json({
    models: [
      {
        name: "doubao-seed-1.8",
        model: "doubao-seed-1.8",
        display_name: "Doubao Seed 1.8",
        description: "",
        supports_thinking: true,
        supports_reasoning_effort: false,
        supports_vision: false,
        is_default: true,
      },
      {
        name: "deepseek-v4",
        model: "deepseek-v4",
        display_name: "DeepSeek V4",
        description: "",
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: false,
        is_default: false,
      },
      {
        name: "gpt-5",
        model: "gpt-5",
        display_name: "GPT-5",
        description: "",
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
        is_default: false,
      },
      {
        name: "gemini-3-pro",
        model: "gemini-3-pro",
        display_name: "Gemini 3 Pro",
        description: "",
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: true,
        is_default: false,
      },
    ],
    token_usage: { enabled: false },
  });
}
