# Hermes Realtime Bridge

> **Status:** Complete — all adapters built and tested, README + systemd service done, pushed to GitHub
> **Started:** 2026-07-13
> **Last updated:** 2026-07-17
> **Goal:** Sub-second voice interface for Hermes via OpenAI Realtime API, with swappable audio adapters (Voice PE hardware, Discord VC, Matrix VC)

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

## Current State

### ✅ Done
- `pyproject.toml` — project config, dependencies, entrypoint
- `src/hermes_realtime/core.py` — full core bridge (GA v2 API):
  - `AudioAdapter` ABC (start/stop/read_audio/write_audio/set_state)
  - `ToolBridge` ABC (get_tools/execute)
  - `RealtimeConfig` dataclass (model, voice, VAD, instructions)
  - `RealtimeBridge` class:
    - WebSocket connect + session config (GA v2 format)
    - Audio loop (read from adapter → 20ms chunks → Realtime API)
    - Response loop (audio deltas → adapter, function calls → tool bridge)
    - Speech start/stop handling (cancel in-progress, state updates)
    - Function call execution + result routing
- `src/hermes_realtime/tools.py` — HermesToolBridge (subprocess mode)
- `src/hermes_realtime/adapters/voice_pe.py` — Voice PE adapter (ESPHome WebSocket)
- `src/hermes_realtime/adapters/discord_vc.py` — Discord VC adapter (rewritten with proven VoiceReceiver approach)
- `src/hermes_realtime/adapters/matrix_vc.py` — Matrix VC adapter (LiveKit, ✅ e2e tested: connects, joins room, publishes audio)
- `src/hermes_realtime/cli.py` — CLI entrypoint (voice-pe, discord-vc, matrix-vc)
- `scripts/test_connectivity.py` — API connectivity test (✅ passing)
- `scripts/test_matrix_vc.py` — Matrix adapter e2e test (✅ passing: LiveKit connect + audio publish)
- `README.md` — full documentation ✅
- `hermes-realtime-bridge.service` — systemd user service file ✅
- `.env` — API key + LiveKit creds stored (600 perms)
- `.env.example` — template
- `config.example.yaml` — config template (all 3 adapters)
- Venv set up with all deps (uv)
- Git repo initialized, pushed to GitHub (Anquietas86/hermes-realtime-bridge)

### ⬜ TODO
- Voice PE hardware order ($69 USD)
- Install systemd service on the host
- End-to-end voice call test (needs a second Matrix client to initiate call)

## Key Design Decisions

1. **One bridge, many adapters** — Core is transport-agnostic. Adapters handle the audio plumbing.
2. **OpenAI Realtime API GA (v2)** — `gpt-realtime-2.1` model, 24kHz PCM16, semantic VAD
3. **Function calling preserved** — Realtime API can call tools. Bridge routes those to Hermes.
4. **20ms audio chunks** — Standard for low-latency streaming. 24kHz, 16-bit mono PCM.
5. **Server VAD** — Turn detection handled by Realtime API (semantic_vad), not client-side.
6. **Adapter state machine** — `idle → listening → thinking → speaking` for LED/UI feedback.

## API Changes from Beta → GA

The GA Realtime API (gpt-realtime-2.1) differs significantly from the beta:
- **Model**: `gpt-realtime-2.1` (not `gpt-4o-realtime-preview`)
- **Session**: requires `"type": "realtime"` in session config
- **Audio format**: nested under `audio.input.format` / `audio.output.format` with `type: "audio/pcm"` and `rate: 24000`
- **VAD**: `semantic_vad` under `audio.input.turn_detection`
- **Voice**: under `audio.output.voice` (marin, cedar recommended)
- **No `OpenAI-Beta` header** needed
- **Event names**: `response.output_audio.delta`, `response.output_audio.done`, etc.
- **Function calls**: detected via `response.done` with `output[].type === "function_call"`
- **No `temperature`** at session level

## File Structure

```
hermes-realtime-bridge/
├── pyproject.toml
├── CLAUDE.md                    ← this file
├── README.md                    ← ✅ full docs
├── config.example.yaml          ← ✅
├── hermes-realtime-bridge.service ← ✅ systemd user service
├── .env                         ← ✅ (API key + LiveKit creds, 600 perms)
├── .env.example                 ← ✅
├── src/
│   └── hermes_realtime/
│       ├── __init__.py
│       ├── core.py              ← ✅ RealtimeBridge, AudioAdapter, ToolBridge (GA v2)
│       ├── tools.py             ← ✅ HermesToolBridge
│       ├── cli.py               ← ✅ CLI entrypoint
│       └── adapters/
│           ├── __init__.py
│           ├── voice_pe.py      ← ✅ Voice PE (ESPHome WebSocket, 24kHz)
│           ├── discord_vc.py    ← ✅ Discord VC (VoiceReceiver approach, Opus ↔ PCM)
│           └── matrix_vc.py     ← ✅ Matrix VC (LiveKit, e2e tested)
├── scripts/
│   ├── test_connectivity.py     ← ✅ API connectivity test (passing)
│   ├── test_matrix_vc.py        ← ✅ Matrix VC e2e test (passing)
│   ├── run_matrix_vc.py         ← ✅ Quick launcher
│   └── run_matrix_vc.sh         ← ✅ Shell wrapper
└── tests/
    └── test_core.py             ← ⬜
```

## How to Resume

1. **Load this file** — it's the project memory
2. **Check `todo` list** — what's in_progress vs pending
3. **Read `src/hermes_realtime/core.py`** — understand the bridge architecture
4. **Next priorities:**
   - Matrix VC adapter (Josh asked about Matrix voice support)
   - Git init + GitHub push
   - README.md
   - Voice PE hardware order

## Dependencies

- `websockets` — Realtime API WebSocket client
- `pydantic` — config validation
- `numpy` — audio buffer handling
- `pyyaml` — config file parsing
- `aiohttp` — Voice PE adapter (ESPHome WebSocket)
- `discord.py[voice]` + `PyNaCl` — Discord VC adapter
- `matrix-nio` — Matrix VC adapter (planned)

## Environment

- **Project root:** `/home/hermes/projects/hermes-realtime-bridge/`
- **Python:** 3.11+ (use `uv` for venv management)
- **API key:** `OPENAI_API_KEY` in `.env` (service account key, full model access)
- **Hermes tools:** Access via subprocess or direct import from `~/.hermes/hermes-agent/`

## Notes

- The Voice PE hardware ($69 USD) hasn't been ordered yet — adapter can be built against the ESPHome spec
- Discord adapter is the heavier lift (Opus codec, UDP session management)
- Matrix adapter would use WebRTC + Opus (similar to Discord but Matrix signaling)
- Realtime API costs: $32/1M audio input tokens, $64/1M audio output tokens — casual use ~$5-15/month
- Bridge should run as a systemd user service alongside the Hermes gateway
- Tested 2026-07-15: bridge connects, sends text, receives 189KB audio response ✅
