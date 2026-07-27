from __future__ import annotations

import pytest

from app.scheduler import app as scheduler_app
from app.worker import app as worker_app


@pytest.mark.parametrize(
    ("target", "expected_message"),
    (
        (worker_app, "Worker configuration is unavailable"),
        (scheduler_app, "Scheduler configuration is unavailable"),
    ),
)
def test_process_main_redacts_config_validation_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target,
    expected_message: str,
) -> None:
    secret = "proxy-user:proxy-password"

    def broken_get_app_config():
        raise ValueError(
            f"input_value=http://{secret}@proxy.example.test",
        )

    monkeypatch.setattr(target, "get_app_config", broken_get_app_config)

    with pytest.raises(SystemExit) as raised:
        target.main()

    output = capsys.readouterr()
    rendered = f"{output.out}\n{output.err}"
    assert raised.value.code == 1
    assert expected_message in output.err
    assert secret not in rendered
    assert "input_value" not in rendered
