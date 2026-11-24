"""
Fake Gemini Live WebSocket Server and Mock Client for Chirkoot MK-V.
Provides deterministic, zero-cost in-process and standalone test fixtures
simulating the Gemini 3.8 Live bidirectional protocol (audio, video, text,
tools, session resumption, GoAway frames, and interruptions).
"""

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

logger = logging.getLogger("FakeGeminiServer")


@dataclass
class FakeBlob:
    data: bytes
    mime_type: str = "audio/pcm;rate=24000"


@dataclass
class FakePart:
    text: Optional[str] = None
    inline_data: Optional[FakeBlob] = None


@dataclass
class FakeModelTurn:
    parts: List[FakePart] = field(default_factory=list)


@dataclass
class FakeTranscription:
    text: str


@dataclass
class FakeServerContent:
    model_turn: Optional[FakeModelTurn] = None
    interrupted: bool = False
    input_transcription: Optional[FakeTranscription] = None
    output_transcription: Optional[FakeTranscription] = None
    turn_complete: bool = False


@dataclass
class FakeUsageMetadata:
    prompt_token_count: int = 150
    candidates_token_count: int = 50
    total_token_count: int = 200


@dataclass
class FakeFunctionCall:
    id: str
    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeToolCall:
    function_calls: List[FakeFunctionCall] = field(default_factory=list)


@dataclass
class FakeToolCallCancellation:
    ids: List[str] = field(default_factory=list)


@dataclass
class FakeSessionResumptionUpdate:
    new_handle: str = "mock_hdl_gemini_38_live_resumption_token_xyz"
    resumption_token: str = "mock_hdl_gemini_38_live_resumption_token_xyz"
    last_acked_index: int = 0


@dataclass
class FakeGoAway:
    time_left: str = "10s"


@dataclass
class FakeLiveResponse:
    server_content: Optional[FakeServerContent] = None
    tool_call: Optional[FakeToolCall] = None
    session_resumption_update: Optional[FakeSessionResumptionUpdate] = None
    go_away: Optional[FakeGoAway] = None
    usage_metadata: Optional[FakeUsageMetadata] = None
    tool_call_cancellation: Optional[FakeToolCallCancellation] = None


