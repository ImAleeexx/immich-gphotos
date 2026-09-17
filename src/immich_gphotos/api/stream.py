import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from immich_gphotos.api.routes import status_snapshot

router = APIRouter()


@router.get("/events")
async def events(request: Request, interval: float = 2.0, max_events: int = 0):
    """Server-sent status frames. `max_events` bounds the stream, for tests."""
    services = request.app.state.services

    async def frames():
        sent = 0
        while max_events == 0 or sent < max_events:
            if await request.is_disconnected():
                return
            yield f"data: {json.dumps(status_snapshot(services))}\n\n"
            sent += 1
            if max_events == 0 or sent < max_events:
                await asyncio.sleep(interval)

    return StreamingResponse(frames(), media_type="text/event-stream")
