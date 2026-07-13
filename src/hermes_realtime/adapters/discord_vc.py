"""Discord Voice Channel audio adapter for Hermes Realtime Bridge.

Connects to a Discord voice channel, decodes incoming Opus audio to PCM16,
and encodes outgoing PCM16 to Opus for playback.

Uses discord.py[voice] for Discord integration and opuslib for Opus codec.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

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
    sample_rate: int = 16000  # Target: 16kHz mono for Realtime API


class DiscordVCAdapter(AudioAdapter):
    """Connects to a Discord VC, streaming audio via Opus ↔ PCM16."""

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

        # State
        self._running = False
        self._bot_task: Optional[asyncio.Task] = None
        self._audio_send_task: Optional[asyncio.Task] = None
        self._audio_recv_task: Optional[asyncio.Task] = None

    # ── AudioAdapter Interface ────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Discord and join the voice channel."""
        self._running = True

        @self.bot.event
        async def on_ready():
            logger.info("Discord bot logged in as %s", self.bot.user)

            guild = self.bot.get_guild(self.config.guild_id)
            if not guild:
                logger.error("Guild %s not found", self.config.guild_id)
                return

            channel = guild.get_channel(self.config.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                logger.error("Channel %s is not a voice channel", self.config.channel_id)
                return

            try:
                self.voice_client = await channel.connect()
                logger.info("Joined voice channel: %s", channel.name)
                self._connected.set()

                # Start audio processing tasks
                self._audio_recv_task = asyncio.create_task(self._receive_audio_loop())
                self._audio_send_task = asyncio.create_task(self._send_audio_loop())

            except Exception as e:
                logger.error("Failed to join voice channel: %s", e)

        # Start the bot in background
        self._bot_task = asyncio.create_task(self._start_bot())

        # Wait for voice connection (with timeout)
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for Discord voice connection")
            await self.stop()

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

        # Cancel tasks
        for task in [self._audio_recv_task, self._audio_send_task]:
            if task:
                task.cancel()

        # Disconnect voice
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            self.voice_client = None

        # Close bot
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

    # ── Audio Processing ──────────────────────────────────────────────

    async def _receive_audio_loop(self) -> None:
        """Receive Opus packets from Discord, decode to PCM16, queue for bridge.

        Discord sends 48kHz stereo Opus. We decode and downsample to 16kHz mono
        PCM16 for the Realtime API.
        """
        import opuslib

        decoder = opuslib.Decoder(48000, 2)  # Discord: 48kHz stereo

        while self._running and self.voice_client and self.voice_client.is_connected():
            try:
                # VoiceClient.read() returns raw Opus packets from the UDP socket
                # This is a blocking call — use a thread to avoid blocking the event loop
                opus_data = await asyncio.to_thread(
                    self.voice_client.read
                )

                if not opus_data:
                    await asyncio.sleep(0.01)
                    continue

                # Decode Opus → PCM (48kHz stereo, 16-bit)
                pcm_48k_stereo = decoder.decode(opus_data, 960 * 2 * 2)  # 20ms frame

                # Downsample to 16kHz mono
                pcm_16k_mono = self._resample_48k_stereo_to_16k_mono(pcm_48k_stereo)

                # Queue for the bridge
                try:
                    self.audio_queue.put_nowait(pcm_16k_mono)
                except asyncio.QueueFull:
                    pass  # Drop if bridge isn't consuming fast enough

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Audio receive error: %s", e)
                await asyncio.sleep(0.1)

    async def _send_audio_loop(self) -> None:
        """Read PCM16 from TTS queue, encode to Opus, send to Discord.

        Discord expects 48kHz stereo Opus. We upsample from 16kHz mono.
        """
        import opuslib

        encoder = opuslib.Encoder(48000, 2, "voip")  # Discord: 48kHz stereo, VOIP mode

        while self._running and self.voice_client and self.voice_client.is_connected():
            try:
                pcm_16k = await asyncio.wait_for(self.tts_queue.get(), timeout=0.5)

                # Upsample 16kHz mono → 48kHz stereo
                pcm_48k_stereo = self._resample_16k_mono_to_48k_stereo(pcm_16k)

                # Encode to Opus
                opus_packet = encoder.encode(pcm_48k_stereo, 960 * 2)  # 20ms frame

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
    def _resample_48k_stereo_to_16k_mono(pcm: bytes) -> bytes:
        """Downsample 48kHz stereo PCM16 → 16kHz mono PCM16.

        Simple approach: average stereo channels, then take every 3rd sample
        (48k / 16k = 3). For production, use scipy.signal.resample or soxr.
        """
        import struct

        # Unpack 16-bit stereo samples
        num_samples = len(pcm) // 4  # 4 bytes per stereo sample (2 × int16)
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples * 2}h", pcm)

        # Average stereo → mono, then decimate 3:1
        mono_16k = []
        for i in range(0, num_samples, 3):
            left = samples[i * 2]
            right = samples[i * 2 + 1] if i * 2 + 1 < len(samples) else left
            mono = (left + right) // 2
            mono_16k.append(mono)

        return struct.pack(f"<{len(mono_16k)}h", *mono_16k)

    @staticmethod
    def _resample_16k_mono_to_48k_stereo(pcm: bytes) -> bytes:
        """Upsample 16kHz mono PCM16 → 48kHz stereo PCM16.

        Simple approach: repeat each sample 3×, duplicate to stereo.
        For production, use proper interpolation (scipy, soxr).
        """
        import struct

        num_samples = len(pcm) // 2  # 2 bytes per mono sample
        if num_samples == 0:
            return b""

        samples = struct.unpack(f"<{num_samples}h", pcm)

        # Repeat each sample 3× (16k → 48k), duplicate to stereo
        stereo_48k = []
        for s in samples:
            for _ in range(3):
                stereo_48k.append(s)  # left
                stereo_48k.append(s)  # right

        return struct.pack(f"<{len(stereo_48k)}h", *stereo_48k)
