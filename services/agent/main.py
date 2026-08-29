"""FastAPI entrypoint for agent-service — ARCHITECTURE.md §2, §7.

Health-check only. No WhatsApp webhook, no LLM tool, no booking/payment
code path here — each of those is a separate, larger unit of work that
CLAUDE.md rule 10 requires asking about before adding (a new tool the model
can call, or anything touching payment/booking confirmation).

Run locally with: uvicorn services.agent.main:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="hotel-sales-agent")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check. No database round-trip, no external call — this
    only proves the process itself is up."""
    return {"status": "ok"}
