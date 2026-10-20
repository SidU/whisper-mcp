# whisper-mcp

Local Whisper mic transcription, exposed as an [MCP](https://modelcontextprotocol.io) server.

Lets any MCP client — Claude Code, Claude Desktop, Quickwork, Cursor, etc. — start a mic recording, read the live transcript as it accumulates, and stop it. Everything runs locally. No audio leaves your machine.

Built on top of [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with VAD and incremental decoding. Packaged with [uv](https://docs.astral.sh/uv/) — one command to install, including the right Python.

## Why

Meeting-notes / interview / voice-memo workflows typically mean juggling two things: *recording* and *the agent that's going to use the transcript*. This bridges them. Your agent calls `start_recording` when a meeting begins, polls `get_transcript` for new content as the meeting progresses, and calls `stop_recording` at the end — all from the same conversation.

## Tools

| Tool | Purpose |
|---|---|
| `start_recording(topic?, model?, language?)` | Spawn the transcriber as a detached subprocess. Returns `session_id`, `transcript_path`, `pid`. Errors if one is already active. |
| `stop_recording(session_id?)` | SIGINT the subprocess so buffered audio flushes, wait up to 15s, return final text + duration. |
| `get_transcript(session_id?, tail_lines?, since_offset?)` | Read the transcript. Safe mid-recording. `since_offset` lets a client poll incrementally. |
| `get_status()` | Is anything recording right now? Also reaps dead-but-registered sessions. |
| `list_sessions(limit=10)` | Recent sessions, newest first. |

## Install

Prereqs:
- macOS or Linux
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — `curl -LsSf https://astral.sh/uv/install.sh | sh` or `brew install uv`
- macOS: `brew install portaudio` (sounddevice depends on it)

```bash
git clone https://github.com/SidU/whisper-mcp.git
cd whisper-mcp
uv sync
uv run whisper_mcp.py --print-config
```

`uv sync` reads `pyproject.toml` + `uv.lock`, downloads a compatible Python (3.10–3.13) if you don't have one, creates `.venv/`, and installs the exact pinned deps.

`--print-config` prints a copy-pasteable `mcpServers` JSON blob with the absolute path to this clone already filled in — one version for empty config files, one inner-only version for existing configs.

## Register with your MCP client

Run `uv run whisper_mcp.py --print-config` from the repo and paste the output into the right config file:

| Client | Config path |
|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Code | `~/.claude.json` (user) or `.mcp.json` (project) |
| Quickwork | `~/.quickwork/mcp_config.json` |
| Cursor | `~/.cursor/mcp.json` |

Then restart the client. `uv run` handles the venv activation and Python selection on every invocation — no need to point at `.venv/bin/python` yourself.

**Claude Code shortcut** — skip the JSON edit by running this from inside the repo:
```bash
claude mcp add whisper-local -- uv --directory "$(pwd)" run whisper_mcp.py
```

## First run

The first call to `start_recording` downloads the Whisper model (~500 MB for `small`, the default) into `~/.cache/huggingface/`. Takes about a minute. Later runs are instant.

Smoke-test the transcriber directly without MCP:

```bash
uv run transcribe_mic.py --model small --language en
# say something, then Ctrl+C
```

If that works, the MCP server will too.

## State and files

By default everything lives under `~/.whisper-mcp/`:

```
~/.whisper-mcp/
├── sessions.json       # session registry (survives restarts)
├── transcripts/        # one .txt per session
└── logs/               # subprocess stderr, for debugging
```

Set `WHISPER_MCP_STATE_DIR` in the server's env (via your MCP client config) to put it elsewhere.

## Design notes

- **One active session at a time.** Deliberate: concurrent recordings make "the current session" ambiguous. Stop one before starting another.
- **Detached subprocess.** The recorder runs in its own process group (`start_new_session=True`). Restarting the MCP server doesn't kill an in-flight recording.
- **Thin MCP layer.** The server is just process management + file reads. All the audio / inference heavy lifting lives in `transcribe_mic.py`, which also works standalone as a CLI.
- **State persists across restarts.** `sessions.json` means `get_status()` works even after the MCP server restarts; the subprocess is identified by PID. Dead PIDs are reaped automatically.
- **Why Whisper and not Parakeet.** faster-whisper is reliable on CPU with int8. parakeet-mlx has crashed with MLX threading errors in my testing. YMMV.
- **Why Python ≤ 3.13.** PyAV (a faster-whisper dep) has no 3.14 wheels yet. `pyproject.toml` caps the version and uv will pick 3.13.

## Model options

Pass to `start_recording(model=...)`:

| Model | Size | Latency on M-series CPU | Notes |
|---|---|---|---|
| `tiny` | 75 MB | fastest | fine for simple en speech |
| `base` | 142 MB | fast | ok |
| `small` | 466 MB | moderate | **default — best balance for meetings** |
| `medium` | 1.5 GB | slow | marginal gain over small for most uses |
| `large-v3` | 2.9 GB | slowest | best accuracy, needs patience on CPU |

## Troubleshooting

**`transcribe_mic.py exited immediately`** — look at the log path in the error, usually `~/.whisper-mcp/logs/<session_id>.log`. Common causes: PortAudio not installed, no mic permission granted to your terminal/MCP host.

**Mic permission** — on macOS, the first time you record, the OS prompts the parent process (your MCP client / terminal) for mic access. If you miss the prompt, grant it in System Settings → Privacy & Security → Microphone and restart.

**`get_status` says my session is "stopped_unexpectedly"** — the subprocess died. Check the log. Most often means the model download was interrupted or PortAudio broke.

## License

MIT. See [LICENSE](LICENSE).
