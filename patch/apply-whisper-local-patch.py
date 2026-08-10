#!/usr/bin/env python3
"""Add a `local` whisper provider to ccgram: on-device whisper.cpp, no API key.

Upstream ccgram only knows hosted OpenAI-compatible providers (openai, groq) and
hard-requires an API key. This adds provider `local`, which shells out to
whisper.cpp's `whisper-cli` exactly like Zamua's telegram plugin does:
    download -> ffmpeg to 16 kHz mono s16le wav -> whisper-cli -> stdout

Model-per-call (no resident server) is deliberate: on a 4 GB Pi shared with
several Claude sessions, a persistent whisper-server would hold ~700 MB idle.
Cost is ~1-2 s of model load per request, which is noise next to inference.

The subprocesses are driven with asyncio (never blocking ccgram's event loop) —
a multi-minute transcription on a Pi would otherwise freeze the whole bot.

Config in ~/.ccgram/.env:
    CCGRAM_WHISPER_PROVIDER=local
    CCGRAM_WHISPER_MODEL_PATH=~/.local/share/whisper-models/ggml-large-v3-turbo-q5_0.bin
    CCGRAM_WHISPER_BIN=~/.local/bin/whisper-cli   # optional, else PATH
    CCGRAM_FFMPEG_BIN=ffmpeg                      # optional
    CCGRAM_WHISPER_THREADS=3                      # optional, default nproc-1
    CCGRAM_WHISPER_TIMEOUT=1800                   # optional, seconds
    CCGRAM_WHISPER_LANGUAGE=auto                  # optional

Idempotent. Re-run after any `uv tool upgrade ccgram`.
"""
import glob
import pathlib
import sys

