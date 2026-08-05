from __future__ import annotations

import deerflow.agents.memory.prompt as prompt_module


def test_char_token_counting_never_loads_tiktoken(monkeypatch) -> None:
    def unexpected_load(*_args, **_kwargs):
        raise AssertionError("char token counting must not load tiktoken")

    monkeypatch.setattr(prompt_module.tiktoken, "get_encoding", unexpected_load)

    assert prompt_module._count_tokens("中文 memory", use_tiktoken=False) > 0
