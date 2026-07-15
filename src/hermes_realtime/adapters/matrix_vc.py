"""Matrix Voice Channel audio adapter for Hermes Realtime Bridge.

Connects to a Matrix room for voice calls, handles WebRTC signaling via
m.call.* events, and bridges Opus audio (48kHz) ↔ 24kHz PCM16 for the
OpenAI Realtime API.

Uses matrix-nio for Matrix client + aiortc for WebRTC.

Matrix VoIP flow:
  1. Caller sends m.call.invite (SDP offer) to room
  2. Callee sends m.call.answer (SDP answer) to room
  3. Both exchange m.call.candidates (ICE candidates)
  4. WebRTC peer connection established → Opus audio flows
  5. Either side sends m.call.hangup to end

Reference: https://spec.matrix.org/v1.9/client-server-api/#voice-over-ip
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from hermes_realtime.core import AudioAdapter

logger = logging.getLogger(__name__)


@dataclass
class MatrixVCConfig:
    """Configuration for the Matrix VC adapter."""

    homeserver: str = "https://matrix.hagger.id.au"
    access_token: str = ""
    user_id: str = "@jarvis:hagger.au"
    room_id: str = ""  # Room to join for voice calls
    sample_rate: int = 24000  # OpenAI Realtime API GA uses 24kHz

    # Auto-answer: if True, accept all incoming calls automatically
    auto_answer: bool = True


class MatrixVCAdapter(AudioAdapter):
    """Connects to Matrix voice calls, streaming audio via WebRTC + Opus.

    This adapter acts as a Matrix VoIP client:
    - Listens for m.call.invite events in the configured room
    - Auto-answers (or can be configured to require manual accept)
    - Handles WebRTC signaling (SDP offer/answer, ICE candidates)
    - Bridges Opus 48kHz ↔ 24kHz PCM16 for the Realtime API
    """

    name = "matrix-vc"

    def __init__(self, config: MatrixVCConfig):
        self.config = config
        self.homeserver = config.homeserver
        self.access_token = config.access_token or os.environ.get("MATRIX_ACCESS_TOKEN", "")
        self.user_id = config.user_id
        self.room_id = config.room_id

        # Matrix client (lazy init)
        self._client = None
        self._sync_task: Optional[asyncio.Task] = None

        # WebRTC
        self._pc = None  # RTCPeerConnection
        self._dc = None  # DataChannel (unused but required by some clients)

        # Audio queues
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self.tts_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        # Call state
        self._running = False
        self._in_call = False
        self._call_id: Optional[str] = None
        self._party_id: str = secrets.token_hex(4)  # 8-char hex ID
        self._opponent_party_id: Optional[str] = None
        self._call_start_time: float = 0

        # Opus codec
        self._opus_encoder = None
        self._opus_decoder = None

    # ── AudioAdapter Interface ────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Matrix and start listening for calls."""
        self._running = True
        await self.set_state("idle")

        if not self.access_token:
            logger.error("No Matrix access token. Set MATRIX_ACCESS_TOKEN env var.")
            return

        if not self.room_id:
            logger.error("No Matrix room ID configured.")
            return

        # Import here to avoid hard dependency at module level
        import nio

        # Create Matrix client
        self._client = nio.AsyncClient(
            homeserver=self.homeserver,
            user=self.user_id,
        )
        self._client.access_token = self.access_token

        # Register call event handler
        self._client.add_event_callback(self._on_call_event, nio.CallInviteEvent)
        self._client.add_event_callback(self._on_call_event, nio.CallAnswerEvent)
        self._client.add_event_callback(self._on_call_event, nio.CallHangupEvent)
        self._client.add_event_callback(self._on_call_event, nio.CallCandidatesEvent)

        # Start sync loop
        self._sync_task = asyncio.create_task(self._sync_loop())

        logger.info(
            "Matrix VC adapter started: user=%s room=%s",
            self.user_id,
            self.room_id,
        )

    async def stop(self) -> None:
        """Hang up any active call and disconnect."""
        logger.info("Stopping Matrix VC adapter...")
        self._running = False

        # Hang up if in call
        if self._in_call:
            await self._hangup()

        # Cancel sync
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        # Close Matrix client
        if self._client:
            await self._client.close()

        # Close WebRTC
        if self._pc:
            await self._pc.close()
            self._pc = None

        # Drain queues
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        while not self.tts_queue.empty():
            self.tts_queue.get_nowait()

        logger.info("Matrix VC adapter stopped.")

    async def read_audio(self) -> Optional[bytes]:
        """Read decoded PCM16 audio from the WebRTC peer connection."""
        if not self._running or not self._in_call:
            return None
        try:
            return await asyncio.wait_for(self.audio_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    async def write_audio(self, pcm: bytes) -> None:
        """Queue PCM16 audio for encoding and sending via WebRTC."""
        if not self._running or not self._in_call:
            return
        try:
            self.tts_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            logger.debug("TTS queue full, dropping chunk")

    async def set_state(self, state: str) -> None:
        """Log state change (no hardware LEDs on Matrix)."""
        logger.debug("Matrix VC state: %s", state)

    # ── Matrix Sync ──────────────────────────────────────────────────

    async def _sync_loop(self) -> None:
        """Sync with Matrix homeserver, processing call events."""
        import nio

        # Initial sync
        try:
            resp = await self._client.sync(timeout=30000)
            if isinstance(resp, nio.SyncError):
                logger.error("Initial sync failed: %s", resp.message)
                return
        except Exception as e:
            logger.error("Initial sync error: %s", e)
            return

        logger.info("Matrix sync started for room %s", self.room_id)

        # Continuous sync
        while self._running:
            try:
                resp = await self._client.sync(timeout=30000)
                if isinstance(resp, nio.SyncError):
                    logger.warning("Sync error: %s", resp.message)
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Sync loop error: %s", e)
                await asyncio.sleep(5)

    # ── Call Event Handler ───────────────────────────────────────────

    async def _on_call_event(self, room: "nio.MatrixRoom", event) -> None:
        """Handle incoming Matrix call events."""
        # Only process events for our configured room
        if room.room_id != self.room_id:
            return

        # Ignore our own events
        if event.sender == self.user_id:
            return

        event_type = type(event).__name__

        if event_type == "CallInviteEvent":
            await self._handle_invite(room, event)
        elif event_type == "CallAnswerEvent":
            await self._handle_answer(room, event)
        elif event_type == "CallCandidatesEvent":
            await self._handle_candidates(room, event)
        elif event_type == "CallHangupEvent":
            await self._handle_hangup(room, event)

    async def _handle_invite(self, room, event) -> None:
        """Handle incoming call invite — auto-answer if configured."""
        import nio

        call_id = event.call_id
        lifetime = event.lifetime  # milliseconds
        offer_sdp = event.offer_sdp

        logger.info(
            "Incoming call from %s (call_id=%s, lifetime=%dms)",
            event.sender,
            call_id,
            lifetime,
        )

        if self._in_call:
            logger.info("Already in a call, rejecting invite")
            await self._send_hangup_event(call_id, event.party_id)
            return

        if not self.config.auto_answer:
            logger.info("Auto-answer disabled, ignoring invite")
            return

        # Accept the call
        self._call_id = call_id
        self._opponent_party_id = event.party_id
        self._call_start_time = time.time()

        await self.set_state("listening")

        # Set up WebRTC
        await self._setup_webrtc()

        # Set remote description (the offer)
        from aiortc import RTCSessionDescription
        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
        await self._pc.setRemoteDescription(offer)

        # Create answer
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        # Send answer via Matrix
        content = {
            "call_id": call_id,
            "party_id": self._party_id,
            "version": "1",
            "answer": {
                "type": answer.type,
                "sdp": answer.sdp,
            },
        }
        await self._client.send_message(
            self.room_id,
            {
                "msgtype": "m.call.answer",
                "body": "Answer",
                **content,
            },
        )

        self._in_call = True
        logger.info("Call accepted: call_id=%s", call_id)

        # Start audio send loop
        asyncio.create_task(self._send_audio_loop())

    async def _handle_answer(self, room, event) -> None:
        """Handle call answer (when we initiated the call)."""
        if event.call_id != self._call_id:
            return

        logger.info("Call answered by %s", event.sender)
        self._opponent_party_id = event.party_id

        from aiortc import RTCSessionDescription
        answer = RTCSessionDescription(sdp=event.answer_sdp, type="answer")
        await self._pc.setRemoteDescription(answer)

        self._in_call = True
        await self.set_state("listening")

        # Start audio send loop
        asyncio.create_task(self._send_audio_loop())

    async def _handle_candidates(self, room, event) -> None:
        """Handle incoming ICE candidates."""
        if event.call_id != self._call_id or not self._pc:
            return

        from aiortc import RTCIceCandidate

        for candidate in event.candidates:
            if not candidate.sdp_mid and not candidate.candidate:
                continue  # end-of-candidates marker
            ice = RTCIceCandidate(
                component=candidate.sdp_m_line_index or 1,
                foundation=candidate.candidate.split(":")[0] if ":" in candidate.candidate else "",
                ip="",  # aiortc extracts from candidate string
                port=0,
                priority=0,
                protocol="udp",
                type="host",
                sdpMid=candidate.sdp_mid,
                sdpMLineIndex=candidate.sdp_m_line_index or 0,
                candidate=candidate.candidate,
            )
            await self._pc.addIceCandidate(ice)

    async def _handle_hangup(self, room, event) -> None:
        """Handle remote hangup."""
        if event.call_id != self._call_id:
            return

        logger.info("Remote hangup: call_id=%s", event.call_id)
        await self._cleanup_call()

    # ── WebRTC Setup ─────────────────────────────────────────────────

    async def _setup_webrtc(self) -> None:
        """Create and configure the WebRTC peer connection."""
        from aiortc import (
            RTCPeerConnection,
            RTCConfiguration,
            RTCIceServer,
            MediaStreamTrack,
        )
        from aiortc.contrib.media import MediaBlackhole, MediaRecorder

        # STUN server for NAT traversal
        config = RTCConfiguration(
            iceServers=[
                RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            ]
        )

        self._pc = RTCPeerConnection(configuration=config)

        # Track connection state
        @self._pc.on("connectionstatechange")
        async def on_connectionstatechange():
            state = self._pc.connectionState
            logger.info("WebRTC connection state: %s", state)
            if state in ("failed", "disconnected", "closed"):
                await self._cleanup_call()

        # Track ICE connection state
        @self._pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            state = self._pc.iceConnectionState
            logger.info("ICE connection state: %s", state)

        # Handle incoming audio track
        @self._pc.on("track")
        async def on_track(track):
            logger.info("Received track: %s", track.kind)
            if track.kind == "audio":
                asyncio.create_task(self._receive_audio_loop(track))

        # Send ICE candidates to Matrix
        @self._pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if not candidate or not self._call_id:
                return

            # Build candidate dict
            cand = {
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
                "candidate": candidate.candidate,
            }

            content = {
                "call_id": self._call_id,
                "party_id": self._party_id,
                "version": "1",
                "candidates": [cand],
            }
            await self._client.send_message(
                self.room_id,
                {
                    "msgtype": "m.call.candidates",
                    "body": "Candidates",
                    **content,
                },
            )

        # Add audio track for sending TTS
        from aiortc import AudioStreamTrack
        self._audio_track = _MatrixAudioTrack(self)
        self._pc.addTrack(self._audio_track)

        # Create data channel (some clients expect it)
        self._dc = self._pc.createDataChannel("matrix-voip")

    # ── Audio Processing ─────────────────────────────────────────────

    async def _receive_audio_loop(self, track) -> None:
        """Receive Opus audio from WebRTC, decode to 24kHz PCM16, queue for bridge."""
        import opuslib

        decoder = opuslib.Decoder(48000, 2)  # WebRTC: 48kHz stereo Opus

        frame_size = 960  # 20ms at 48kHz
        buffer = bytearray()

        while self._running and self._in_call:
            try:
                frame = await track.recv()
                # frame is an av.audio.frame.AudioFrame
                # Convert to bytes (48kHz stereo PCM16)
                pcm_48k = frame.to_ndarray().tobytes()

                # Decode if it's Opus (aiortc may give raw PCM or Opus)
                # For simplicity, assume raw PCM from aiortc
                # Downsample 48kHz stereo → 24kHz mono
                pcm_24k = self._resample_48k_stereo_to_24k_mono(pcm_48k)

                try:
                    self.audio_queue.put_nowait(pcm_24k)
                except asyncio.QueueFull:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Audio receive error: %s", e)
                await asyncio.sleep(0.01)

    async def _send_audio_loop(self) -> None:
        """Read PCM24 from TTS queue, upsample to 48kHz stereo, send via WebRTC track."""
        while self._running and self._in_call:
            try:
                pcm_24k = await asyncio.wait_for(self.tts_queue.get(), timeout=0.5)

                # Upsample 24kHz mono → 48kHz stereo
                pcm_48k = self._resample_24k_mono_to_48k_stereo(pcm_24k)

                # Queue for the audio track to send
                self._audio_track.add_frame(pcm_48k)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Audio send error: %s", e)
                await asyncio.sleep(0.1)

    # ── Call Control ─────────────────────────────────────────────────

    async def _hangup(self) -> None:
        """Send hangup event and clean up."""
        if self._call_id:
            await self._send_hangup_event(self._call_id, self._opponent_party_id or "")
        await self._cleanup_call()

    async def _send_hangup_event(self, call_id: str, party_id: str) -> None:
        """Send m.call.hangup to the room."""
        if not self._client:
            return

        content = {
            "call_id": call_id,
            "party_id": self._party_id,
            "version": "1",
        }
        try:
            await self._client.send_message(
                self.room_id,
                {
                    "msgtype": "m.call.hangup",
                    "body": "Hangup",
                    **content,
                },
            )
        except Exception as e:
            logger.warning("Failed to send hangup: %s", e)

    async def _cleanup_call(self) -> None:
        """Clean up call state."""
        self._in_call = False
        self._call_id = None
        self._opponent_party_id = None

        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
            self._pc = None

        self._dc = None
        await self.set_state("idle")
        logger.info("Call cleaned up")

    # ── Resampling Helpers ────────────────────────────────────────────

    @staticmethod
    def _resample_48k_stereo_to_24k_mono(pcm: bytes) -> bytes:
        """Downsample 48kHz stereo PCM16 → 24kHz mono PCM16.

        Average stereo channels, then take every 2nd sample (48k / 24k = 2).
        """
        import struct

        num_samples = len(pcm) // 4  # 4 bytes per stereo sample
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
        """Upsample 24kHz mono PCM16 → 48kHz stereo PCM16.

        Repeat each sample 2×, duplicate to stereo.
        """
        import struct

        num_samples = len(pcm) // 2
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples}h", pcm)

        stereo_48k = []
        for s in samples:
            for _ in range(2):
                stereo_48k.append(s)  # left
                stereo_48k.append(s)  # right

        return struct.pack(f"<{len(stereo_48k)}h", *stereo_48k)


# ── Audio Track for Sending ──────────────────────────────────────────────


class _MatrixAudioTrack:
    """Simple audio track that feeds PCM frames to WebRTC.

    aiortc expects an AudioStreamTrack with a recv() coroutine.
    This wraps a queue of PCM frames.
    """

    kind = "audio"

    def __init__(self, adapter: MatrixVCAdapter):
        self._adapter = adapter
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._sample_rate = 48000
        self._channels = 2

    def add_frame(self, pcm: bytes) -> None:
        """Add a PCM frame to the send queue."""
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            pass

    async def recv(self):
        """Called by aiortc to get the next audio frame."""
        import av

        try:
            pcm = await asyncio.wait_for(self._queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            # Return silence
            pcm = b"\x00" * 1920  # 20ms of 48kHz stereo PCM16

        # Create an av AudioFrame
        frame = av.AudioFrame.from_ndarray(
            np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2),
            format="s16",
            layout="stereo",
        )
        frame.sample_rate = self._sample_rate
        frame.pts = None  # Let aiortc set timestamps
        return frame


import numpy as np