# ---------------------------------------------------------------- provider module
LOCAL_SRC = '''\
"""On-device transcription via whisper.cpp's whisper-cli, with model laddering.

Added by ~/.ccgram/apply-whisper-local-patch.py — not part of upstream ccgram.

whisper.cpp only reads 16 kHz mono PCM wav, so every input is normalised with
ffmpeg first. Both subprocesses run through asyncio and are killed on timeout,
so a slow transcription can never block ccgram's event loop or leak a process.

Model laddering: whisper's cost is per 30 s WINDOW, so wall-clock grows linearly
with length and the big model becomes untenable on long audio (a 30 min file on
large-v3-turbo is ~1 hour). Instead of a fixed model we pick the most accurate
one that still fits a wall-clock budget, so short clips get full quality and
long ones stay bounded. Per-window costs below are MEASURED on this Pi 5 at 3
threads, not published ratios.
"""
from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from .base import TranscriptionResult

logger = structlog.get_logger(__name__)

# 16 kHz * 1 channel * 2 bytes — lets us read duration off the converted wav
# without a second ffprobe subprocess.
_WAV_BYTES_PER_SEC = 16000 * 2
_WINDOW_SECS = 30


@dataclass(frozen=True)
class _Model:
    """A rung on the ladder. secs_per_window/load_secs measured on this Pi."""

    name: str
    filename: str
    secs_per_window: float
    load_secs: float
    english_only: bool

    def estimate(self, duration_secs: float) -> float:
        windows = math.ceil(max(duration_secs, 1) / _WINDOW_SECS)
        return windows * self.secs_per_window + self.load_secs


# Best -> fastest. Measured 2026-08-09 on an 88 s sample, Pi 5, -t 3:
#   turbo 62.0 s/window | small.en 17.6 | base.en 6.3 | tiny.en 2.4
# i.e. turbo is ~2x SLOWER than realtime while small.en is ~1.7x faster.
_LADDER: tuple[_Model, ...] = (
    _Model("large-v3-turbo", "ggml-large-v3-turbo-q5_0.bin", 62.0, 5.2, False),
    _Model("small.en", "ggml-small.en-q5_1.bin", 17.6, 1.5, True),
    _Model("base.en", "ggml-base.en-q5_1.bin", 6.3, 0.6, True),
    _Model("tiny.en", "ggml-tiny.en-q5_1.bin", 2.4, 0.3, True),
)

_DEFAULT_MODEL_DIR = "~/.local/share/whisper-models"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _default_threads() -> int:
    # Leave a core for ccgram + the Claude sessions sharing this box; whisper.cpp
    # scales sub-linearly anyway (4 threads measured only ~8% over 3), so the
    # last core buys little and costs a lot in interactive responsiveness.
    return max(1, (os.cpu_count() or 2) - 1)


def _fmt_secs(secs: float) -> str:
    return f"{int(secs)}s" if secs < 90 else f"{round(secs / 60)} min"


async def _run(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """Run a command with a hard timeout, killing the process on expiry."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


class LocalWhisperCliTranscriber:
    """Transcribe on-device via whisper.cpp, choosing a model by audio length."""

    def __init__(self) -> None:
        self.model_dir = Path(
            _env("CCGRAM_WHISPER_MODEL_DIR") or _DEFAULT_MODEL_DIR
        ).expanduser()
        # Setting MODEL_PATH pins one model and disables laddering entirely.
        self.pinned = _env("CCGRAM_WHISPER_MODEL_PATH")
        self.binary = _env("CCGRAM_WHISPER_BIN") or "whisper-cli"
        self.ffmpeg = _env("CCGRAM_FFMPEG_BIN") or "ffmpeg"
        # A fixed language, NOT "auto": auto-detect costs one extra full encoder
        # pass (~55 s on this Pi, measured) on top of the real work.
        self.language = _env("CCGRAM_WHISPER_LANGUAGE") or "en"
        self.threads = int(_env("CCGRAM_WHISPER_THREADS") or _default_threads())
        self.timeout = float(_env("CCGRAM_WHISPER_TIMEOUT") or 7200)
        # Target wall-clock. The ladder picks the best model that fits this, so
        # a longer file gets a smaller model rather than a longer wait.
        self.budget = float(_env("CCGRAM_WHISPER_BUDGET_SECS") or 240)

    # ------------------------------------------------------------- model choice
    def _available(self) -> list[_Model]:
        """Ladder rungs whose weights are present and whose language fits.

        The small rungs are English-only builds; if the user configured another
        language they must not be selected, or output would be garbage.
        """
        english = self.language.lower().startswith("en")
        return [
            m
            for m in _LADDER
            if (m.filename and (self.model_dir / m.filename).exists())
            and (english or not m.english_only)
        ]

    def plan(self, duration_secs: float) -> tuple[_Model, float]:
        """Pick the most accurate model whose estimate fits the budget."""
        if self.pinned:
            path = Path(self.pinned).expanduser()
            known = next((m for m in _LADDER if m.filename == path.name), None)
            model = known or _Model(path.stem, path.name, 62.0, 5.2, False)
            return model, model.estimate(duration_secs)

        candidates = self._available()
        if not candidates:
            msg = (
                f"No whisper models found in {self.model_dir} "
                f"(language={self.language!r})"
            )
            raise RuntimeError(msg)
        for model in candidates:
            est = model.estimate(duration_secs)
            if est <= self.budget:
                return model, est
        fastest = candidates[-1]
        return fastest, fastest.estimate(duration_secs)

    def model_path(self, model: _Model) -> Path:
        if self.pinned:
            return Path(self.pinned).expanduser()
        return self.model_dir / model.filename

    def estimate_secs(self, duration_secs: float) -> float:
        return self.plan(duration_secs)[1]

    def progress_hint(self, duration_secs: float) -> str:
        """Text for the 'transcribing...' message, e.g. ' — small.en, about 2 min'."""
        try:
            model, est = self.plan(duration_secs)
        except RuntimeError:
            return ""
        return f" \\u2014 {model.name}, about {_fmt_secs(est)}"

    # ------------------------------------------------------------------ running
    def _resolve_binary(self) -> str:
        """Absolute path to whisper-cli, honouring ~ and PATH."""
        cand = Path(self.binary).expanduser()
        if cand.is_absolute() or "/" in self.binary:
            if not cand.exists():
                msg = f"whisper-cli not found at {cand}"
                raise RuntimeError(msg)
            return str(cand)
        found = shutil.which(self.binary)
        if not found:
            msg = f"whisper-cli not found on PATH (looked for {self.binary!r})"
            raise RuntimeError(msg)
        return found

    async def transcribe(
        self, audio_bytes: bytes, filename: str
    ) -> TranscriptionResult:
        """Transcribe audio bytes on-device. Raises RuntimeError on failure."""
        binary = self._resolve_binary()
        if not shutil.which(self.ffmpeg) and not Path(self.ffmpeg).expanduser().exists():
            msg = f"ffmpeg not found ({self.ffmpeg!r}) — needed to decode audio"
            raise RuntimeError(msg)

        suffix = Path(filename).suffix or ".bin"
        with tempfile.TemporaryDirectory(prefix="ccgram-whisper-") as tmp:
            src = Path(tmp) / f"in{suffix}"
            wav = Path(tmp) / "in16k.wav"
            src.write_bytes(audio_bytes)

            # 1) normalise to what whisper.cpp accepts: 16 kHz mono s16le wav
            try:
                rc, _, err = await _run(
                    [
                        self.ffmpeg, "-nostdin", "-y", "-i", str(src),
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                        "-f", "wav", str(wav),
                    ],
                    timeout=300,
                )
            except asyncio.TimeoutError:
                msg = "Audio conversion timed out (ffmpeg)."
                raise RuntimeError(msg) from None
            if rc != 0 or not wav.exists():
                tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
                msg = "Could not decode that audio file: " + " / ".join(tail)
                raise RuntimeError(msg)

            # 2) choose a model from the ACTUAL duration, not Telegram metadata
            duration = wav.stat().st_size / _WAV_BYTES_PER_SEC
            model, est = self.plan(duration)
            model_file = self.model_path(model)
            if not model_file.exists():
                msg = f"Whisper model not found at {model_file}"
                raise RuntimeError(msg)

            # 3) transcribe
            cmd = [
                binary, "-m", str(model_file), "-f", str(wav),
                "-t", str(self.threads), "-nt", "-np",
                "-l", self.language or "en",
            ]
            logger.info(
                "whisper_local_start",
                model=model.name, audio_secs=round(duration, 1),
                estimate_secs=round(est), threads=self.threads,
            )
            try:
                rc, out, err = await _run(cmd, timeout=self.timeout)
            except asyncio.TimeoutError:
                mins = int(self.timeout // 60)
                msg = (
                    f"Transcription timed out after {mins} min. Raise "
                    "CCGRAM_WHISPER_TIMEOUT or use a shorter clip."
                )
                raise RuntimeError(msg) from None
            if rc != 0:
                tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
                msg = "whisper-cli failed: " + " / ".join(tail)
                raise RuntimeError(msg)

        text = " ".join(out.decode("utf-8", "replace").split())
        logger.info("whisper_local_done", model=model.name, chars=len(text))
        return TranscriptionResult(text=text, language=None)
'''

