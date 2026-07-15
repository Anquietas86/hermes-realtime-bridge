#!/usr/bin/env python3
"""Launcher for Hermes Realtime Bridge — Matrix VC (LiveKit) adapter.

Reads API keys from .env and starts the bridge with proper env vars.
Usage: python scripts/run_matrix_vc.py
"""
import os
import sys
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def load_env():
    """Load key=value pairs from .env file."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        print(f"ERROR: {env_file} not found", file=sys.stderr)
        sys.exit(1)
    
    env = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                env[key] = val
    return env

def main():
    env = load_env()
    
    # Verify required keys
    for key in ("OPENAI_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if key not in env:
            print(f"ERROR: {key} not found in .env", file=sys.stderr)
            sys.exit(1)
    
    print(f"OPENAI_API_KEY: {len(env['OPENAI_API_KEY'])} chars")
    print(f"LIVEKIT_API_KEY: {len(env['LIVEKIT_API_KEY'])} chars")
    
    # Build command
    venv_python = str(PROJECT_ROOT / ".venv" / "bin" / "python")
    cmd = [
        venv_python, "-m", "hermes_realtime.cli",
        "--adapter", "matrix-vc",
        "--matrix-room", "!ooYStQUSKarbOQeTOj:hagger.au",
        "-v",
    ]
    cmd.extend(sys.argv[1:])
    
    # Set up environment
    proc_env = os.environ.copy()
    proc_env.update(env)
    
    # Run
    os.chdir(PROJECT_ROOT)
    proc = subprocess.Popen(cmd, env=proc_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    # Stream output
    try:
        for line in proc.stdout:
            print(line, end="")
    except KeyboardInterrupt:
        proc.terminate()
    
    proc.wait()
    sys.exit(proc.returncode)

if __name__ == "__main__":
    main()
