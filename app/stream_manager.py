import subprocess
import threading
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

HLS_DIR = "/hls"


class StreamManager:
    def __init__(self):
        self.current_url: Optional[str] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._streamlink_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = True

        os.makedirs(HLS_DIR, exist_ok=True)

        self._start_placeholder()

        watchdog = threading.Thread(target=self._watchdog, daemon=True)
        watchdog.start()

    # ------------------------------------------------------------------ public

    def set_url(self, url: str):
        with self._lock:
            self.current_url = url
            self._restart_locked()

    def clear_url(self):
        with self._lock:
            self.current_url = None
            self._restart_locked()

    def get_status(self) -> dict:
        with self._lock:
            ffmpeg_alive = (
                self._ffmpeg_proc is not None
                and self._ffmpeg_proc.poll() is None
            )
            return {
                "streaming": self.current_url is not None,
                "url": self.current_url,
                "ffmpeg_alive": ffmpeg_alive,
            }

    # ----------------------------------------------------------------- private

    def _watchdog(self):
        """Restart processes if they die unexpectedly."""
        while self._running:
            time.sleep(5)
            with self._lock:
                ffmpeg_alive = (
                    self._ffmpeg_proc is not None
                    and self._ffmpeg_proc.poll() is None
                )
                if not ffmpeg_alive:
                    logger.warning("FFmpeg process died — restarting")
                    self._restart_locked()

    def _restart_locked(self):
        """Must be called with self._lock held."""
        self._stop_locked()
        if self.current_url:
            self._start_stream_locked(self.current_url)
        else:
            self._start_placeholder_locked()

    def _stop_locked(self):
        for proc in (self._ffmpeg_proc, self._streamlink_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._ffmpeg_proc = None
        self._streamlink_proc = None

    def _start_placeholder(self):
        with self._lock:
            self._start_placeholder_locked()

    def _start_placeholder_locked(self):
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            # Video input: black screen
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=25,format=yuv420p",
            # Audio input: silence
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            *_hls_out_encode("0:v", "1:a"),
        ]
        logger.info("Starting placeholder stream")
        self._ffmpeg_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)

    def _start_stream_locked(self, url: str):
        logger.info("Starting streamlink for %s", url)
        sl_cmd = [
            "streamlink",
            "--stdout",
            "--loglevel", "error",
            url,
            "best",
        ]
        self._streamlink_proc = subprocess.Popen(
            sl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        ff_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", "pipe:0",
            *_hls_out_copy(),
        ]
        self._ffmpeg_proc = subprocess.Popen(
            ff_cmd,
            stdin=self._streamlink_proc.stdout,
            stderr=subprocess.PIPE,
        )
        logger.info("FFmpeg restream started")


# ------------------------------------------------------------------ helpers

def _hls_out_copy() -> list:
    """Pass-through: no re-encode, native quality."""
    return [
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_filename", f"{HLS_DIR}/seg_%03d.ts",
        f"{HLS_DIR}/stream.m3u8",
    ]


def _hls_out_encode(video_map: str, audio_map: str) -> list:
    """Encode from synthetic lavfi inputs (placeholder only)."""
    return [
        "-map", video_map, "-map", audio_map,
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_filename", f"{HLS_DIR}/seg_%03d.ts",
        f"{HLS_DIR}/stream.m3u8",
    ]