# ---------------------------------------------------------------- locate package
matches = glob.glob(
    str(
        pathlib.Path.home()
        / ".local/share/uv/tools/ccgram/lib/python*/site-packages/ccgram/whisper/__init__.py"
    )
)
if not matches:
    sys.exit("ERROR: could not find ccgram/whisper/__init__.py")
INIT = pathlib.Path(matches[0])
(INIT.parent / "local_cli.py").write_text(LOCAL_SRC)
print(f"wrote {INIT.parent / 'local_cli.py'}")

src = INIT.read_text()
if "local_cli" in src:
    print("whisper/__init__.py already patched")
    sys.exit(0)

# Branch to the local provider before the hosted-provider lookup, which would
# otherwise reject it as unknown and demand an API key.
ANCHOR = "    provider = config.whisper_provider\n    if not provider:\n        return None\n"
if ANCHOR not in src:
    sys.exit("ERROR: get_transcriber anchor not found (ccgram version changed?)")
src = src.replace(
    ANCHOR,
    ANCHOR
    + "\n"
    + "    if provider == \"local\":\n"
    + "        from .local_cli import LocalWhisperCliTranscriber\n"
    + "\n"
    + "        return LocalWhisperCliTranscriber()\n",
    1,
)
INIT.write_text(src)
print(f"patched {INIT}")
