from xml.etree.ElementTree import Element, tostring

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from chiefvoice.calls.coordinator import CallCoordinator
from chiefvoice.channels.vobiz.adapter import VobizAdapter

router = APIRouter()


def _adapter(request: Request) -> VobizAdapter:
    adapter: VobizAdapter | None = getattr(request.app.state, "vobiz_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="Vobiz is not configured")
    return adapter


@router.api_route("/webhooks/vobiz/inbound", methods=["GET", "POST"])
async def inbound_answer(request: Request):
    adapter = _adapter(request)
    coordinator: CallCoordinator | None = getattr(request.app.state, "call_coordinator", None)
    pipeline_factory = getattr(request.app.state, "voice_pipeline_factory", None)
    if coordinator is None or pipeline_factory is None:
        call = await adapter.parse_inbound(request)
        reason = (
            "Call services are not configured"
            if coordinator is None
            else "Voice pipeline is not configured"
        )
        return await adapter.reject_inbound(call, reason)
    return await coordinator.handle_inbound("vobiz", request)


@router.api_route("/webhooks/vobiz/answer", methods=["GET", "POST"])
async def outbound_answer(request: Request):
    coordinator: CallCoordinator | None = getattr(request.app.state, "call_coordinator", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Call services are not configured")
    adapter = _adapter(request)
    session = await adapter.answer_outbound(request)
    try:
        await coordinator.mark_answered(session)
    except LookupError:
        return adapter.reject_outbound_answer()
    return adapter.outbound_answer_response(session)


@router.api_route("/webhooks/vobiz/hangup", methods=["GET", "POST"])
async def hangup_callback(request: Request):
    adapter = _adapter(request)
    coordinator: CallCoordinator | None = getattr(request.app.state, "call_coordinator", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Call services are not configured")
    session = await adapter.hangup_callback(request)
    # If media is still active, let its WebSocket finalizer flush the Pipecat
    # audio buffer and persist recording/transcript state before ending the call.
    # Only calls without an active media session are finalized directly here.
    media_session_closed = await adapter.close_active_media_session(session.session_id)
    if not media_session_closed:
        await coordinator.end_session(session, "completed")
    return Response(
        content=tostring(Element("Response"), encoding="unicode"),
        media_type="application/xml",
    )


@router.websocket("/webhooks/vobiz/media")
async def media_websocket(websocket: WebSocket, state: str = ""):
    adapter: VobizAdapter | None = getattr(websocket.app.state, "vobiz_adapter", None)
    coordinator: CallCoordinator | None = getattr(websocket.app.state, "call_coordinator", None)
    if adapter is None or coordinator is None:
        await websocket.close(code=1011)
        return
    pipeline_factory = getattr(websocket.app.state, "voice_pipeline_factory", None)
    try:
        await adapter.run_media_session(
            websocket,
            state,
            pipeline_factory,
            coordinator.end_session,
            coordinator.attach_session_persistence,
        )
    except WebSocketDisconnect:
        pass
