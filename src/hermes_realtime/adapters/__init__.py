"""Audio adapters for Hermes Realtime Bridge."""

from .voice_pe import VoicePEAdapter, VoicePEConfig
from .discord_vc import DiscordVCAdapter, DiscordVCConfig

__all__ = ["VoicePEAdapter", "VoicePEConfig", "DiscordVCAdapter", "DiscordVCConfig"]
