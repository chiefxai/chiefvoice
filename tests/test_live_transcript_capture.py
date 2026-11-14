from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import TranscriptionFrame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from chiefvoice.agents.call.pipeline import build_pipeline
from chiefvoice.agents.post_call.transcript import TranscriptCollector
from chiefvoice.sessions import AcceptedSession, Direction


@pytest.mark.asyncio
async def test_separate_taps_capture_caller_and_assistant_transcript_and_persist_segments():
    session = AcceptedSession("phone", "vobiz", Direction.OUTBOUND, tenant_id="tenant-1")
    persist_segment = AsyncMock()
    session.metadata["persist_transcript_segment"] = persist_segment
    caller_tap, assistant_tap = TranscriptCollector.for_pipeline(session)
    caller_tap.push_frame = AsyncMock()
    assistant_tap.push_frame = AsyncMock()

    await caller_tap.process_frame(
        TranscriptionFrame("I need to reschedule.", "caller", "now"),
        FrameDirection.UPSTREAM,
    )
    await assistant_tap.process_frame(
        TTSTextFrame("I can help with that.", "sentences"),
        FrameDirection.DOWNSTREAM,
    )

    assert session.metadata["post_call_transcript"] == (
        "Caller: I need to reschedule.\nAssistant: I can help with that."
    )
    assert persist_segment.await_args_list[0].args == ("Caller", "I need to reschedule.")
    assert persist_segment.await_args_list[1].args == ("Assistant", "I can help with that.")


def test_pipeline_places_transcript_taps_on_both_sides_of_the_model():
    transport = _FakeTransport()
    model = FrameProcessor()
    caller_tap = TranscriptCollector(
        AcceptedSession("phone", "vobiz", Direction.OUTBOUND), capture_assistant=False
    )
    assistant_tap = TranscriptCollector(
        AcceptedSession("phone", "vobiz", Direction.OUTBOUND), capture_user=False
    )

    pipeline = build_pipeline(
        transport,
        [model],
        transcript_processor=caller_tap,
        assistant_transcript_processor=assistant_tap,
    )

    processors = pipeline.processors
    assert processors.index(caller_tap) < processors.index(model)
    assert processors.index(model) < processors.index(assistant_tap)
    assert processors.index(assistant_tap) < processors.index(transport.output_processor)


class _FakeTransport:
    def __init__(self):
        self.input_processor = FrameProcessor()
        self.output_processor = FrameProcessor()

    def input(self):
        return self.input_processor

    def output(self):
        return self.output_processor
