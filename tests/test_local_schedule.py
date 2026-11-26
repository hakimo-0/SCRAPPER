import json
import os
from types import SimpleNamespace

import pytest

from scraper import crm, local_schedule
from scraper.scheduled_search import run_scheduled_search


def _result():
    return SimpleNamespace(
        status="complete",
        listings=[{
            "reference_number": "AA-1",
            "source": "Jobbörse",
            "name": "Beispiel GmbH",
            "title": "Ausbildung",
            "city": "Bonn",
            "url": "https://arbeitsagentur.example/job/AA-1",
        }],
        reported_total=1,
        retrieved_count=1,
        unique_count=1,
        message="Complete",
    )


def test_schedule_config_validation_and_launcher(tmp_path):
    config_path = tmp_path / "output" / "scheduled_search.json"
    launcher_path = tmp_path / "output" / "run.cmd"
    config = {
        "enabled": True,
        "keyword": "Fachinformatiker",
        "locations": ["Bonn", "Köln"],
        "schedule_time": "08:45",
    }

    local_schedule.save_config(config_path, config)
    launcher = local_schedule.write_launcher(
        config_path,
        launcher_path,
        project_dir=tmp_path,
        python_executable=tmp_path / "venv" / "python.exe",
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    content = launcher.read_text(encoding="utf-8")
    assert saved["source"] == "arbeitsagentur"
    assert saved["enabled"] is True
    assert "cd /d" in content
    assert "-m scraper.scheduled_search" in content
    with pytest.raises(ValueError, match="requires a keyword"):
        local_schedule.save_config(config_path, {"enabled": True, "keyword": ""})


def test_scheduled_search_imports_and_notifies_only_new_rows(tmp_path):
    config_path = tmp_path / "scheduled_search.json"
    search_db = tmp_path / "search.sqlite"
    crm_db = tmp_path / "crm.sqlite"
    runs_db = tmp_path / "runs.sqlite"
    local_schedule.save_config(config_path, {
        "enabled": True,
        "keyword": "Fachinformatiker",
        "locations": ["Bonn"],
        "schedule_time": "08:45",
        "processing_purpose": "Ausbildungsstellen prüfen",
        "legal_basis": "Organisation approval",
    })
    calls = []

    def fake_search(keyword, location, **_kwargs):
        calls.append((keyword, location))
        return _result()

    first = run_scheduled_search(
        config_path,
        search_database_path=search_db,
        crm_database_path=crm_db,
        runs_database_path=runs_db,
        search_fn=fake_search,
    )
    second = run_scheduled_search(
        config_path,
        search_database_path=search_db,
        crm_database_path=crm_db,
        runs_database_path=runs_db,
        search_fn=fake_search,
    )

    assert calls == [("Fachinformatiker", "Bonn")]
    assert first["status"] == "complete"
    assert first["new_count"] == 1
    assert second is None
    saved = crm.list_leads(crm_db)[0]
    assert saved["source"] == "Jobbörse"
    assert saved["processing_purpose"] == "Ausbildungsstellen prüfen"
    assert local_schedule.recent_runs(runs_db)[0]["new_count"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler integration")
def test_register_uses_interactive_user_task_without_credentials(tmp_path, monkeypatch):
    config_path = tmp_path / "schedule.json"
    launcher_path = tmp_path / "run.cmd"
    config_path.write_text("{}", encoding="utf-8")
    launcher_path.write_text("@echo off\r\n", encoding="utf-8")
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_schedule.subprocess, "run", fake_run)

    local_schedule.register_windows_task(config_path, launcher_path, "08:45")

    assert "/IT" in captured["command"]
    assert "/ST" in captured["command"]
    assert "08:45" in captured["command"]
    assert not any("password" in argument.casefold() for argument in captured["command"])
