"""services/agent/main.py — the health-check endpoint.

No TestClient here: FastAPI's TestClient needs httpx, a dependency not yet
requested or approved (CLAUDE.md's dependency rules) for a handler this
simple. Calling the route function directly exercises its actual logic;
services/agent/main.py itself is a one-line `@app.get` wiring the two
together, verified separately by actually running the server (see the PR
description), not worth a dependency to also cover here.
"""

from __future__ import annotations

import asyncio

from services.agent.main import health


def test_health_returns_ok() -> None:
    assert asyncio.run(health()) == {"status": "ok"}
