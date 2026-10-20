#!/usr/bin/env python3
"""Continuously transcribe microphone audio to a text file using faster-whisper.

Setup (handled by setup.sh in this repo):
    pip install faster-whisper sounddevice numpy

Usage:
    ./transcribe_mic.py                       # writes transcript_YYYYMMDD_HHMMSS.txt
    ./transcribe_mic.py -o notes.txt          # append to notes.txt
    ./transcribe_mic.py --model small --chunk 6 --language en

Stop with Ctrl+C. The pending audio in the buffer is flushed before exit.
"""

import argparse
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
CHANNELS = 1


def parse_args():
    p = argparse.ArgumentParser(description="Continuous mic transcription (faster-whisper).")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output file (default: transcript_YYYYMMDD_HHMMSS.txt in CWD).")
    p.add_argument("--model", default="base",
                   help="faster-whisper model: tiny, base, small, medium, large-v3 (default: base).")
    p.add_argument("--language", default=None,
                   help="Force language code (e.g., en). Default: auto-detect.")
    p.add_argument("--chunk", type=float, default=8.0,
                   help="Seconds of audio per transcription pass (default: 8).")
    p.add_argument("--device", type=str, default="auto",
                   help="Compute device: auto, cpu, cuda, metal (default: auto).")
    p.add_argument("--compute-type", default="int8",
                   help="CTranslate2 compute type: int8, int8_float16, float16, float32 (default: int8).")
    p.add_argument("--input-device", type=int, default=None,
                   help="sounddevice input device index. Use --list-devices to see options.")
    p.add_argument("--list-devices", action="store_true",
                   help="List available input devices and exit.")
    p.add_argument("--no-timestamps", action="store_true",
                   help="Do not prefix each line with a wall-clock timestamp.")
    return p.parse_args()


def resolve_device(choice: str) -> str:
    if choice != "auto":
        return choice
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def main():
    args = parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    out_path = args.output or Path.cwd() / f"transcript_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    device = resolve_device(args.device)
    print(f"Loading model '{args.model}' on {device} ({args.compute_type})...", file=sys.stderr)
    model = WhisperModel(args.model, device=device, compute_type=args.compute_type)
    print(f"Writing transcript to: {out_path}", file=sys.stderr)
    print("Recording. Press Ctrl+C to stop.", file=sys.stderr)

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue()
    stop_event = threading.Event()

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"[audio status] {status}", file=sys.stderr)
        audio_q.put(indata.copy().reshape(-1))

    chunk_samples = int(args.chunk * SAMPLE_RATE)
    fout = open(out_path, "a", buffering=1, encoding="utf-8")

    def transcribe_buffer(buf: np.ndarray):
        if buf.size == 0:
            return
        # faster-whisper expects float32 mono in [-1, 1].
        audio = buf.astype(np.float32)
        segments, _ = model.transcribe(
            audio,
            language=args.language,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300),
            beam_size=1,
        )
        text_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        if not text_parts:
            return
        line = " ".join(text_parts)
        prefix = "" if args.no_timestamps else f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
        fout.write(f"{prefix}{line}\n")
        print(f"{prefix}{line}", flush=True)

    def worker():
        buffer = np.zeros(0, dtype=np.float32)
        while not stop_event.is_set() or not audio_q.empty():
            try:
                chunk = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            buffer = np.concatenate([buffer, chunk])
            while buffer.size >= chunk_samples:
                head, buffer = buffer[:chunk_samples], buffer[chunk_samples:]
                transcribe_buffer(head)
        # Flush remainder on shutdown.
        transcribe_buffer(buffer)

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    def handle_sigint(_signum, _frame):
        print("\nStopping — flushing remaining audio...", file=sys.stderr)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=audio_callback,
            device=args.input_device,
            blocksize=int(SAMPLE_RATE * 0.5),
        ):
            while not stop_event.is_set():
                sd.sleep(200)
    finally:
        stop_event.set()
        worker_thread.join(timeout=30)
        fout.close()
        print(f"Done. Transcript: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
