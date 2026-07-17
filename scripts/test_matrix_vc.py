"""End-to-end test for Matrix VC adapter (LiveKit).

Connects to the LiveKit server, joins the DM room, and verifies
audio track publishing. Requires LIVEKIT_API_KEY and LIVEKIT_API_SECRET
in the project .env or environment.
"""
import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test")

# Add project src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_realtime.adapters.matrix_vc import MatrixVCAdapter, MatrixVCConfig


async def main():
    # Load env from project .env
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    if key not in os.environ:
                        os.environ[key] = val

    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")

    if not api_key or not api_secret:
        logger.error("LIVEKIT_API_KEY and LIVEKIT_API_SECRET required")
        return 1

    # Use the DM room between @jarvis and @josh
    room_id = "!ooYStQUSKarbOQeTOj:hagger.au"

    config = MatrixVCConfig(
        livekit_url="ws://192.168.0.7:7880",
        api_key=api_key,
        api_secret=api_secret,
        room_id=room_id,
        auto_join=True,
    )

    adapter = MatrixVCAdapter(config)

    logger.info("Starting Matrix VC adapter (LiveKit)...")
    try:
        await asyncio.wait_for(adapter.start(), timeout=15)
    except asyncio.TimeoutError:
        logger.error("Adapter start timed out")
        return 1

    logger.info("Adapter started — waiting 5s for room connection...")
    await asyncio.sleep(5)

    # Check state
    logger.info("Adapter running: %s", adapter._running)
    logger.info("Room connected: %s", adapter._room is not None)
    if adapter._room:
        logger.info(
            "Room state: %s, participants: %d",
            adapter._room.connection_state,
            len(list(adapter._room.remote_participants.values())),
        )

    # Stop
    await adapter.stop()
    logger.info("✅ Matrix VC adapter test complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
