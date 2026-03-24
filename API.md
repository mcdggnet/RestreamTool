# Restream Tool API

A self-hosted livestream proxy that ingests a Kick, Twitch, or YouTube stream and re-serves it as HLS at a fixed URL. When no stream is active it outputs a black/silent placeholder so clients stay connected.

---

## Base URL

```
http://localhost:8080
```

---

## Streams

### Video — `GET /hls/stream.m3u8`

Native quality, no re-encoding — the stream is passed through as-is into HLS segments. Feed it into any HLS player (VLC, hls.js, ffplay, Safari, etc).

```bash
ffplay http://localhost:8080/hls/stream.m3u8
```

---

## Control Endpoints

### `GET /status`

Returns the current state of the restreamer.

**Response**
```json
{
  "streaming": true,
  "url": "https://kick.com/streamer",
  "ffmpeg_alive": true
}
```

| Field | Type | Description |
|---|---|---|
| `streaming` | bool | Whether a source URL is active |
| `url` | string \| null | The current source URL |
| `ffmpeg_alive` | bool | Whether the transcoder process is running |

---

### `POST /stream`

Set the source stream. Accepts Kick, Twitch, or YouTube URLs. Starts restreaming immediately and replaces any previously active stream.

**Request body**
```json
{ "url": "https://kick.com/streamer" }
```

**Supported URL formats**
```
https://kick.com/<channel>
https://www.twitch.tv/<channel>
https://www.youtube.com/watch?v=<id>
https://www.youtube.com/@<handle>/live
```

**Response**
```json
{ "status": "ok", "url": "https://kick.com/streamer" }
```

**Example**
```bash
curl -X POST http://localhost:8080/stream \
  -H "Content-Type: application/json" \
  -d '{"url": "https://kick.com/streamer"}'
```

---

### `DELETE /stream`

Stop the current stream and revert to the black/silent placeholder. HLS clients remain connected.

**Response**
```json
{ "status": "ok", "message": "Switched to placeholder" }
```

**Example**
```bash
curl -X DELETE http://localhost:8080/stream
```

---

## Quick Start

```bash
# Start the container
docker compose up --build

# Point it at a stream
curl -X POST http://localhost:8080/stream \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.twitch.tv/monstercat"}'

# Watch it
ffplay http://localhost:8080/hls/stream.m3u8

# Stop the stream
curl -X DELETE http://localhost:8080/stream
```

---

## Notes

- There is no authentication. Do not expose this service to the public internet.
- Segments are ~2 seconds long. Expect 4–6 seconds of latency on top of the source stream's own delay.
- If the source stream goes offline, the transcoder will exit and the watchdog will restart it into placeholder mode automatically.
- The `best` quality is always selected from the source; the output is then scaled down to 360p by ffmpeg.
