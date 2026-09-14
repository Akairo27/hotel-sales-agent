"""FastAPI entrypoint for agent-service — ARCHITECTURE.md §2, §7.

Health-check plus the WhatsApp Cloud API webhook (services/agent/webhook.py)
— signature verification, idempotent inbound logging, and spend/rate-cap
enforcement. No output guard and no outbound WhatsApp send yet: see
webhook.py's own module docstring for why that split is deliberate. No
booking/payment code path here — CLAUDE.md rule 10 requires asking about
before adding anything touching payment/booking confirmation.

Run locally with: uvicorn services.agent.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

from services.agent.webhook import router as webhook_router

app = FastAPI(title="hotel-sales-agent")
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check. No database round-trip, no external call — this
    only proves the process itself is up."""
    return {"status": "ok"}
