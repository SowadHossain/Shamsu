from __future__ import annotations

from shamsu.safety.autonomy import is_long_running_enabled, set_long_running_enabled


def test_long_running_defaults_to_disabled(tmp_path):
    assert is_long_running_enabled(tmp_path) is False


def test_set_long_running_enabled_persists_across_calls(tmp_path):
    set_long_running_enabled(tmp_path, True)

    assert is_long_running_enabled(tmp_path) is True


def test_set_long_running_enabled_can_be_turned_back_off(tmp_path):
    set_long_running_enabled(tmp_path, True)
    set_long_running_enabled(tmp_path, False)

    assert is_long_running_enabled(tmp_path) is False


def test_is_long_running_enabled_survives_corrupt_config_file(tmp_path):
    config_dir = tmp_path / ".shamsu"
    config_dir.mkdir()
    (config_dir / "autonomy.json").write_text("not valid json{{{", encoding="utf-8")

    assert is_long_running_enabled(tmp_path) is False
