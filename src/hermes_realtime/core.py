"""Core Realtime Bridge — OpenAI Realtime API WebSocket client.

Handles the WebSocket lifecycle, audio streaming, and function call routing.
Audio source/sink adapters plug in via the AudioAdapter interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets
import numpy as np

logger = logging.getLogger(__name__)

# ── Audio Adapter Interface ──────────────────────────────────────────────


class AudioAdapter(ABC):
    """Abstract audio source/sink. Implement per transport (Voice PE, Discord VC)."""

    name: str = "base"

    @abstractmethod
    async def start(self) -> None:
        """Initialize the adapter (connect, join channel, etc.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the adapter."""
        ...

    @abstractmethod
    async def read_audio(self) -> Optional[bytes]:
        """Read a chunk of 16kHz mono PCM16 audio. Return None if no data."""
        ...

    @abstractmethod
    async def write_audio(self, pcm: bytes) -> None:
        """Write a chunk of 16kHz mono PCM16 audio to the sink."""
        ...

    @abstractmethod
    async def set_state(self, state: str) -> None:
        """Update visual state: idle, listening, thinking, speaking."""
        ...


# ── Tool Bridge Interface ────────────────────────────────────────────────


class ToolBridge(ABC):
    """Routes function calls from the Realtime API to actual tool execution."""

    @abstractmethod
    async def get_tools(self) -> list[dict]:
        """Return the tool definitions for the Realtime API session."""
        ...

    @abstractmethod
    async def execute(self, name: str, arguments: dict) -> str:
        """Execute a tool and return the result string."""
        ...


# ── Session Config ───────────────────────────────────────────────────────


@dataclass
class RealtimeConfig:
    """Configuration for a Realtime API session."""

    model: str = "gpt-4o-realtime-preview"
    voice: str = "alloy"
    instructions: str = (
        "You are Jarvis, a capable AI assistant. You have access to tools "
        "that let you control smart home devices, query infrastructure, and "
        "access persistent memory. Be concise and helpful. Use tools when "
        "appropriate."
    )
    temperature: float = 0.8
    input_audio_format: str = "pcm16"
    output_audio_format: str = "pcm16"
    input_audio_transcription: Optional[dict] = None
    turn_detection: Optional[dict] = field(
        default_factory=lambda: {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
        }
    )


# ── Core Bridge ──────────────────────────────────────────────────────────


class RealtimeBridge:
    """Main bridge: connects audio adapter ↔ Realtime API ↔ tool bridge."""

    def __init__(
        self,
        api_key: str,
        adapter: AudioAdapter,
        tool_bridge: Optional[ToolBridge] = None,
        config: Optional[RealtimeConfig] = None,
        base_url: str = "wss://api.openai.com/v1/realtime",
    ):
        self.api_key = api_key
        self.adapter = adapter
        self.tool_bridge = tool_bridge
        self.config = config or RealtimeConfig()
        self.base_url = base_url

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        """Run the bridge: connect, stream audio, handle responses."""
        self._running = True
        await self.adapter.start()
        await self.adapter.set_state("listening")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        url = f"{self.base_url}?model={self.config.model}"

        async with websockets.connect(url, extra_headers=headers) as ws:
            self._ws = ws
            logger.info("Connected to Realtime API")

            # Send session config
            await self._send_session_update()

            # Start audio + response loops
            audio_task = asyncio.create_task(self._audio_loop())
            response_task = asyncio.create_task(self._response_loop())
            self._tasks = [audio_task, response_task]

            done, pending = await asyncio.wait(
                self._tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    logger.error("Task failed: %s", task.exception())

        await self.adapter.stop()

    async def stop(self) -> None:
        """Stop the bridge."""
        self._running = False
        if self._ws:
            await self._ws.close()
        for task in self._tasks:
            task.cancel()

    # ── Internal ──────────────────────────────────────────────────────

    async def _send_session_update(self) -> None:
        """Send session configuration to the Realtime API."""
        session_config = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": self.config.instructions,
                "voice": self.config.voice,
                "input_audio_format": self.config.input_audio_format,
                "output_audio_format": self.config.output_audio_format,
                "temperature": self.config.temperature,
                "turn_detection": self.config.turn_detection,
            },
        }
        if self.tool_bridge:
            tools = await self.tool_bridge.get_tools()
            if tools:
                session_config["session"]["tools"] = tools
                session_config["session"]["tool_choice"] = "auto"

        if self.config.input_audio_transcription:
            session_config["session"]["input_audio_transcription"] = (
                self.config.input_audio_transcription
            )

        await self._ws.send(json.dumps(session_config))
        logger.info("Session configured: model=%s voice=%s", self.config.model, self.config.voice)

    async def _audio_loop(self) -> None:
        """Read audio from adapter, send to Realtime API."""
        chunk_ms = 20  # 20ms chunks
        chunk_bytes = int(16000 * 2 * chunk_ms / 1000)  # 16kHz, 16-bit mono

        while self._running:
            try:
                pcm = await self.adapter.read_audio()
                if pcm is None:
                    await asyncio.sleep(0.01)
                    continue

                # Send in 20ms chunks
                for i in range(0, len(pcm), chunk_bytes):
                    chunk = pcm[i : i + chunk_bytes]
                    if len(chunk) < chunk_bytes:
                        break
                    event = {
                        "type": "input_audio_buffer.append",
                        "audio": _bytes_to_b64(chunk),
                    }
                    await self._ws.send(json.dumps(event))

            except websockets.ConnectionClosed:
                break
            except Exception:
                logger.exception("Audio loop error")
                await asyncio.sleep(0.1)

    async def _response_loop(self) -> None:
        """Receive responses from Realtime API, route audio + function calls."""
        while self._running:
            try:
                msg = await self._ws.recv()
                event = json.loads(msg)
                event_type = event.get("type", "")

                if event_type == "response.audio.delta":
                    # Audio chunk from the model
                    audio_b64 = event.get("delta", "")
                    if audio_b64:
                        pcm = _b64_to_bytes(audio_b64)
                        await self.adapter.write_audio(pcm)

                elif event_type == "response.audio.done":
                    await self.adapter.set_state("listening")

                elif event_type == "response.audio_transcript.delta":
                    delta = event.get("delta", "")
                    if delta:
                        logger.info("Assistant: %s", delta)

                elif event_type == "response.function_call_arguments.done":
                    await self._handle_function_call(event)

                elif event_type == "input_audio_buffer.speech_started":
                    await self.adapter.set_state("listening")
                    # Cancel any in-progress response
                    await self._ws.send(json.dumps({"type": "response.cancel"}))

                elif event_type == "input_audio_buffer.speech_stopped":
                    await self.adapter.set_state("thinking")

                elif event_type == "error":
                    logger.error("Realtime API error: %s", event.get("error", {}))

            except websockets.ConnectionClosed:
                break
            except Exception:
                logger.exception("Response loop error")

    async def _handle_function_call(self, event: dict) -> None:
        """Execute a function call and send the result back."""
        if not self.tool_bridge:
            return

        call_id = event.get("call_id", "")
        name = event.get("name", "")
        arguments_str = event.get("arguments", "{}")

        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            arguments = {}

        logger.info("Function call: %s(%s)", name, arguments)
        await self.adapter.set_state("thinking")

        try:
            result = await self.tool_bridge.execute(name, arguments)
        except Exception as e:
            result = json.dumps({"error": str(e)})

        # Send result back
        output_event = {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        }
        await self._ws.send(json.dumps(output_event))

        # Trigger a response
        await self._ws.send(json.dumps({"type": "response.create"}))


# ── Helpers ──────────────────────────────────────────────────────────────

import base64


def _bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64_to_bytes(data: str) -> bytes:
    return base64.b64decode(data)
