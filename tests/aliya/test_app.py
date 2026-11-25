import pytest

from agent.aliya.app import main


def test_missing_config_exits_nonzero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALIYA_CONTROL_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        main(config_path=tmp_path / "missing.yaml")
    assert exc.value.code != 0
