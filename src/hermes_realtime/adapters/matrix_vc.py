"""Matrix Voice Channel audio adapter for Hermes Realtime Bridge (LiveKit).

Uses LiveKit SDK to join MatrixRTC calls directly — no legacy m.call.* protocol.
Element Call creates a LiveKit room; this adapter joins as a participant and
bridges audio between LiveKit (Opus 48kHz) and the OpenAI Realtime API (24kHz PCM16).

Architecture:
  Element Call → LiveKit Server (LXC 209) → this adapter → RealtimeBridge → OpenAI

Requires:
  - LiveKit server running on the Synapse host
  - lk-jwt-service for Matrix auth
  - Synapse configured with livekit.* settings
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Optional

from hermes_realtime.core import AudioAdapter

logger = logging.getLogger(__name__)


@dataclass
class MatrixVCConfig:
    """Configuration for the Matrix VC adapter (LiveKit backend)."""

    # LiveKit connection
    livekit_url: str = "ws://192.168.0.7:7880"
    api_key: str = ""
    api_secret: str = ""

    # Matrix room to monitor for calls
    room_id: str = ""

    # Audio
    sample_rate: int = 24000  # OpenAI Realtime API GA uses 24kHz

    # Auto-join: if True, join calls automatically
    auto_join: bool = True


class MatrixVCAdapter(AudioAdapter):
    """Joins LiveKit rooms for MatrixRTC calls, bridges audio to Realtime API.

    This adapter connects directly to the LiveKit server. When Element Call
    creates a LiveKit room for a Matrix call, this adapter joins as a
    participant and streams audio.
    """

    name = "matrix-vc"

    def __init__(self, config: MatrixVCConfig):
        self.config = config
        self.livekit_url = config.livekit_url
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.room_id = config.room_id

        # LiveKit room connection
        self._room = None
        self._audio_track = None
        self._connected = asyncio.Event()

        # Audio queues
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self.tts_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        # State
        self._running = False
        self._in_call = False
        self._participant_identity = f"jarvis-{secrets.token_hex(4)}"

    # ── AudioAdapter Interface ────────────────────────────────────────

    async def start(self) -> None:
        """Connect to LiveKit and join the room."""
        self._running = True
        await self.set_state("idle")

        if not self.api_key or not self.api_secret:
            logger.error("LiveKit API key/secret not configured")
            return

        # Connect to LiveKit room
        await self._join_room()
        await self.set_state("listening")
        logger.info(
            "Matrix VC adapter started: livekit=%s room=%s identity=%s",
            self.livekit_url,
            self.room_id,
            self._participant_identity,
        )

    async def stop(self) -> None:
        """Leave LiveKit room and clean up."""
        logger.info("Stopping Matrix VC adapter...")
        self._running = False

        if self._room:
            try:
                await self._room.disconnect()
            except Exception:
                pass
            self._room = None

        # Drain queues
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        while not self.tts_queue.empty():
            self.tts_queue.get_nowait()

        logger.info("Matrix VC adapter stopped.")

    async def read_audio(self) -> Optional[bytes]:
        """Read decoded PCM16 audio from LiveKit participants."""
        if not self._running or not self._in_call:
            return None
        try:
            return await asyncio.wait_for(self.audio_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    async def write_audio(self, pcm: bytes) -> None:
        """Queue PCM16 audio for sending to LiveKit room."""
        if not self._running or not self._in_call:
            return
        try:
            self.tts_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            logger.debug("TTS queue full, dropping chunk")

    async def set_state(self, state: str) -> None:
        """Log state change."""
        logger.debug("Matrix VC state: %s", state)

    # ── LiveKit Connection ────────────────────────────────────────────

    async def _join_room(self) -> None:
        """Connect to LiveKit server and join the call room."""
        from livekit import api, rtc

        # Generate access token
        token = (
            api.AccessToken(self.api_key, self.api_secret)
            .with_identity(self._participant_identity)
            .with_name("Jarvis")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self.room_id,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
            .to_jwt()
        )

        logger.info("Connecting to LiveKit: %s", self.livekit_url)

        self._room = rtc.Room()

        # Set up event handlers
        @self._room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant):
            logger.info(
                "Participant joined: %s (%s)",
                participant.identity,
                participant.name or "unknown",
            )
            self._in_call = True
            self._connected.set()

        @self._room.on("participant_disconnected")
        def on_participant_disconnected(participant: rtc.RemoteParticipant):
            logger.info("Participant left: %s", participant.identity)
            # Check if anyone else is still in the room
            if len(list(self._room.remote_participants.values())) == 0:
                self._in_call = False
                logger.info("All participants left — call ended")

        @self._room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            logger.info(
                "Track subscribed: %s from %s (kind=%s)",
                track.name or "unnamed",
                participant.identity,
                track.kind,
            )
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(self._process_audio_track(track))

        @self._room.on("disconnected")
        def on_disconnected(reason=None):
            logger.info("Disconnected from LiveKit: %s", reason)
            self._in_call = False

        # Connect
        try:
            await self._room.connect(self.livekit_url, token)
            logger.info("Connected to LiveKit room: %s", self.room_id)

            # Publish our audio track
            self._audio_source = rtc.AudioSource(
                sample_rate=48000,
                num_channels=2,
            )
            self._audio_track = rtc.LocalAudioTrack.create_audio_track(
                "jarvis-voice", self._audio_source
            )
            await self._room.local_participant.publish_track(
                self._audio_track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
            )
            logger.info("Published audio track")

            # Start audio send loop
            asyncio.create_task(self._send_audio_loop())

        except Exception as e:
            logger.error("Failed to connect to LiveKit: %s", e)
            raise

    # ── Audio Processing ─────────────────────────────────────────────

    async def _process_audio_track(self, track: rtc.AudioTrack) -> None:
        """Receive audio from a LiveKit participant, decode to 24kHz PCM16."""
        import numpy as np

        audio_stream = rtc.AudioStream(track)
        # AudioStream is async iterable

        async for audio_frame_event in audio_stream:
            if not self._running:
                break

            frame = audio_frame_event.frame
            # frame is an AudioFrame with:
            # - frame.data: numpy array (samples × channels)
            # - frame.sample_rate: int
            # - frame.num_channels: int

            # Convert to PCM16 bytes
            pcm_48k = frame.data.astype(np.int16).tobytes()

            # Downsample 48kHz stereo → 24kHz mono
            pcm_24k = self._resample_48k_stereo_to_24k_mono(pcm_48k)

            try:
                self.audio_queue.put_nowait(pcm_24k)
            except asyncio.QueueFull:
                pass

    async def _send_audio_loop(self) -> None:
        """Read PCM24 from TTS queue, upsample to 48kHz stereo, send via LiveKit."""
        import numpy as np

        while self._running and self._room and self._room.connection_state != "disconnected":
            try:
                pcm_24k = await asyncio.wait_for(self.tts_queue.get(), timeout=0.5)

                # Upsample 24kHz mono → 48kHz stereo
                pcm_48k = self._resample_24k_mono_to_48k_stereo(pcm_24k)

                # Convert to numpy array for LiveKit
                num_samples = len(pcm_48k) // 4  # 4 bytes per stereo sample
                audio_data = np.frombuffer(pcm_48k, dtype=np.int16).reshape(
                    num_samples, 2
                )

                # Create and push audio frame
                from livekit import rtc

                frame = rtc.AudioFrame(
                    data=audio_data,
                    sample_rate=48000,
                    num_channels=2,
                    samples_per_channel=num_samples,
                )
                await self._audio_source.capture_frame(frame)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Audio send error: %s", e)
                await asyncio.sleep(0.1)

    # ── Resampling Helpers ────────────────────────────────────────────

    @staticmethod
    def _resample_48k_stereo_to_24k_mono(pcm: bytes) -> bytes:
        """Downsample 48kHz stereo PCM16 → 24kHz mono PCM16."""
        import struct

        num_samples = len(pcm) // 4
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples * 2}h", pcm)

        mono_24k = []
        for i in range(0, num_samples, 2):
            left = samples[i * 2]
            right = samples[i * 2 + 1] if i * 2 + 1 < len(samples) else left
            mono = (left + right) // 2
            mono_24k.append(mono)

        return struct.pack(f"<{len(mono_24k)}h", *mono_24k)

    @staticmethod
    def _resample_24k_mono_to_48k_stereo(pcm: bytes) -> bytes:
        """Upsample 24kHz mono PCM16 → 48kHz stereo PCM16."""
        import struct

        num_samples = len(pcm) // 2
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples}h", pcm)

        stereo_48k = []
        for s in samples:
            for _ in range(2):
                stereo_48k.append(s)
                stereo_48k.append(s)

        return struct.pack(f"<{len(stereo_48k)}h", *stereo_48k)
