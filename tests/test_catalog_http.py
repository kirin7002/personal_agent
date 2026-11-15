from copy import deepcopy
import shutil

from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app


def test_http_catalog_preview_apply_export_and_profile(tmp_path):
    folder = tmp_path / "capabilities"
    shutil.copytree(ROOT / "capabilities", folder)
    app = create_app(Settings(db_path=tmp_path / "web.db", catalog_dir=folder, hermes_key="", dashscope_key=""))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/api/catalog/reload").status_code == 401
        client.post("/api/session")
        before = client.get("/api/catalog").json()
        root = client.get("/api/catalog/tree").json()
        assert len(root["items"]) == 3 and "input_schema" not in str(root)
        bundle = client.get("/api/catalog/export").json()
        tool = deepcopy(next(t for t in bundle["tools"] if t["id"] == "control.climate.set_temperature"))
        tool["id"] = "control.climate.set_extra_temperature"
        preview = client.post("/api/catalog/import/preview", json={"payload": tool, "mode": "merge"})
        assert preview.status_code == 200
        p = preview.json()
        assert client.get("/api/catalog").json()["counts"]["control"] == 50  # Preview is read-only.
        assert p["diff"]["tools"]["added"] == [tool["id"]]
        r = client.post("/api/catalog/import/apply", json={"preview_id": p["preview_id"], "base_revision": p["base_revision"]})
        assert r.status_code == 200
        assert client.get("/api/catalog").json()["counts"]["control"] == 51
        assert client.post("/api/catalog/reload").status_code == 200
        assert len(client.get("/api/catalog/export").json()["tools"]) == 69
        r = client.post("/api/examples/"+tool["id"])
        assert r.status_code == 200 and r.json()["status"] == "generated"
        bad = deepcopy(tool)
        bad["input_schema"] = {"type": "object", "$dynamicRef": "https://example.com/private"}
        assert client.post("/api/catalog/import/preview", json={"payload": bad, "mode": "upsert"}).status_code == 409
        assert client.get("/api/catalog").json()["counts"]["control"] == 51
        assert client.post("/api/session/profile", json={"profile_id": "demo-no-suspension"}).status_code == 200
        failed = client.post("/api/examples/control.suspension.set_height").json()
        assert failed["status"] == "unsupported" and failed["calls"] == []
        query = client.post("/api/examples/status.vehicle.get_configuration").json()
        assert query["status"] == "completed" and not query["simulated_dispatch"]
        assert query["results"][0]["data"]["equipment"]["air_suspension"] is False
        assert "下发成功" not in query["summary"]
