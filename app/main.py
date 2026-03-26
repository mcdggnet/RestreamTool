import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _log_versions():
    """Log tool versions and codec support at startup to catch env differences."""
    for tool, flag in (("ffmpeg", "-version"), ("streamlink", "--version")):
        path = shutil.which(tool)
        if not path:
            logger.error("DIAG: %s not found in PATH", tool)
            continue
        try:
            out = subprocess.check_output(
                [tool, flag], stderr=subprocess.STDOUT, text=True
            ).splitlines()[0]
            logger.info("DIAG: %s -> %s (at %s)", tool, out, path)
        except Exception as e:
            logger.error("DIAG: failed to get %s version: %s", tool, e)

    # Check libx264 is available in ffmpeg
    try:
        codecs = subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"], stderr=subprocess.STDOUT, text=True
        )
        for codec in ("libx264", "aac"):
            status = "available" if codec in codecs else "MISSING"
            logger.info("DIAG: ffmpeg codec %s: %s", codec, status)
    except Exception as e:
        logger.error("DIAG: failed to check ffmpeg encoders: %s", e)


def _log_dir(path: str):
    """Check a directory exists and is writable."""
    exists = os.path.isdir(path)
    writable = os.access(path, os.W_OK) if exists else False
    logger.info("DIAG: dir %s — exists=%s writable=%s", path, exists, writable)


def _drain_stderr(proc: subprocess.Popen, label: str):
    """
    Read stderr from a subprocess in a background thread and log each line.

    Critical: if stderr=PIPE and nothing reads it, the OS pipe buffer (~64 KB)
    fills up and the subprocess blocks/deadlocks. This is the most common cause
    of ffmpeg silently dying in prod under load.
    """
    def _read():
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                logger.warning("[%s] %s", label, line)
        rc = proc.wait()
        logger.info("[%s] process exited (returncode=%s)", label, rc)
    threading.Thread(target=_read, daemon=True).start()

HLS_DIR = "/hls"
MEDIA_DIR = "/media"

_HLS_OUT = [
    "-f", "hls",
    "-hls_time", "2",
    "-hls_list_size", "6",
    "-hls_flags", "delete_segments+append_list+independent_segments",
    "-hls_segment_filename", f"{HLS_DIR}/seg_%03d.ts",
    f"{HLS_DIR}/stream.m3u8",
]


# ---------------------------------------------------------------------------
# Stream manager
# ---------------------------------------------------------------------------

class StreamManager:
    def __init__(self):
        self._source_type: str = "placeholder"   # placeholder | live | file
        self._source: Optional[str] = None
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._streamlink: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

        _log_versions()

        os.makedirs(HLS_DIR, exist_ok=True)
        os.makedirs(MEDIA_DIR, exist_ok=True)
        _log_dir(HLS_DIR)
        _log_dir(MEDIA_DIR)

        with self._lock:
            self._restart_locked()

        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---- public ----

    def set_live(self, url: str):
        with self._lock:
            self._source_type = "live"
            self._source = url
            self._restart_locked()

    def set_file(self, filename: str):
        path = os.path.join(MEDIA_DIR, filename)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with self._lock:
            self._source_type = "file"
            self._source = path
            self._restart_locked()

    def clear(self):
        with self._lock:
            self._source_type = "placeholder"
            self._source = None
            self._restart_locked()

    def get_status(self) -> dict:
        with self._lock:
            return {
                "type": self._source_type,
                "source": self._source,
                "ffmpeg_alive": self._ffmpeg is not None and self._ffmpeg.poll() is None,
            }

    def list_files(self) -> list[str]:
        try:
            return sorted(f for f in os.listdir(MEDIA_DIR) if os.path.isfile(os.path.join(MEDIA_DIR, f)))
        except OSError:
            return []

    # ---- private ----

    def _watchdog(self):
        while True:
            time.sleep(5)
            with self._lock:
                if self._ffmpeg is None or self._ffmpeg.poll() is not None:
                    code = self._ffmpeg.returncode if self._ffmpeg else "n/a"
                    logger.warning(
                        "FFmpeg exited (returncode=%s, source_type=%s, source=%s) — restarting",
                        code, self._source_type, self._source,
                    )
                    self._restart_locked()

    def _restart_locked(self):
        self._stop_locked()
        if self._source_type == "live":
            self._start_live_locked()
        elif self._source_type == "file":
            self._start_file_locked()
        else:
            self._start_placeholder_locked()

    def _stop_locked(self):
        for proc in (self._ffmpeg, self._streamlink):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._ffmpeg = None
        self._streamlink = None

    def _start_placeholder_locked(self):
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25,format=yuv420p",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "64k",
            *_HLS_OUT,
        ]
        logger.info("Starting placeholder — cmd: %s", " ".join(cmd))
        self._ffmpeg = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        _drain_stderr(self._ffmpeg, "ffmpeg/placeholder")

    def _start_live_locked(self):
        sl_cmd = [
            "streamlink", "--stdout", "--loglevel", "warning",
            "--retry-streams", "1", "--retry-max", "0",  # loop forever on Kick segment gaps
            self._source, "best",
        ]
        logger.info("Starting streamlink — cmd: %s", " ".join(sl_cmd))
        self._streamlink = subprocess.Popen(
            sl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _drain_stderr(self._streamlink, "streamlink")

        ff_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                  "-i", "pipe:0", "-c", "copy", *_HLS_OUT]
        logger.info("Starting ffmpeg — cmd: %s", " ".join(ff_cmd))
        self._ffmpeg = subprocess.Popen(
            ff_cmd,
            stdin=self._streamlink.stdout,
            stderr=subprocess.PIPE,
        )
        _drain_stderr(self._ffmpeg, "ffmpeg/live")
        logger.info("Live stream started: %s", self._source)

    def _start_file_locked(self):
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-stream_loop", "-1",
            "-i", self._source,
            "-c", "copy",
            *_HLS_OUT,
        ]
        logger.info("Starting file playback — cmd: %s", " ".join(cmd))
        self._ffmpeg = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        _drain_stderr(self._ffmpeg, "ffmpeg/file")
        logger.info("File playback started: %s", self._source)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

manager: StreamManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = StreamManager()
    yield


app = FastAPI(title="Restream Tool", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LiveRequest(BaseModel):
    url: str

class FileRequest(BaseModel):
    filename: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return {
        "endpoints": {
            "live_stream": "POST /stream/live  { url: '...' }",
            "file_stream": "POST /stream/file  { filename: '...' }",
            "stop":        "DELETE /stream",
            "files":       "GET /files",
            "status":      "GET /status",
            "hls":         "GET /hls/stream.m3u8",
        }
    }


@app.post("/stream/live")
def stream_live(req: LiveRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "url is required")
    manager.set_live(url)
    return {"status": "ok", "type": "live", "source": url}


@app.post("/stream/file")
def stream_file(req: FileRequest):
    name = req.filename.strip()
    if not name or "/" in name or "\\" in name:
        raise HTTPException(400, "filename must be a plain filename with no path separators")
    try:
        manager.set_file(name)
    except FileNotFoundError:
        raise HTTPException(404, f"'{name}' not found in /media")
    return {"status": "ok", "type": "file", "source": name}


@app.delete("/stream")
def stop_stream():
    manager.clear()
    return {"status": "ok", "message": "Switched to placeholder"}


@app.get("/files")
def list_files():
    return {"files": manager.list_files()}


@app.get("/status")
def status():
    return manager.get_status()
