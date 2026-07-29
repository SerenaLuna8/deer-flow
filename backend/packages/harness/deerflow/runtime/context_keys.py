"""Internal runtime-context keys shared by Worker and Agent middleware."""

from typing import Final

CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: Final[str] = "__deerflow_pre_run_message_ids"
