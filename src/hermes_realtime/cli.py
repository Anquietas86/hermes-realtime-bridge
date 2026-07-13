"""CLI entrypoint for Hermes Realtime Bridge.

Usage:
    hermes-realtime --adapter voice-pe --config config.yaml
    hermes-realtime --adapter discord-vc --token $DISCORD_BOT_TOKEN --channel 1518510627524575292
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from .core import RealtimeBridge, RealtimeConfig
from .tools import HermesToolBridge

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hermes Realtime Bridge — sub-second voice via OpenAI Realtime API",
    )
    parser.add_argument(
        "--adapter",
        choices=["voice-pe", "discord-vc"],
        required=True,
        help="Audio adapter to use",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="OpenAI API key (or set OPENAI_API_KEY env var)",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-realtime-preview",
        help="Realtime model to use",
    )
    parser.add_argument(
        "--voice",
        default="alloy",
        help="TTS voice (alloy, echo, shimmer, etc.)",
    )
    parser.add_argument(
        "--instructions",
        default=None,
        help="System instructions for the model",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="Disable Hermes tool bridge",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    # Voice PE adapter args
    voice_pe = parser.add_argument_group("Voice PE adapter")
    voice_pe.add_argument("--pe-host", default="voice-pe.local", help="ESPHome device hostname")
    voice_pe.add_argument("--pe-port", type=int, default=6053, help="ESPHome WebSocket port")
    voice_pe.add_argument("--pe-password", default=None, help="ESPHome API password")

    # Discord VC adapter args
    discord = parser.add_argument_group("Discord VC adapter")
    discord.add_argument("--discord-token", default=None, help="Discord bot token")
    discord.add_argument("--discord-guild", type=int, default=None, help="Discord guild ID")
    discord.add_argument("--discord-channel", type=int, default=None, help="Discord voice channel ID")

    return parser.parse_args()


def load_config(config_path: Path | None) -> dict:
    """Load YAML config, merge with defaults."""
    defaults = {
        "model": "gpt-4o-realtime-preview",
        "voice": "alloy",
        "temperature": 0.8,
        "instructions": None,
        "tools": True,
        "voice_pe": {
            "host": "voice-pe.local",
            "port": 6053,
            "password": None,
        },
        "discord": {
            "token": None,
            "guild_id": None,
            "channel_id": None,
        },
    }

    if config_path and config_path.exists():
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
        # Shallow merge
        for key, value in user_config.items():
            if isinstance(value, dict) and key in defaults and isinstance(defaults[key], dict):
                defaults[key].update(value)
            else:
                defaults[key] = value

    return defaults


async def main_async() -> None:
    args = parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Config
    config = load_config(args.config)

    # API key
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("No API key. Set OPENAI_API_KEY env var or pass --api-key.")
        sys.exit(1)

    # Build adapter
    if args.adapter == "voice-pe":
        from .adapters.voice_pe import VoicePEAdapter, VoicePEConfig

        pe_config = VoicePEConfig(
            host=args.pe_host or config["voice_pe"]["host"],
            port=args.pe_port or config["voice_pe"]["port"],
            api_password=args.pe_password or config["voice_pe"]["password"],
        )
        adapter = VoicePEAdapter(pe_config)

    elif args.adapter == "discord-vc":
        from .adapters.discord_vc import DiscordVCAdapter, DiscordVCConfig

        token = args.discord_token or config["discord"]["token"] or os.environ.get("DISCORD_BOT_TOKEN")
        guild_id = args.discord_guild or config["discord"]["guild_id"]
        channel_id = args.discord_channel or config["discord"]["channel_id"]

        if not token:
            logger.error("No Discord token. Set DISCORD_BOT_TOKEN or pass --discord-token.")
            sys.exit(1)
        if not channel_id:
            logger.error("No Discord voice channel ID. Pass --discord-channel.")
            sys.exit(1)

        dc_config = DiscordVCConfig(
            token=token,
            guild_id=guild_id,
            channel_id=channel_id,
        )
        adapter = DiscordVCAdapter(dc_config)

    else:
        logger.error("Unknown adapter: %s", args.adapter)
        sys.exit(1)

    # Build tool bridge
    tool_bridge = None
    if not args.no_tools and config.get("tools", True):
        tool_bridge = HermesToolBridge()

    # Build realtime config
    instructions = args.instructions or config.get("instructions")
    if not instructions:
        instructions = (
            "You are Jarvis, a capable AI assistant. You have access to tools "
            "that let you control smart home devices, query infrastructure, and "
            "access persistent memory. Be concise and helpful. Use tools when "
            "appropriate."
        )

    rt_config = RealtimeConfig(
        model=args.model or config["model"],
        voice=args.voice or config["voice"],
        instructions=instructions,
        temperature=config.get("temperature", 0.8),
    )

    # Build and run bridge
    bridge = RealtimeBridge(
        api_key=api_key,
        adapter=adapter,
        tool_bridge=tool_bridge,
        config=rt_config,
    )

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()

    def shutdown():
        logger.info("Shutting down...")
        asyncio.create_task(bridge.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    logger.info("Starting Hermes Realtime Bridge (adapter=%s, model=%s)", args.adapter, rt_config.model)
    await bridge.run()
    logger.info("Bridge stopped.")


def main() -> None:
    """Entry point."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
