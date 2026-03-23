import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from stream_manager import StreamManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

app.mount("/hls", StaticFiles(directory="/hls"), name="hls")
app.mount("/audio", StaticFiles(directory="/audio"), name="audio")


class StreamRequest(BaseModel):
    url: str


@app.get("/")
def index():
    return {
        "endpoints": {
            "set_stream": "POST /stream  { url: '...' }",
            "stop_stream": "DELETE /stream",
            "status": "GET /status",
            "video_hls": "GET /hls/stream.m3u8",
            "audio_hls": "GET /audio/stream.m3u8",
        }
    }


@app.post("/stream")
def set_stream(req: StreamRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    manager.set_url(url)
    return {"status": "ok", "url": url}


@app.delete("/stream")
def stop_stream():
    manager.clear_url()
    return {"status": "ok", "message": "Switched to placeholder"}


@app.get("/status")
def status():
    return manager.get_status()
