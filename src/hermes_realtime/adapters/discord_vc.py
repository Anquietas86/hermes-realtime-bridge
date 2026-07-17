"""Discord Voice Channel audio adapter for Hermes Realtime Bridge.

Connects to a Discord voice channel, decodes incoming Opus audio to PCM16,
and encodes outgoing PCM16 to Opus for playback.

Uses the same proven decryption approach as the gateway's VoiceReceiver.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional

import discord
from discord.ext import commands

from hermes_realtime.core import AudioAdapter

logger = logging.getLogger(__name__)


@dataclass
class DiscordVCConfig:
    """Configuration for the Discord VC adapter."""

    token: str = ""
    guild_id: int = 0
    channel_id: int = 0
    sample_rate: int = 24000  # OpenAI Realtime API GA uses 24kHz


class DiscordVCAdapter(AudioAdapter):
    """Connects to a Discord VC, streaming audio via Opus ↔ PCM16.

    Uses the gateway's proven VoiceReceiver approach:
    - conn.add_socket_listener() for packet capture
    - Dynamic RTP header parsing (CSRC, extension, padding)
    - NaCl Aead decryption with correct nonce format
    - Opus decode (48kHz stereo) → resample to 24kHz mono
    """

    name = "discord-vc"

    def __init__(self, config: DiscordVCConfig):
        self.config = config
        self.token = config.token or os.environ.get("DISCORD_BOT_TOKEN", "")

        # Discord client
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)

        # Voice state
        self.voice_client: Optional[discord.VoiceClient] = None
        self._connected = asyncio.Event()

        # Audio queues
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.tts_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

        # Opus decoder
        self._decoder = None
        self._encoder = None

        # State
        self._running = False
        self._bot_task: Optional[asyncio.Task] = None
        self._audio_send_task: Optional[asyncio.Task] = None

        # Packet tracking (mirrors VoiceReceiver)
        self._secret_key: Optional[bytes] = None
        self._bot_ssrc: int = 0
        self._packet_debug_count = 0

    # ── AudioAdapter Interface ────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Discord and join the voice channel."""
        self._running = True

        async def connect_voice():
            """Wait for guild to be available, then connect to voice."""
            await self.bot.wait_until_ready()
            logger.info("Discord bot logged in as %s", self.bot.user)

            for attempt in range(10):
                guild = self.bot.get_guild(self.config.guild_id)
                if guild:
                    break
                logger.debug("Waiting for guild %s (attempt %d)...", self.config.guild_id, attempt + 1)
                await asyncio.sleep(1)
            else:
                logger.error("Guild %s not found after 10 attempts", self.config.guild_id)
                return

            channel = guild.get_channel(self.config.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                logger.error("Channel %s is not a voice channel", self.config.channel_id)
                return

            try:
                self.voice_client = await channel.connect()
                logger.info("Joined voice channel: %s", channel.name)

                # Set up Opus codec
                import opuslib
                self._decoder = opuslib.Decoder(48000, 2)  # Discord: 48kHz stereo
                self._encoder = opuslib.Encoder(48000, 2, "voip")

                # Register audio receive callback (gateway's proven approach)
                self._register_audio_callback()

                # Start audio send loop
                self._audio_send_task = asyncio.create_task(self._send_audio_loop())

                self._connected.set()

            except Exception as e:
                logger.error("Failed to join voice channel: %s", e)

        self._bot_task = asyncio.create_task(self._start_bot())
        asyncio.create_task(connect_voice())

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for Discord voice connection")
            await self.stop()

    def _register_audio_callback(self) -> None:
        """Register packet listener using gateway's proven approach.

        Uses conn.add_socket_listener() (same as VoiceReceiver) and
        does proper RTP header parsing with dynamic size calculation.
        """
        import nacl.secret

        conn = self.voice_client._connection
        self._secret_key = bytes(conn.secret_key)
        self._bot_ssrc = conn.ssrc

        # Use a thread-safe queue for cross-thread audio transfer
        import queue
        self._raw_queue: queue.Queue[bytes] = queue.Queue(maxsize=100)

        def on_packet(data: bytes):
            """Called by SocketReader thread with raw UDP data.

            Mirrors VoiceReceiver._on_packet exactly.
            """
            if len(data) < 16:
                return

            # RTP version check: top 2 bits must be 10 (version 2).
            # Payload type (byte 1 lower 7 bits) = 0x78 (120) for voice.
            if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
                return

            first_byte = data[0]
            _, _, seq, timestamp, ssrc = struct.unpack_from(">BBHII", data, 0)

            # Skip bot's own audio
            if ssrc == self._bot_ssrc:
                return

            # Calculate dynamic RTP header size (RFC 9335 / rtpsize mode)
            cc = first_byte & 0x0F  # CSRC count
            has_extension = bool(first_byte & 0x10)  # extension bit
            has_padding = bool(first_byte & 0x20)  # padding bit
            header_size = 12 + (4 * cc) + (4 if has_extension else 0)

            if len(data) < header_size + 4:  # need at least header + nonce
                return

            # Read extension length from preamble (for skipping after decrypt)
            ext_data_len = 0
            if has_extension:
                ext_preamble_offset = 12 + (4 * cc)
                ext_words = struct.unpack_from(">H", data, ext_preamble_offset + 2)[0]
                ext_data_len = ext_words * 4

            header = bytes(data[:header_size])
            payload_with_nonce = data[header_size:]

            # --- NaCl transport decrypt (aead_xchacha20_poly1305_rtpsize) ---
            if len(payload_with_nonce) < 4:
                return
            nonce = bytearray(24)
            nonce[:4] = payload_with_nonce[-4:]
            encrypted = bytes(payload_with_nonce[:-4])

            try:
                box = nacl.secret.Aead(self._secret_key)
                decrypted = box.decrypt(encrypted, header, bytes(nonce))
            except Exception:
                return

            # Skip encrypted extension data to get the actual opus payload
            if ext_data_len and len(decrypted) > ext_data_len:
                decrypted = decrypted[ext_data_len:]

            # --- Strip RTP padding (RFC 3550 §5.1) ---
            if has_padding and len(decrypted) > 0:
                pad_len = decrypted[-1]
                if 0 < pad_len <= len(decrypted):
                    decrypted = decrypted[:-pad_len]

            if len(decrypted) < 1:
                return

            # Decode Opus → PCM (48kHz stereo)
            try:
                pcm_48k_stereo = self._decoder.decode(decrypted, 960 * 2)
            except Exception:
                return

            # Resample 48kHz stereo → 24kHz mono
            pcm_24k = self._resample_48k_stereo_to_24k_mono(pcm_48k_stereo)

            # Queue for the bridge (thread-safe)
            try:
                self._raw_queue.put_nowait(pcm_24k)
            except queue.Full:
                pass

        # Register with VoiceConnectionState (same as gateway's VoiceReceiver)
        conn.add_socket_listener(on_packet)
        logger.info("Registered audio receive callback (mode=%s, bot_ssrc=%d)", conn.mode, self._bot_ssrc)

        # Start a task to drain the thread-safe queue into the asyncio queue
        self._drain_task = asyncio.create_task(self._drain_raw_queue())

    async def _start_bot(self) -> None:
        """Run the Discord bot (blocks until stopped)."""
        try:
            await self.bot.start(self.token)
        except discord.LoginFailure:
            logger.error("Invalid Discord bot token")
        except Exception as e:
            logger.error("Discord bot error: %s", e)

    async def stop(self) -> None:
        """Leave voice channel and disconnect."""
        self._running = False

        if self._audio_send_task:
            self._audio_send_task.cancel()

        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None

        if self.bot and not self.bot.is_closed():
            await self.bot.close()

        logger.info("Discord VC adapter stopped.")

    async def read_audio(self) -> Optional[bytes]:
        """Read decoded PCM16 audio from the voice channel."""
        if not self._running:
            return None
        try:
            return await asyncio.wait_for(self.audio_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return None

    async def write_audio(self, pcm: bytes) -> None:
        """Queue PCM16 audio for encoding and sending to voice channel."""
        if not self._running:
            return
        try:
            await self.tts_queue.put(pcm)
        except asyncio.QueueFull:
            logger.warning("TTS queue full, dropping audio chunk")

    async def set_state(self, state: str) -> None:
        """Log state change (no hardware LEDs on Discord)."""
        logger.debug("Discord VC state: %s", state)

    async def _drain_raw_queue(self) -> None:
        """Drain the thread-safe raw queue into the asyncio audio queue."""
        import queue
        while self._running:
            try:
                pcm = self._raw_queue.get(timeout=0.1)
                await self.audio_queue.put(pcm)
            except queue.Empty:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                break

    # ── Audio Send Loop ──────────────────────────────────────────────

    async def _send_audio_loop(self) -> None:
        """Read PCM16 from TTS queue, encode to Opus, send to Discord."""
        while self._running and self.voice_client and self.voice_client.is_connected():
            try:
                pcm_24k = await asyncio.wait_for(self.tts_queue.get(), timeout=0.5)

                # Upsample 24kHz mono → 48kHz stereo
                pcm_48k_stereo = self._resample_24k_mono_to_48k_stereo(pcm_24k)

                # Encode to Opus (20ms frame = 960 samples at 48kHz)
                opus_packet = self._encoder.encode(pcm_48k_stereo, 960 * 2)

                # Send to Discord
                if self.voice_client:
                    self.voice_client.send_audio_packet(opus_packet, encode=False)

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
        num_samples = len(pcm) // 4  # 4 bytes per stereo sample (2 × int16)
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples * 2}h", pcm)

        # Average stereo → mono, then decimate 2:1 (48k → 24k)
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
        num_samples = len(pcm) // 2  # 2 bytes per mono sample
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples}h", pcm)

        # Repeat each sample 2× (24k → 48k), duplicate to stereo
        stereo_48k = []
        for s in samples:
            for _ in range(2):
                stereo_48k.append(s)  # left
                stereo_48k.append(s)  # right

        return struct.pack(f"<{len(stereo_48k)}h", *stereo_48k)
