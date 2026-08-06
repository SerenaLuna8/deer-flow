"""Step 1: explain database-backed model setup."""

from __future__ import annotations

from dataclasses import dataclass

from wizard.ui import (
    print_header,
    print_info,
)

MODEL_ADMIN_PATH = "/admin/settings/models"


@dataclass(frozen=True)
class LLMStepResult:
    admin_path: str = MODEL_ADMIN_PATH


def run_llm_step(step_label: str = "Step 1/3") -> LLMStepResult:
    print_header(f"{step_label} · Model configuration")
    print_info("Model definitions and provider Credentials are stored in PostgreSQL, not config.yaml or .env.")
    print_info(f"After the services start, sign in as a system administrator and open {MODEL_ADMIN_PATH} to configure and activate a model.")
    return LLMStepResult()
