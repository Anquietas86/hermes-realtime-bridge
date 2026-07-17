"""Quick test for Matrix VC adapter — syncs with homeserver and verifies connectivity."""
import asyncio
import json
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from hermes_realtime.adapters.matrix_vc import MatrixVCAdapter, MatrixVCConfig


async def main():
    # Get token from env
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    if not token:
        # Try .env
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("MATRIX_ACCESS_TOKEN="):
                        token = line.strip().split("=", 1)[1]
                        break
    if not token:
        logger.error("No MATRIX_ACCESS_TOKEN found")
        return 1

    # First, find available rooms
    import nio
    client = nio.AsyncClient(
        homeserver="https://matrix.hagger.au",
        user="@jarvis:hagger.au",
    )
    client.access_token = token

    logger.info("Syncing to discover rooms...")
    resp = await client.sync(timeout=30000)
    if isinstance(resp, nio.SyncError):
        logger.error("Sync failed: %s", resp.message)
        return 1

    rooms = list(resp.rooms.join.keys())
    logger.info("Joined rooms: %s", rooms)

    # Find the Home room
    home_room = None
    for room_id in rooms:
        room = resp.rooms.join[room_id]
        name = getattr(room, 'name', None) or getattr(room, 'canonical_alias', None) or room_id
        logger.info("  Room: %s — %s", name, room_id)
        if "home" in str(name).lower() or "Home" in str(name):
            home_room = room_id

    if not home_room and rooms:
        home_room = rooms[0]  # fallback to first room

    if not home_room:
        logger.error("No rooms found")
        return 1

    logger.info("Using room: %s", home_room)

    # Test adapter
    config = MatrixVCConfig(
        homeserver="https://matrix.hagger.au",
        access_token=token,
        user_id="@jarvis:hagger.au",
        room_id=home_room,
        auto_answer=True,
    )

    adapter = MatrixVCAdapter(config)

    logger.info("Starting adapter...")
    await adapter.start()

    # Let it sync for a bit
    logger.info("Adapter running — waiting 10s for sync...")
    await asyncio.sleep(10)

    # Check state
    logger.info("Adapter running: %s", adapter._running)
    logger.info("Client connected: %s", adapter._client is not None)

    # Stop
    await adapter.stop()
    await client.close()

    logger.info("✅ Matrix VC adapter test complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
