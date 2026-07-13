# Hermes Realtime Bridge

Sub-second voice interface for [Hermes Agent](https://github.com/NousResearch/hermes-agent) via OpenAI's Realtime API, with swappable audio adapters.

## Architecture

```
Audio Source (Voice PE / Discord VC)
        │
        ▼
  AudioAdapter (per-transport: PCM in/out)
        │
        ▼
  RealtimeBridge (core: WebSocket ↔ OpenAI Realtime API)
        │
        ├── Audio streaming (PCM16, 16kHz mono, 20ms chunks)
        ├── Function call routing → ToolBridge
        └── Session management (VAD, turn detection)
        │
        ▼
  ToolBridge → Hermes tools (HA, terminal, memory, etc.)
```

## Features

- **Sub-second latency** — streaming audio via OpenAI Realtime API (not pipeline STT→LLM→TTS)
- **Swappable adapters** — Voice PE hardware and Discord VC, same bridge core
- **Full tool access** — Realtime API function calls routed to Hermes tools (Home Assistant, infrastructure, memory, shell)
- **Server VAD** — turn detection handled by the Realtime API
- **Persistent context** — Realtime session carries conversation, Hermes carries your world

## Quick Start

### Prerequisites

- Python 3.11+
- OpenAI API key (for Realtime API)
- Hermes Agent installed

### Install

```bash
cd ~/projects/hermes-realtime-bridge
uv pip install -e ".[all]" --python /home/hermes/.hermes/hermes-agent/venv/bin/python
```

### Voice PE (Hardware)

```bash
# Set API key
export OPENAI_API_KEY="sk-..."

# Run with Voice PE adapter
hermes-realtime --adapter voice-pe --pe-host voice-pe.local
```

### Discord Voice Channel

```bash
# Set API key + Discord token
export OPENAI_API_KEY="sk-..."
export DISCORD_BOT_TOKEN="..."

# Run with Discord adapter
hermes-realtime --adapter discord-vc --discord-channel 1518510627524575292
```

### Configuration File

```bash
hermes-realtime --config config.yaml --adapter voice-pe
```

See `config.example.yaml` for all options.

## Adapters

### Voice PE (Preview Edition)

Connects to Home Assistant Voice Preview Edition hardware running custom ESPHome firmware. The ESP32-S3 streams 16kHz mono PCM16 audio over WebSocket. XMOS XU316 handles AEC, beamforming, and noise suppression in hardware.

- **Hardware:** $69 USD, one USB-C cable
- **Firmware:** Custom ESPHome component (replaces stock `voice_assistant`)
- **Wake word:** "Jarvis" via `micro_wake_word` (free, offline)

### Discord VC

Connects to a Discord voice channel. Decodes incoming Opus audio to PCM16, encodes outgoing PCM16 to Opus. Uses `discord.py[voice]`.

- **Requires:** Bot with Connect + Speak permissions
- **Channel:** Any voice channel the bot can access

## Tools

The bridge exposes 5 Hermes tools to the Realtime API:

| Tool | Description |
|------|-------------|
| `ha_control` | Control Home Assistant devices (lights, climate, locks, etc.) |
| `ha_query` | Query HA entity state or list entities |
| `infra_query` | Check infrastructure health (NFS, Docker, network, Zabbix) |
| `memory_lookup` | Look up information from persistent memory |
| `run_command` | Run shell commands on the server |

## Cost

- **OpenAI Realtime API:** ~$0.06/min audio input, ~$0.24/min audio output
- **Casual use estimate:** $5-15/month
- **Wake word:** Free, local, offline (Voice PE only)

## Project Structure

```
hermes-realtime-bridge/
├── pyproject.toml
├── CLAUDE.md                    # Project memory for AI assistants
├── README.md                    # This file
├── config.example.yaml
├── src/
│   └── hermes_realtime/
│       ├── __init__.py
│       ├── core.py              # RealtimeBridge, AudioAdapter ABC, ToolBridge ABC
│       ├── tools.py             # HermesToolBridge (function calls → Hermes)
│       ├── cli.py               # CLI entrypoint
│       └── adapters/
│           ├── __init__.py
│           ├── voice_pe.py      # Voice PE (ESPHome WebSocket)
│           └── discord_vc.py    # Discord VC (Opus ↔ PCM)
├── scripts/
└── tests/
```

## License

MIT
