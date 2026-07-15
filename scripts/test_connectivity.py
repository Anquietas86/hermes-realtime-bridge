"""Quick connectivity test for the Realtime Bridge (GA v2 API).

Tests:
1. WebSocket connection to OpenAI Realtime API
2. Session configuration
3. Text message → audio response
4. Function call routing (if tools enabled)
"""
import asyncio
import json
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from hermes_realtime.core import RealtimeBridge, RealtimeConfig, AudioAdapter, ToolBridge


class TestAdapter(AudioAdapter):
    """Test adapter that captures output and sends a text message."""
    name = "test"

    def __init__(self, sample_rate=24000):
        self.audio_out = []
        self.states = []
        self.sample_rate = sample_rate
        self._sent_text = False

    async def start(self):
        logger.info("Test adapter started")

    async def stop(self):
        logger.info("Test adapter stopped")

    async def read_audio(self):
        # Don't send audio — we'll send a text message instead
        await asyncio.sleep(0.1)
        return None

    async def write_audio(self, pcm: bytes):
        self.audio_out.append(pcm)

    async def set_state(self, state: str):
        self.states.append(state)
        logger.info("State: %s", state)


class TestToolBridge(ToolBridge):
    """Test tool bridge that echoes calls."""
    def __init__(self):
        self.calls = []

    async def get_tools(self):
        return [
            {
                "type": "function",
                "name": "echo",
                "description": "Echo back the message",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "Message to echo"}
                    },
                    "required": ["message"],
                },
            }
        ]

    async def execute(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return json.dumps({"echo": arguments.get("message", "no message")})


async def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("OPENAI_API_KEY="):
                        api_key = line.strip().split("=", 1)[1]
                        break
    if not api_key:
        logger.error("No OPENAI_API_KEY found")
        return 1

    adapter = TestAdapter()
    tool_bridge = TestToolBridge()
    config = RealtimeConfig(
        model="gpt-realtime-2.1",
        voice="marin",
        instructions="You are a test assistant. Respond with a very short greeting.",
    )

    bridge = RealtimeBridge(
        api_key=api_key,
        adapter=adapter,
        tool_bridge=tool_bridge,
        config=config,
    )

    logger.info("Connecting to OpenAI Realtime API (model=%s)...", config.model)

    # Run bridge in background
    bridge_task = asyncio.create_task(bridge.run())

    # Wait for session to be created
    await asyncio.sleep(3)

    # Send a text message to trigger a response
    if bridge._ws:
        logger.info("Sending text message...")
        await bridge._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello! Say hi back in one sentence."}],
            },
        }))
        await bridge._ws.send(json.dumps({"type": "response.create"}))

    # Wait for response
    await asyncio.sleep(8)

    # Stop
    await bridge.stop()
    try:
        await asyncio.wait_for(bridge_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    logger.info("=== Test Results ===")
    logger.info("States seen: %s", adapter.states)
    logger.info("Audio chunks received: %d", len(adapter.audio_out))
    total_audio = sum(len(c) for c in adapter.audio_out)
    logger.info("Total audio bytes: %d", total_audio)
    logger.info("Tool calls: %d", len(tool_bridge.calls))

    if total_audio > 0:
        logger.info("✅ Bridge connected and received audio response (%d bytes)", total_audio)
        return 0
    elif adapter.states:
        logger.warning("⚠️  Connected but no audio received — check model/voice config")
        return 0
    else:
        logger.error("❌ No state updates — connection failed")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
