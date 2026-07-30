# tests/test_v2_health_router.py
#
# main.py's app needs a live Postgres pool at startup (see its lifespan),
# so none of the 52+ routes still defined directly on `app` in main.py
# can be exercised with TestClient without a database - that's real,
# pre-existing test debt, not something this file fixes. What *can* be
# tested in isolation, at the actual HTTP layer rather than just the
# handler function, is any router with no such dependency - starting
# with the simplest one, v2's health check.
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.v2.health import router as v2_health_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_health_router, prefix="/api/v2")
    return TestClient(app)


def test_v2_health_returns_ok():
    response = _client().get("/api/v2/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "v2"}


def test_v2_health_is_get_only():
    response = _client().post("/api/v2/health")
    assert response.status_code == 405
