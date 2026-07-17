# Hermes Realtime Bridge

Sub-second voice interface for [Hermes Agent](https://github.com/NousResearch/hermes-agent) via OpenAI's Realtime API, with swappable audio adapters.

**Status:** Core bridge tested and working with OpenAI Realtime API GA (`gpt-realtime-2.1`). Three adapters built: Voice PE hardware, Discord VC, Matrix VC (LiveKit).

## Architecture

```
Audio Source (Voice PE / Discord VC / Matrix VC)
        │
        ▼
  AudioAdapter (per-transport: PCM in/out, 24kHz mono)
        │
        ▼
  RealtimeBridge (core: WebSocket ↔ OpenAI Realtime API)
        │
        ├── Audio streaming (PCM16, 24kHz mono, 20ms chunks)
        ├── Function call routing → ToolBridge
        └── Session management (VAD, turn detection)
        │
        ▼
  ToolBridge → Hermes tools (HA, terminal, memory, etc.)
```

One bridge, many adapters. The core is transport-agnostic — adapters handle the audio plumbing. The Realtime API is a faster voice frontend; Hermes remains the brain.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/Anquietas86/hermes-realtime-bridge.git
cd hermes-realtime-bridge
uv venv
source .venv/bin/activate
uv pip install -e ".[all]"

# Set your API key
echo "OPENAI_API_KEY=sk-..." > .env

# Test connectivity
python scripts/test_connectivity.py

# Run with an adapter
hermes-realtime --adapter discord-vc --config config.yaml
```

## Adapters

### Voice PE (ESPHome Hardware)

Connects to a [Home Assistant Voice Preview Edition](https://www.home-assistant.io/voice-pe/) running custom ESPHome firmware that streams raw audio over WebSocket.

- **Hardware:** Voice PE ($69 USD) — ESP32-S3 + XMOS XU316 audio DSP
- **Firmware:** [TristanBrotherton/voicepe-realtime-firmware](https://github.com/TristanBrotherton/voicepe-realtime-firmware)
- **Wake word:** "Jarvis" via `micro_wake_word` on-device (free, offline)
- **Latency:** ~200-500ms

```bash
hermes-realtime --adapter voice-pe --config config.yaml
```

### Discord VC

Connects to a Discord voice channel and bridges Opus audio to/from the Realtime API. Uses the same proven VoiceReceiver decryption approach as the Hermes gateway.

**Important:** The bridge uses the same Discord bot token as the Hermes gateway. Discord only allows one voice connection per bot token per guild. For Discord voice, use the gateway's built-in `/voice join` instead. The Discord VC adapter is for standalone use or when the gateway isn't running.

```bash
export DISCORD_BOT_TOKEN="..."
hermes-realtime --adapter discord-vc --config config.yaml
```

### Matrix VC (LiveKit)

Joins MatrixRTC voice calls via LiveKit. Element Call creates a LiveKit room; this adapter joins as a participant and bridges audio.

- **Backend:** LiveKit server (co-located with Synapse on LXC 209)
- **Room:** Uses the existing DM room between @jarvis and @josh
- **Auth:** LiveKit API key/secret (stored in Bitwarden)

```bash
export LIVEKIT_API_KEY="..."
export LIVEKIT_API_SECRET="..."
hermes-realtime --adapter matrix-vc --config config.yaml
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and customize:

```yaml
# Realtime API
model: gpt-realtime-2.1
voice: marin
tools: true

# Voice PE
voice_pe:
  host: voice-pe.local
  port: 6053

# Discord VC
discord:
  token: null          # or DISCORD_BOT_TOKEN env var
  guild_id: null
  channel_id: null

# Matrix VC (LiveKit)
matrix:
  livekit_url: "ws://192.168.0.7:7880"
  api_key: null        # or LIVEKIT_API_KEY env var
  api_secret: null     # or LIVEKIT_API_SECRET env var
  room_id: "!ooYStQUSKarbOQeTOj:hagger.au"
  auto_join: true
```

## API Details

Uses OpenAI Realtime API GA (`gpt-realtime-2.1`):
- **Sample rate:** 24kHz PCM16 mono
- **VAD:** Semantic VAD (server-side turn detection)
- **Chunk size:** 20ms (480 samples)
- **Voices:** marin, cedar, alloy, ash, ballad, coral, echo, sage, shimmer, verse
- **Pricing:** $32/1M audio input tokens, $64/1M audio output tokens (~$5-15/month casual use)

## Tools

The bridge routes Realtime API function calls to Hermes tools via subprocess:

| Tool | Description |
|------|-------------|
| `ha_control` | Control Home Assistant devices (lights, switches, climate) |
| `ha_query` | Query Home Assistant entity states |
| `infra_query` | Check infrastructure health (NFS, Docker, services) |
| `memory_query` | Search Hermes persistent memory |
| `shell_command` | Run shell commands (with approval) |

## Systemd Service

```bash
# Install as user service
mkdir -p ~/.config/systemd/user
cp hermes-realtime-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-realtime-bridge
```

## Dependencies

- Python 3.11+
- `websockets` — Realtime API WebSocket client
- `numpy` — audio buffer handling
- `pydantic` — config validation
- `pyyaml` — config file parsing
- Voice PE: `aiohttp`
- Discord: `discord.py[voice]`, `PyNaCl`, `opuslib`
- Matrix: `livekit`, `livekit-api`

## License

MIT
