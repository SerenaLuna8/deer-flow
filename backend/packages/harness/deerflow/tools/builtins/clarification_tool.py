from typing import Literal, Required, TypedDict

from langchain.tools import tool


class ClarificationFormField(TypedDict, total=False):
    """One model-provided field in a bounded structured clarification form."""

    name: Required[str]
    label: str
    type: Literal[
        "text",
        "textarea",
        "number",
        "select",
        "multi_select",
        "checkbox",
        "date",
    ]
    required: bool
    options: list[str]
    placeholder: str


@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
    fields: list[ClarificationFormField] | None = None,
) -> str:
    """Ask the user for clarification when you need more information to proceed.

    Use this tool when you encounter situations where you cannot proceed without user input:

    - **Missing information**: Required details not provided (e.g., file paths, URLs, specific requirements)
    - **Ambiguous requirements**: Multiple valid interpretations exist
    - **Approach choices**: Several valid approaches exist and you need user preference
    - **Risky operations**: Destructive actions that need explicit confirmation (e.g., deleting files, modifying production)
    - **Suggestions**: You have a recommendation but want user approval before proceeding

    The execution will be interrupted and the question will be presented to the user.
    Wait for the user's response before continuing.

    When to use ask_clarification:
    - You need information that wasn't provided in the user's request
    - The requirement can be interpreted in multiple ways
    - Multiple valid implementation approaches exist
    - You're about to perform a potentially dangerous operation
    - You have a recommendation but need user approval

    Choosing the interaction shape:
    - One open question -> just `question`
    - Pick one option -> `options`
    - Collect several related values -> `fields`

    Best practices:
    - Ask ONE clarification at a time for clarity; one form still counts as one clarification
    - Be specific and clear in your question
    - Don't make assumptions when clarification is needed
    - For risky operations, ALWAYS ask for confirmation
    - After calling this tool, execution will be interrupted automatically

    Args:
        question: The clarification question to ask the user. Be specific and clear.
        clarification_type: The type of clarification needed (missing_info, ambiguous_requirement, approach_choice, risk_confirmation, suggestion).
        context: Optional context explaining why clarification is needed. Helps the user understand the situation.
        options: Optional list of choices (for approach_choice or suggestion types). Present clear options for the user to choose from.
        fields: Optional form fields for collecting multiple related values in one card. Fields take precedence over options.
            Each field requires a unique `name`; supported types are text, textarea, number, select, multi_select,
            checkbox, and date. Select fields require options. Keep forms to at most 16 fields, 24 options per
            field, and 200 characters per field name, label, option, or placeholder.
    """
    # This is a placeholder implementation
    # The actual logic is handled by ClarificationMiddleware which intercepts this tool call
    # and interrupts execution to present the question to the user
    return "Clarification request processed by middleware"
