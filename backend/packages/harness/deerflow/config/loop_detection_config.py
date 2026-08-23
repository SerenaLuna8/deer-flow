"""Legacy feature switch for unified Harness tool-call control."""

from pydantic import BaseModel, ConfigDict, Field


class LoopDetectionConfig(BaseModel):
    """Compatibility switch mapped to the unified ``ToolCallControl`` module."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description=("Compatibility switch for unified repeated-call and tool-budget control. Limits come from the resolved Runtime Policy profile."),
    )
