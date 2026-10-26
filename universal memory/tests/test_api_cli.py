import json

from fastapi.testclient import TestClient

from universal_memory.api import create_app
from universal_memory.cli import main


def test_api_crud_recall_and_validation(engine):
    with TestClient(create_app(engine)) as client:
        response = client.post("/memories", json={"content": "Alex created a game"})
        assert response.status_code == 201, response.text
        id = response.json()["id"]
        assert client.post("/recall", json={"query": "Alex game"}).json()[0]["memory"]["id"] == id
        assert client.get(f"/memories/{id}").status_code == 200
        assert client.patch(f"/memories/{id}", json={"importance": 0.9}).status_code == 200
        for action in ("pin", "unpin", "archive", "restore", "forget", "restore"):
            assert client.post(f"/memories/{id}/{action}").status_code == 200
        assert client.get("/memories").json()[0]["id"] == id
        assert client.get("/stats").json()["total"] == 1
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/openapi.json").status_code == 200
        assert client.post("/memories", json={"content": " "}).status_code == 422
        assert client.post("/recall", json={"query": "x", "top_k": 0}).status_code == 422
        assert client.patch(f"/memories/{id}", json={"namespace": "escape"}).status_code == 422
        assert client.delete(f"/memories/{id}").status_code == 204
        assert client.get(f"/memories/{id}").status_code == 404


def test_api_auth(engine):
    with TestClient(create_app(engine, api_key="test-secret")) as client:
        assert client.get("/stats").status_code == 401
        assert (
            client.get("/stats", headers={"Authorization": "Bearer test-secret"}).status_code == 200
        )


def test_api_links_consolidation_and_maintenance(engine):
    with TestClient(create_app(engine)) as client:
        a = client.post("/memories", json={"content": "Chess on Monday"}).json()["id"]
        b = client.post("/memories", json={"content": "Chess on Tuesday"}).json()["id"]
        assert client.post(f"/memories/{a}/links", json={"target": b}).status_code == 204
        result = client.post("/consolidations", json={"ids": [a, b], "content": "Chess often"})
        assert result.status_code == 201
        id = result.json()["id"]
        assert client.get(f"/consolidations/{id}").status_code == 200
        assert client.post(f"/consolidations/{id}/reverse").status_code == 204
        assert client.post("/maintenance/rebuild-index").json()["pending"] == 0


def test_cli(tmp_path, capsys):
    args = ["--data", str(tmp_path / "cli")]
    assert main([*args, "remember", "Alex memory"]) == 0
    id = json.loads(capsys.readouterr().out)["id"]
    assert main([*args, "search", "Alex"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["memory"]["id"] == id
    assert main([*args, "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert main([*args, "inspect", "missing"]) == 1
