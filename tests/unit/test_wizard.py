from efb_telegram_master.wizard import DataModel


def test_data_model_initializes_request_state_per_instance(tmp_path, monkeypatch):
    monkeypatch.setattr("efb_telegram_master.wizard.get_config_path", lambda channel_id: tmp_path / f"{channel_id}.yaml")

    first = DataModel("first", "one")
    second = DataModel("second", "two")
    first.request = object()
    first.building_default = False

    assert first.__dict__["request"] is not second.__dict__["request"]
    assert second.building_default is True
