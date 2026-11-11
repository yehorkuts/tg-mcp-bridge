def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_post(client):
    assert client.post("/health").status_code == 405
