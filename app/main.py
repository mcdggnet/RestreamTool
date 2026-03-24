import logging
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

        os.makedirs(HLS_DIR, exist_ok=True)
        os.makedirs(MEDIA_DIR, exist_ok=True)

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
                    logger.warning("FFmpeg exited — restarting")
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
        logger.info("Starting placeholder")
        self._ffmpeg = subprocess.Popen(cmd, stderr=subprocess.PIPE)

    def _start_live_locked(self):
        self._streamlink = subprocess.Popen(
            ["streamlink", "--stdout", "--loglevel", "error", self._source, "best"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._ffmpeg = subprocess.Popen(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
             "-i", "pipe:0", "-c", "copy", *_HLS_OUT],
            stdin=self._streamlink.stdout,
            stderr=subprocess.PIPE,
        )
        logger.info("Live stream started: %s", self._source)

    def _start_file_locked(self):
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-stream_loop", "-1",
            "-i", self._source,
            "-c", "copy",
            *_HLS_OUT,
        ]
        logger.info("File playback started: %s", self._source)
        self._ffmpeg = subprocess.Popen(cmd, stderr=subprocess.PIPE)


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
