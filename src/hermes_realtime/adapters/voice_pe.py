"""Voice PE audio adapter for Hermes Realtime Bridge.

This adapter connects to an ESPHome device running custom firmware that streams
16kHz mono PCM16 audio over WebSocket. It handles sending audio to the device
for playback and receiving audio from the device's microphone.

Implements the `AudioAdapter` ABC from `hermes_realtime.core`.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
import websockets

from hermes_realtime.core import AudioAdapter

logger = logging.getLogger(__name__)


@dataclass
class VoicePEConfig:
    """Configuration for the Voice PE adapter."""

    host: str = "voice-pe.local"
    port: int = 6053
    api_password: Optional[str] = None
    sample_rate: int = 16000


class VoicePEAdapter(AudioAdapter):
    """AudioAdapter for Voice PE (Preview Edition) hardware.

    Connects to an ESPHome device via WebSocket, streams audio, and handles
    LED state updates.
    """

    name: str = "voice-pe"

    def __init__(self, config: VoicePEConfig):
        self.config = config
        self.host = config.host
        self.port = config.port
        self.api_password = config.api_password

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.tts_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._running = False
        self._websocket_task: Optional[asyncio.Task] = None
        self._read_audio_task: Optional[asyncio.Task] = None
        self._write_audio_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Connect to the ESPHome WebSocket and start audio streaming."""
        self._running = True
        await self.set_state("idle")

        ws_url = f"ws://{self.host}:{self.port}/"
        logger.info("Connecting to Voice PE: %s", ws_url)

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self.ws = ws
                    logger.info("Connected to Voice PE WebSocket.")

                    # Authenticate if password provided
                    if self.api_password:
                        await ws.send(json.dumps({"type": "auth", "api_password": self.api_password}))
                        auth_response = await ws.recv()
                        if json.loads(auth_response).get("type") != "auth_ok":
                            logger.error("Voice PE authentication failed.")
                            raise websockets.ConnectionClosedError(None, None, "Auth failed")
                        logger.info("Voice PE authenticated.")

                    # Start audio processing tasks
                    self._read_audio_task = asyncio.create_task(self._read_audio_loop())
                    self._write_audio_task = asyncio.create_task(self._write_audio_loop())
                    self._websocket_task = asyncio.create_task(self._keepalive_loop())

                    await self._websocket_task

            except (websockets.ConnectionClosed, websockets.ConnectionClosedError, aiohttp.ClientConnectorError) as e:
                logger.warning("Voice PE connection error: %s. Retrying in 5s...", e)
            except Exception as e:
                logger.exception("Voice PE unexpected error: %s", e)
            finally:
                for task in [self._read_audio_task, self._write_audio_task, self._websocket_task]:
                    if task:
                        task.cancel()
                if self.ws:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                self.ws = None
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Gracefully disconnect and clean up."""
        logger.info("Stopping Voice PE adapter...")
        self._running = False

        for task in [self._read_audio_task, self._write_audio_task, self._websocket_task]:
            if task:
                task.cancel()

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None

        # Drain queues
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        while not self.tts_queue.empty():
            self.tts_queue.get_nowait()

        logger.info("Voice PE adapter stopped.")

    async def _read_audio_loop(self) -> None:
        """Receive audio and control messages from the ESPHome WebSocket."""
        while self._running and self.ws:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=1.0)

                if isinstance(message, bytes):
                    # Binary = audio data
                    try:
                        self.audio_queue.put_nowait(message)
                    except asyncio.QueueFull:
                        pass
                else:
                    # Text = JSON control message
                    try:
                        msg_data = json.loads(message)
                        if msg_data.get("type") == "led":
                            await self.set_state(msg_data.get("state", "idle"))
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON text message: %s", message[:100])

            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                logger.warning("Voice PE WebSocket closed during receive.")
                self._running = False
                break
            except Exception as e:
                logger.exception("Error in read loop: %s", e)
                self._running = False
                break

    async def _write_audio_loop(self) -> None:
        """Send TTS audio from queue to ESPHome speaker."""
        while self._running and self.ws:
            try:
                pcm_chunk = await self.tts_queue.get()
                try:
                    await self.ws.send(pcm_chunk)
                finally:
                    self.tts_queue.task_done()
            except websockets.ConnectionClosed:
                logger.warning("Voice PE WebSocket closed during send.")
                self._running = False
                break
            except Exception as e:
                logger.exception("Error in write loop: %s", e)
                self._running = False
                break

    async def _keepalive_loop(self) -> None:
        """Keep the WebSocket task alive for cancellation tracking."""
        while self._running and self.ws:
            await asyncio.sleep(0.1)

    async def read_audio(self) -> Optional[bytes]:
        """Read PCM16 audio from the ESPHome microphone. Returns None if no data."""
        if not self._running:
            return None
        try:
            return await asyncio.wait_for(self.audio_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    async def write_audio(self, pcm: bytes) -> None:
        """Queue PCM16 audio for playback on the ESPHome speaker."""
        if not self._running:
            return
        try:
            self.tts_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            logger.debug("TTS queue full, dropping chunk")

    async def set_state(self, state: str) -> None:
        """Send LED state update to the ESPHome device."""
        if not self._running or not self.ws:
            return
        message = {"type": "led", "state": state}
        try:
            await self.ws.send(json.dumps(message))
            logger.debug("Voice PE state: %s", state)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            logger.debug("Failed to send state: %s", e)
