"""Settings live as JSON next to the app, not inside the meeting store."""

import json

from app.storage import SettingsStore


def test_defaults_when_no_file_exists(tmp_path):
    settings = SettingsStore(tmp_path / "settings.json").load()
    assert settings.stt_model == "small.en"
    assert settings.storage_dir == ""
    assert settings.identify_speakers is False


def test_saving_writes_readable_json(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save({"retention_days": 30, "identify_speakers": True, "storage_dir": "/data/meetings"})

    stored = json.loads(path.read_text())
    assert stored["retention_days"] == 30
    assert stored["identify_speakers"] is True
    assert store.load().storage_dir == "/data/meetings"


def test_values_are_coerced_to_their_declared_types(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    settings = store.save({"retention_days": "90", "speaker_similarity": "0.7", "store_audio": "true"})
    assert settings.retention_days == 90
    assert settings.speaker_similarity == 0.7
    assert settings.store_audio is True


def test_unknown_keys_are_ignored(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save({"definitely_not_a_setting": "x"})
    assert "definitely_not_a_setting" not in json.loads((tmp_path / "settings.json").read_text())


def test_a_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    assert SettingsStore(path).load().stt_model == "small.en"


def test_reset_restores_defaults(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save({"retention_days": 90})
    assert store.reset().retention_days == 0
