#!/usr/bin/env python3
"""MCP server for local Whisper mic transcription.

Wraps transcribe_mic.py (faster-whisper, in a local venv) as an MCP server so
MCP clients (Claude Code, Claude Desktop, Cursor, etc.) can start, stop, and
read a live transcript from the mic.

Design:
- The MCP server itself is lightweight — just process management + file reads.
- Audio capture + inference happen in transcribe_mic.py, launched as a detached
  subprocess under this repo's venv.
- One active recording at a time. State persists in WHISPER_MCP_STATE_DIR
  (default: ~/.whisper-mcp/) so a session survives MCP-server restarts (the
  recorder outlives the MCP server because it's started detached).

Tools:
  start_recording(topic?, model?, language?)      -> session_id, transcript_path, pid
  stop_recording(session_id?)                     -> text, duration, line_count, path
  get_transcript(session_id?, tail_lines?, since_offset?) -> text, offset, is_recording
  get_status()                                    -> active session info or null
  list_sessions(limit=10)                         -> recent sessions
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

# -------------------- Paths --------------------

REPO_ROOT = Path(__file__).resolve().parent
TRANSCRIBE_SCRIPT = REPO_ROOT / "transcribe_mic.py"
# The MCP server runs inside the uv-managed venv (uv sync + uv run), so
# sys.executable is already the right Python for the transcriber.
TRANSCRIBE_PY = sys.executable

STATE_DIR = Path(os.environ.get("WHISPER_MCP_STATE_DIR", Path.home() / ".whisper-mcp"))
TRANSCRIPTS_DIR = STATE_DIR / "transcripts"
SESSIONS_FILE = STATE_DIR / "sessions.json"
LOG_DIR = STATE_DIR / "logs"

for d in (TRANSCRIPTS_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)


# -------------------- State --------------------

def _load_sessions() -> list[dict]:
    if not SESSIONS_FILE.exists():
        return []
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except json.JSONDecodeError:
        return []


def _save_sessions(sessions: list[dict]) -> None:
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _reap_dead() -> list[dict]:
    """Mark any 'active' session whose PID is gone as 'stopped_unexpectedly'."""
    sessions = _load_sessions()
    changed = False
    for s in sessions:
        if s.get("status") == "active" and not _pid_alive(s["pid"]):
            s["status"] = "stopped_unexpectedly"
            s["stopped_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
    if changed:
        _save_sessions(sessions)
    return sessions


def _active_session() -> Optional[dict]:
    for s in _reap_dead():
        if s.get("status") == "active":
            return s
    return None


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s or "recording"


# -------------------- MCP tools --------------------

mcp = MCPServer("whisper-local")


@mcp.tool()
def start_recording(
    topic: Optional[str] = None,
    model: str = "small",
    language: str = "en",
) -> dict:
    """Start a local Whisper mic transcription session.

    Spawns transcribe_mic.py as a detached subprocess. Only one session can be
    active at a time — call stop_recording first.

    Args:
      topic:    short label (e.g. "1:1 with Ragini"). Used in the filename;
                defaults to "recording". Spaces/punctuation are kebab-cased.
      model:    faster-whisper model name. Default "small". Options: tiny, base,
                small, medium, large-v3. "small" is a good balance for meetings.
      language: ISO code. Default "en".

    Returns:
      {session_id, transcript_path, pid, started_at, topic}
    """
    if _active_session() is not None:
        active = _active_session()
        raise RuntimeError(
            f"a recording is already active (session {active['session_id']}, "
            f"started {active['started_at']}). Stop it first."
        )

    slug = _slugify(topic or "recording")
    ts = datetime.now()
    session_id = f"{slug}_{ts:%Y%m%d_%H%M%S}"
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.txt"
    log_path = LOG_DIR / f"{session_id}.log"

    cmd = [
        TRANSCRIBE_PY,
        str(TRANSCRIBE_SCRIPT),
        "-o", str(transcript_path),
        "--model", model,
        "--language", language,
    ]

    log_fh = open(log_path, "ab")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach from this MCP process group
        cwd=str(STATE_DIR),
    )

    # Give the subprocess ~0.5s to fail fast (bad venv, missing deps, etc.)
    time.sleep(0.5)
    if proc.poll() is not None:
        tail = log_path.read_text(errors="replace")[-800:]
        raise RuntimeError(
            f"transcribe_mic.py exited immediately (code {proc.returncode}).\n"
            f"Log tail:\n{tail}\n"
            f"Did you run ./setup.sh? Full log: {log_path}"
        )

    session = {
        "session_id": session_id,
        "topic": topic or "",
        "slug": slug,
        "pid": proc.pid,
        "transcript_path": str(transcript_path),
        "log_path": str(log_path),
        "model": model,
        "language": language,
        "started_at": ts.isoformat(timespec="seconds"),
        "stopped_at": None,
        "status": "active",
    }
    sessions = _load_sessions()
    sessions.append(session)
    _save_sessions(sessions)

    return {
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "pid": proc.pid,
        "started_at": session["started_at"],
        "topic": topic or "",
    }


@mcp.tool()
def stop_recording(session_id: Optional[str] = None) -> dict:
    """Stop the active (or named) recording and return the final transcript.

    Sends SIGINT so transcribe_mic.py flushes its audio buffer before exiting
    (clean shutdown — the script traps SIGINT for this purpose). Waits up to
    15s for the process to exit, then SIGTERMs as a backstop.

    Args:
      session_id: optional. Defaults to the currently-active session.

    Returns:
      {session_id, text, line_count, duration_sec, transcript_path, status}
    """
    sessions = _reap_dead()

    if session_id is None:
        candidates = [s for s in sessions if s.get("status") == "active"]
        if not candidates:
            raise RuntimeError("no active session to stop")
        session = candidates[-1]
    else:
        session = next((s for s in sessions if s["session_id"] == session_id), None)
        if session is None:
            raise RuntimeError(f"session not found: {session_id}")

    pid = session["pid"]
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass

        deadline = time.time() + 15
        while _pid_alive(pid) and time.time() < deadline:
            time.sleep(0.3)

        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(0.5)

    session["status"] = "stopped"
    session["stopped_at"] = datetime.now().isoformat(timespec="seconds")
    _save_sessions(sessions)

    transcript_path = Path(session["transcript_path"])
    text = transcript_path.read_text(errors="replace") if transcript_path.exists() else ""
    line_count = sum(1 for _ in text.splitlines()) if text else 0

    started = datetime.fromisoformat(session["started_at"])
    stopped = datetime.fromisoformat(session["stopped_at"])
    duration_sec = int((stopped - started).total_seconds())

    return {
        "session_id": session["session_id"],
        "text": text,
        "line_count": line_count,
        "duration_sec": duration_sec,
        "transcript_path": str(transcript_path),
        "status": session["status"],
    }


@mcp.tool()
def get_transcript(
    session_id: Optional[str] = None,
    tail_lines: Optional[int] = None,
    since_offset: Optional[int] = None,
) -> dict:
    """Read the transcript for the active (or named) session.

    Safe to call mid-recording — the transcript file is line-buffered. Use
    `since_offset` to poll incrementally (pass back the `end_offset` from the
    prior call to fetch only new content).

    Args:
      session_id:   optional. Defaults to active session, else most recent.
      tail_lines:   if set, return only the last N lines.
      since_offset: if set, return only bytes after this offset in the file.
                    tail_lines takes precedence if both are provided.

    Returns:
      {session_id, text, end_offset, line_count, is_recording, last_modified}
    """
    sessions = _reap_dead()
    if not sessions:
        raise RuntimeError("no sessions yet")

    if session_id is None:
        active = next((s for s in sessions if s.get("status") == "active"), None)
        session = active if active else sessions[-1]
    else:
        session = next((s for s in sessions if s["session_id"] == session_id), None)
        if session is None:
            raise RuntimeError(f"session not found: {session_id}")

    path = Path(session["transcript_path"])
    if not path.exists():
        return {
            "session_id": session["session_id"],
            "text": "",
            "end_offset": 0,
            "line_count": 0,
            "is_recording": session.get("status") == "active",
            "last_modified": None,
        }

    raw = path.read_bytes()
    end_offset = len(raw)

    if tail_lines is not None and tail_lines > 0:
        lines = raw.decode(errors="replace").splitlines()
        text = "\n".join(lines[-tail_lines:])
    elif since_offset is not None and since_offset > 0:
        text = raw[since_offset:].decode(errors="replace")
    else:
        text = raw.decode(errors="replace")

    return {
        "session_id": session["session_id"],
        "text": text,
        "end_offset": end_offset,
        "line_count": raw.count(b"\n"),
        "is_recording": session.get("status") == "active",
        "last_modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


@mcp.tool()
def get_status() -> dict:
    """Return the currently-active recording session, if any.

    Also reaps any session whose subprocess died (marks it
    'stopped_unexpectedly'). Call this before start_recording if you want to
    confirm the slot is free.

    Returns:
      {active: session dict or null, recent: last 3 sessions}
    """
    sessions = _reap_dead()
    active = next((s for s in sessions if s.get("status") == "active"), None)
    return {
        "active": active,
        "recent": sessions[-3:],
    }


@mcp.tool()
def list_sessions(limit: int = 10) -> list[dict]:
    """List recent recording sessions, newest first.

    Args:
      limit: max number to return (default 10).
    """
    sessions = _reap_dead()
    return list(reversed(sessions[-limit:]))


def _print_config() -> None:
    """Print the mcpServers JSON blob with absolute paths filled in.

    Prints TWO versions:
      1. Full file — paste into a brand-new / empty client config.
      2. Inner block — drop in next to existing servers inside an existing
         "mcpServers": { ... } block.
    """
    inner = {
        "whisper-local": {
            "command": "uv",
            "args": ["--directory", str(REPO_ROOT), "run", "whisper_mcp.py"],
        }
    }
    full = {"mcpServers": inner}

    print()
    print("Add this to your MCP client's config file.")
    print()
    print("Config paths:")
    print("  Claude Desktop:  ~/Library/Application Support/Claude/claude_desktop_config.json")
    print("  Claude Code:     ~/.claude.json  (or project .mcp.json)")
    print("  Cursor:          ~/.cursor/mcp.json")
    print()
    print("--- If the config file is new / empty, paste this whole thing: ---")
    print(json.dumps(full, indent=2))
    print()
    print("--- If you already have an mcpServers block, paste just this inner entry inside it: ---")
    inner_json = json.dumps(inner, indent=2)
    # Strip the outermost { } so it drops cleanly into an existing block
    inner_lines = inner_json.splitlines()[1:-1]
    print("\n".join(inner_lines))
    print()
    print("Then restart the client.")
    print()


if __name__ == "__main__":
    if "--print-config" in sys.argv:
        _print_config()
        sys.exit(0)
    mcp.run()