class FakeLiveSession:
    """Mock Gemini Live Session conforming to google-genai LiveSession protocol."""

    def __init__(self, model_name: str = "gemini-3.8-live", config: Any = None):
        self.model_name = model_name
        self.config = config
        self.response_queue: asyncio.Queue = asyncio.Queue()
        self.received_inputs: List[Dict[str, Any]] = []
        self.received_content_turns: List[Any] = []
        self.received_tool_responses: List[Any] = []
        self.is_closed = False
        self.on_tool_response_callback: Optional[Callable[["FakeLiveSession", Any], None]] = None

    async def receive(self) -> AsyncIterator[FakeLiveResponse]:
        """Async iterator yielding responses from the mock Gemini Live server."""
        while not self.is_closed:
            try:
                # Wait for next response with timeout to allow clean exit
                resp = await asyncio.wait_for(self.response_queue.get(), timeout=0.1)
                if resp is None:  # Sentinel for session termination
                    break
                yield resp
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def send_realtime_input(
        self,
        audio: Any = None,
        video: Any = None,
        text: Optional[str] = None,
        audio_stream_end: bool = False
    ):
        """Records incoming realtime streaming input from Chirkoot client."""
        self.received_inputs.append({
            "type": "realtime",
            "has_audio": audio is not None,
            "has_video": video is not None,
            "text": text,
            "audio_stream_end": audio_stream_end,
            "timestamp": time.time()
        })

    async def send_client_content(self, turns: Any = None, turn_complete: bool = False):
        """Records re-seeded conversation turns or prompt turns."""
        self.received_content_turns.append({
            "turns": turns,
            "turn_complete": turn_complete,
            "timestamp": time.time()
        })

    async def send_tool_response(self, function_responses: Any = None):
        """Records tool execution responses returned by Chirkoot dispatcher."""
        self.received_tool_responses.append({
            "responses": function_responses,
            "timestamp": time.time()
        })
        if self.on_tool_response_callback:
            res = self.on_tool_response_callback(self, function_responses)
            if asyncio.iscoroutine(res):
                await res

    # -------------------------------------------------------------------------
    # Programmatic Test Control Helpers
    # -------------------------------------------------------------------------

    def inject_text_transcript(self, text: str, is_user: bool = False):
        """Injects a user or model transcript response."""
        content = FakeServerContent(
            input_transcription=FakeTranscription(text=text) if is_user else None,
            output_transcription=FakeTranscription(text=text) if not is_user else None
        )
        self.response_queue.put_nowait(FakeLiveResponse(server_content=content))

    def inject_model_audio(self, pcm_bytes: bytes = b"\x00" * 960):
        """Injects simulated 24 kHz audio chunk."""
        blob = FakeBlob(data=pcm_bytes, mime_type="audio/pcm;rate=24000")
        part = FakePart(inline_data=blob)
        content = FakeServerContent(model_turn=FakeModelTurn(parts=[part]))
        self.response_queue.put_nowait(FakeLiveResponse(server_content=content))

    def inject_tool_call(self, call_id: str, tool_name: str, args: Dict[str, Any]):
        """Injects a tool call request for Chirkoot dispatcher."""
        fc = FakeFunctionCall(id=call_id, name=tool_name, args=args)
        tc = FakeToolCall(function_calls=[fc])
        self.response_queue.put_nowait(FakeLiveResponse(tool_call=tc))

    def inject_tool_call_cancellation(self, call_ids: List[str]):
        """Injects cancellation for pending tool calls."""
        canc = FakeToolCallCancellation(ids=call_ids)
        self.response_queue.put_nowait(FakeLiveResponse(tool_call_cancellation=canc))

    def inject_resumption_update(self, handle: str, ack_index: int = 1):
        """Injects a 2-hour session resumption handle update."""
        update = FakeSessionResumptionUpdate(
            new_handle=handle,
            resumption_token=handle,
            last_acked_index=ack_index
        )
        self.response_queue.put_nowait(FakeLiveResponse(session_resumption_update=update))

    def inject_go_away(self, time_left: str = "5s"):
        """Injects a GoAway signal warning of impending connection termination."""
        self.response_queue.put_nowait(FakeLiveResponse(go_away=FakeGoAway(time_left=time_left)))

    def inject_interrupted(self):
        """Injects user interruption notification."""
        content = FakeServerContent(interrupted=True)
        self.response_queue.put_nowait(FakeLiveResponse(server_content=content))

    def inject_turn_complete(self):
        """Injects end-of-turn indicator."""
        content = FakeServerContent(turn_complete=True)
        self.response_queue.put_nowait(FakeLiveResponse(server_content=content))

    def inject_usage_metadata(self, prompt_tokens: int = 100, candidate_tokens: int = 25):
        """Injects token consumption metadata."""
        usage = FakeUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidate_tokens,
            total_token_count=prompt_tokens + candidate_tokens
        )
        self.response_queue.put_nowait(FakeLiveResponse(usage_metadata=usage))

    def close(self):
        """Closes the session."""
        self.is_closed = True
        self.response_queue.put_nowait(None)


class _FakeAsyncContextManager:
    def __init__(self, session: FakeLiveSession):
        self.session = session

    async def __aenter__(self) -> FakeLiveSession:
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.session.close()


class _FakeLiveNamespace:
    def __init__(self, client: "FakeGenAIClient"):
        self._client = client

    def connect(self, model: str = "gemini-3.8-live", config: Any = None):
        session = FakeLiveSession(model_name=model, config=config)
        self._client.sessions.append(session)
        if self._client.session_initializer:
            self._client.session_initializer(session)
        return _FakeAsyncContextManager(session)


class _FakeAioNamespace:
    def __init__(self, client: "FakeGenAIClient"):
        self.live = _FakeLiveNamespace(client)


class FakeGenAIClient:
    """Mock drop-in replacement for google.genai.Client."""

    def __init__(self, api_key: str = "mock-api-key"):
        self.api_key = api_key
        self.sessions: List[FakeLiveSession] = []
        self.session_initializer: Optional[Callable[[FakeLiveSession], None]] = None
        self.aio = _FakeAioNamespace(self)

    @property
    def latest_session(self) -> Optional[FakeLiveSession]:
        return self.sessions[-1] if self.sessions else None
