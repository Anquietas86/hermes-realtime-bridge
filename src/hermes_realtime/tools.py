"""Hermes Tool Bridge — routes Realtime API function calls to Hermes tools.

This bridge translates OpenAI Realtime API function call schemas into
actual Hermes tool invocations. It can run in two modes:

1. SUBPROCESS — spawns `hermes chat -q` for each tool call (simplest, most isolated)
2. DIRECT — imports Hermes tools directly (faster, needs Hermes in PYTHONPATH)

Default is SUBPROCESS for safety and isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Optional

from .core import ToolBridge

logger = logging.getLogger(__name__)

# ── Tool Definitions (OpenAI Realtime API format) ────────────────────────

HERMES_TOOLS = [
    {
        "type": "function",
        "name": "ha_control",
        "description": "Control a Home Assistant device — turn lights on/off, set temperature, lock doors, open/close covers, run scenes, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Device domain: light, switch, climate, cover, lock, scene, script, fan, media_player",
                    "enum": ["light", "switch", "climate", "cover", "lock", "scene", "script", "fan", "media_player"],
                },
                "service": {
                    "type": "string",
                    "description": "Service to call: turn_on, turn_off, toggle, set_temperature, etc.",
                },
                "entity_id": {
                    "type": "string",
                    "description": "Full entity ID, e.g. light.living_room, climate.thermostat",
                },
                "data": {
                    "type": "object",
                    "description": "Additional parameters: brightness, temperature, hvac_mode, color_name, etc.",
                },
            },
            "required": ["domain", "service"],
        },
    },
    {
        "type": "function",
        "name": "ha_query",
        "description": "Query the state of a Home Assistant entity or list entities by domain/area.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "What to query",
                    "enum": ["get_state", "list_entities"],
                },
                "entity_id": {
                    "type": "string",
                    "description": "Entity ID for get_state, e.g. sensor.temperature",
                },
                "domain": {
                    "type": "string",
                    "description": "Filter by domain for list_entities: light, switch, climate, sensor, etc.",
                },
                "area": {
                    "type": "string",
                    "description": "Filter by area name for list_entities: living room, kitchen, etc.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "type": "function",
        "name": "infra_query",
        "description": "Query infrastructure — check server status, NFS mounts, Docker containers, network health, Zabbix alerts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to check: nfs, docker, network, zabbix, all",
                    "enum": ["nfs", "docker", "network", "zabbix", "all"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "memory_lookup",
        "description": "Look up information from persistent memory — user preferences, environment details, past decisions, project context.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up: a person, project, preference, or topic",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "run_command",
        "description": "Run a shell command on the server. Use for system checks, file operations, service management. Avoid destructive commands.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                },
            },
            "required": ["command"],
        },
    },
]


# ── Tool Bridge Implementation ───────────────────────────────────────────


class HermesToolBridge(ToolBridge):
    """Routes Realtime API function calls to Hermes tools via subprocess."""

    def __init__(
        self,
        hermes_home: Optional[str] = None,
        mode: str = "subprocess",
    ):
        self.hermes_home = hermes_home or os.path.expanduser("~/.hermes")
        self.mode = mode
        self._hermes_bin = os.path.join(self.hermes_home, "hermes-agent", "venv", "bin", "hermes")

    async def get_tools(self) -> list[dict]:
        """Return tool definitions for the Realtime API session."""
        return HERMES_TOOLS

    async def execute(self, name: str, arguments: dict) -> str:
        """Execute a tool and return the result."""
        handler = getattr(self, f"_handle_{name}", None)
        if handler:
            try:
                result = await handler(arguments)
                return json.dumps({"success": True, "result": result})
            except Exception as e:
                logger.exception("Tool %s failed", name)
                return json.dumps({"success": False, "error": str(e)})
        else:
            return json.dumps({"success": False, "error": f"Unknown tool: {name}"})

    # ── Tool Handlers ─────────────────────────────────────────────────

    async def _handle_ha_control(self, args: dict) -> str:
        """Control a Home Assistant device."""
        domain = args["domain"]
        service = args["service"]
        entity_id = args.get("entity_id", "")
        data = args.get("data", {})

        cmd_parts = [self._hermes_bin, "chat", "-q"]
        if entity_id:
            cmd_parts.append(
                f"Call ha_call_service with domain={domain}, service={service}, "
                f"entity_id={entity_id}, data={json.dumps(data)}. "
                f"Report what you did and the result."
            )
        else:
            cmd_parts.append(
                f"Call ha_call_service with domain={domain}, service={service}, "
                f"data={json.dumps(data)}. Report what you did and the result."
            )

        return await self._run_hermes(cmd_parts)

    async def _handle_ha_query(self, args: dict) -> str:
        """Query Home Assistant state."""
        action = args["action"]
        entity_id = args.get("entity_id", "")
        domain = args.get("domain", "")
        area = args.get("area", "")

        if action == "get_state" and entity_id:
            prompt = f"Call ha_get_state for {entity_id} and report the state and key attributes."
        elif action == "list_entities":
            filters = []
            if domain:
                filters.append(f"domain={domain}")
            if area:
                filters.append(f"area={area}")
            filter_str = ", ".join(filters) if filters else "all entities"
            prompt = f"Call ha_list_entities with {filter_str} and summarize what you find."
        else:
            return "Invalid query action"

        return await self._run_hermes([self._hermes_bin, "chat", "-q", prompt])

    async def _handle_infra_query(self, args: dict) -> str:
        """Query infrastructure health."""
        query = args["query"]
        prompt = (
            f"Run a quick infrastructure health check for: {query}. "
            f"Use terminal commands to check the relevant services. "
            f"Be concise — just report status and any issues."
        )
        return await self._run_hermes([self._hermes_bin, "chat", "-q", prompt])

    async def _handle_memory_lookup(self, args: dict) -> str:
        """Look up information from memory."""
        query = args["query"]
        prompt = (
            f"Look up '{query}' in memory using fact_store(action='probe') or "
            f"fact_store(action='search'). Report what you find concisely."
        )
        return await self._run_hermes([self._hermes_bin, "chat", "-q", prompt])

    async def _handle_run_command(self, args: dict) -> str:
        """Run a shell command."""
        command = args["command"]
        timeout = args.get("timeout", 30)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            result = stdout.decode("utf-8", errors="replace")[:2000]
            if stderr:
                result += "\n[stderr]\n" + stderr.decode("utf-8", errors="replace")[:500]
            return result
        except asyncio.TimeoutError:
            return f"Command timed out after {timeout}s"
        except Exception as e:
            return f"Command failed: {e}"

    # ── Helpers ───────────────────────────────────────────────────────

    async def _run_hermes(self, cmd: list[str], timeout: int = 60) -> str:
        """Run a Hermes CLI command and return the output."""
        env = os.environ.copy()
        env["HERMES_HOME"] = self.hermes_home

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output += "\n[stderr]\n" + stderr_text[:500]
            return output[:3000]  # Cap at 3000 chars
        except asyncio.TimeoutError:
            return f"Hermes command timed out after {timeout}s"
        except FileNotFoundError:
            return f"Hermes binary not found at {self._hermes_bin}"
        except Exception as e:
            return f"Hermes command failed: {e}"
