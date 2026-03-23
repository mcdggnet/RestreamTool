import subprocess
import threading
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

HLS_DIR = "/hls"
AUDIO_DIR = "/audio"


class StreamManager:
    def __init__(self):
        self.current_url: Optional[str] = None
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._streamlink_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = True

        os.makedirs(HLS_DIR, exist_ok=True)
        os.makedirs(AUDIO_DIR, exist_ok=True)

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
            *_video_hls_out("0:v", "1:a"),
            *_audio_hls_out("1:a"),
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
            *_video_hls_out("0:v:0", "0:a:0"),
            *_audio_hls_out("0:a:0"),
        ]
        self._ffmpeg_proc = subprocess.Popen(
            ff_cmd,
            stdin=self._streamlink_proc.stdout,
            stderr=subprocess.PIPE,
        )
        logger.info("FFmpeg transcoder started")


# ------------------------------------------------------------------ helpers

def _video_hls_out(video_map: str, audio_map: str) -> list:
    return [
        "-map", video_map, "-map", audio_map,
        "-c:v", "libx264",
        "-vf", "scale=-2:360",
        "-b:v", "500k", "-maxrate", "600k", "-bufsize", "1200k",
        "-g", "50", "-sc_threshold", "0", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_filename", f"{HLS_DIR}/seg_%03d.ts",
        f"{HLS_DIR}/stream.m3u8",
    ]


def _audio_hls_out(audio_map: str) -> list:
    return [
        "-map", audio_map,
        "-vn",
        "-c:a", "libopus", "-b:a", "96k", "-ar", "48000",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", f"{AUDIO_DIR}/seg_%03d.m4s",
        f"{AUDIO_DIR}/stream.m3u8",
    ]
