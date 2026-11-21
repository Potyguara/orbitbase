import json

from app.services.startup_recovery import recover_interrupted_processing


def test_running_progress_is_marked_failed(tmp_path):
    adv = tmp_path / "processing" / "job_1_advanced" / "advanced_progress.json"
    adv.parent.mkdir(parents=True)
    adv.write_text(json.dumps({"percent": 56, "status": "running", "step": "geometry_start"}))
    done = tmp_path / "processing" / "job_2" / "progress.json"
    done.parent.mkdir(parents=True)
    done.write_text(json.dumps({"percent": 100, "status": "completed"}))

    result = recover_interrupted_processing(tmp_path)

    assert result["files"] == 1
    payload = json.loads(adv.read_text())
    assert payload["status"] == "failed"
    assert payload["percent"] == 56
    assert json.loads(done.read_text())["status"] == "completed"
