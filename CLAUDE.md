# Hermes Realtime Bridge

> **Status:** In development — core bridge built, adapters + tool bridge in progress
> **Started:** 2026-07-13
> **Goal:** Sub-second voice interface for Hermes via OpenAI Realtime API, with swappable audio adapters (Voice PE hardware + Discord VC)

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

## Current State

### ✅ Done
- `pyproject.toml` — project config, dependencies, entrypoint
- `src/hermes_realtime/core.py` — full core bridge:
  - `AudioAdapter` ABC (start/stop/read_audio/write_audio/set_state)
  - `ToolBridge` ABC (get_tools/execute)
  - `RealtimeConfig` dataclass (model, voice, VAD, instructions)
  - `RealtimeBridge` class:
    - WebSocket connect + session config
    - Audio loop (read from adapter → 20ms chunks → Realtime API)
    - Response loop (audio deltas → adapter, function calls → tool bridge)
    - Speech start/stop handling (cancel in-progress, state updates)
    - Function call execution + result routing

### 🚧 In Progress
- `src/hermes_realtime/tools.py` — Hermes tool bridge (next file to create)
- `src/hermes_realtime/adapters/voice_pe.py` — Voice PE adapter
- `src/hermes_realtime/adapters/discord_vc.py` — Discord VC adapter

### ⬜ TODO
- `src/hermes_realtime/cli.py` — CLI entrypoint
- `config.yaml` — bridge configuration
- `README.md` — docs
- Git init + GitHub push

## Key Design Decisions

1. **One bridge, many adapters** — Core is transport-agnostic. Adapters handle the audio plumbing.
2. **OpenAI Realtime API** — WebSocket protocol, not REST. Handles STT+LLM+TTS in one streaming pass.
3. **Function calling preserved** — Realtime API can call tools. Bridge routes those to Hermes.
4. **20ms audio chunks** — Standard for low-latency streaming. 16kHz, 16-bit mono PCM.
5. **Server VAD** — Turn detection handled by Realtime API, not client-side.
6. **Adapter state machine** — `idle → listening → thinking → speaking` for LED/UI feedback.

## File Structure

```
hermes-realtime-bridge/
├── pyproject.toml
├── CLAUDE.md                    ← this file
├── README.md                    ← TODO
├── config.example.yaml          ← TODO
├── src/
│   └── hermes_realtime/
│       ├── __init__.py
│       ├── core.py              ← ✅ RealtimeBridge, AudioAdapter, ToolBridge
│       ├── tools.py             ← 🚧 HermesToolBridge
│       ├── cli.py               ← ⬜ CLI entrypoint
│       └── adapters/
│           ├── __init__.py
│           ├── voice_pe.py      ← ⬜ Voice PE (ESPHome WebSocket)
│           └── discord_vc.py    ← ⬜ Discord VC (Opus ↔ PCM)
├── scripts/
│   └── test_voice_pe.py         ← ⬜
└── tests/
    └── test_core.py             ← ⬜
```

## How to Resume

1. **Load this file** — it's the project memory
2. **Check `todo` list** — what's in_progress vs pending
3. **Read `src/hermes_realtime/core.py`** — understand the bridge architecture
4. **Next file to create:** `src/hermes_realtime/tools.py` (HermesToolBridge)
5. **Then:** Voice PE adapter → Discord adapter → CLI → config → docs → git

## Dependencies

- `websockets` — Realtime API WebSocket client
- `pydantic` — config validation
- `numpy` — audio buffer handling
- `aiohttp` — Voice PE adapter (ESPHome WebSocket)
- `discord.py[voice]` + `PyNaCl` — Discord VC adapter

## Environment

- **Project root:** `/home/hermes/projects/hermes-realtime-bridge/`
- **Python:** 3.11+ (use `uv` for venv management)
- **API key:** `OPENAI_API_KEY` from `~/.hermes/.env`
- **Hermes tools:** Access via subprocess or direct import from `~/.hermes/hermes-agent/`

## Notes

- The Voice PE hardware ($69 USD) hasn't been ordered yet — adapter can be built against the ESPHome spec
- Discord adapter is the heavier lift (Opus codec, UDP session management)
- Realtime API costs ~$0.06/min input, ~$0.24/min output — casual use ~$5-15/month
- Bridge should run as a systemd user service alongside the Hermes gateway
