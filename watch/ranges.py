"""Small time-range extension for the existing watch pipeline, no new Agent/API."""
import math
import os
from pathlib import Path
import subprocess
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx


def requested_range(start_seconds=None, end_seconds=None, *, maximum=180):
    if start_seconds is None and end_seconds is None:
        return None
    values = (start_seconds, end_seconds)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in values):
        raise ValueError("INVALID_VIDEO_TIME_RANGE")
    start, end = (round(float(value), 6) for value in values)
    if start < 0 or end <= start:
        raise ValueError("INVALID_VIDEO_TIME_RANGE")
    if end - start > maximum:
        raise ValueError("VIDEO_RANGE_DURATION_LIMIT")
    return [start, end]


def resolve_range(selection, source_duration, *, maximum=180):
    source_duration = float(source_duration)
    if not math.isfinite(source_duration) or source_duration <= 0:
        raise ValueError("VIDEO_DURATION_UNKNOWN")
    if selection is None:
        if source_duration > maximum:
            raise ValueError("VIDEO_DURATION_LIMIT_SPECIFY_TIME_RANGE")
        return [0.0, source_duration]
    start, end = selection
    if start >= source_duration or end > source_duration + .001:
        raise ValueError("VIDEO_TIME_RANGE_OUTSIDE_SOURCE")
    return [start, min(end, source_duration)]


def coverage_fields(source_duration, time_range, clip_duration):
    start, end = time_range
    full = start == 0 and abs(end - source_duration) <= .001
    uncovered = ([[0.0, start]] if start > 0 else []) + ([[end, source_duration]] if end < source_duration - .001 else [])
    return {"source_duration_seconds": source_duration,
        "source_time_range_seconds": [start, end], "analysis_range_seconds": [start, end],
        "clip_time_range_seconds": [0.0, clip_duration],
        "full_selected_part_covered": full, "uncovered_source_ranges_seconds": uncovered,
        "coverage": "entire_selected_part_file_submitted" if full else "selected_time_range_file_submitted",
        "timestamp_reference": {"model_input_zero_is_source_seconds": start,
            "requested_event_time_basis": "original_selected_part_seconds"}}


def merge_coverage(intervals, source_duration):
    merged = []
    for start, end in sorted(intervals):
        if start < 0 or end > source_duration + .001 or end <= start:
            continue
        if merged and start <= merged[-1][1] + .001:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps, position = [], 0.0
    for start, end in merged:
        if start > position + .001:
            gaps.append([position, start])
        position = max(position, end)
    if position < source_duration - .001:
        gaps.append([position, source_duration])
    return {"covered_ranges_seconds": merged, "uncovered_ranges_seconds": gaps,
        "fully_covered_by_completed_calls": not gaps,
        "basis": "completed_provider_analysis_records_for_this_BV_part_and_model"}


def clean_child_environment():
    return {key: value for key, value in os.environ.items() if key.upper() in
        ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")}


class MediaRangeInputs:
    """Job-local byte streaming, credentials stay in memory rather than FFmpeg argv."""
    def __init__(self, inputs, *, max_bytes, deadline):
        self.inputs, self.max_bytes, self.deadline = inputs, max_bytes, deadline
        self.bytes_read, self.failure = 0, None
        self._lock = threading.Lock()
        self._server, self._thread = None, None
        self._client = None

    def __enter__(self):
        if not any(urlparse(str(item["url"])).scheme in ("https", "http") for item in self.inputs):
            self.urls = [str(item["url"]) for item in self.inputs]
            return self
        token = secrets.token_urlsafe(24)
        parent = self
        self._client = httpx.Client(timeout=httpx.Timeout(15), follow_redirects=False, trust_env=True)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                prefix = "/" + token + "/"
                index = self.path[len(prefix):] if self.path.startswith(prefix) else ""
                if not index.isdigit() or int(index) >= len(parent.inputs):
                    self.send_error(404)
                    return
                item = parent.inputs[int(index)]
                headers = dict(item.get("headers") or {})
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]
                try:
                    parent.deadline()
                    with parent._client.stream("GET", item["url"], headers=headers) as response:
                        if response.status_code not in (200, 206) or (
                                headers.get("Range") and response.status_code != 206):
                            parent.failure = "VIDEO_RANGE_HTTP_FAILURE"
                            self.send_error(502)
                            return
                        self.send_response(response.status_code)
                        for name in ("Content-Length", "Content-Range", "Content-Type", "Accept-Ranges"):
                            if response.headers.get(name):
                                self.send_header(name, response.headers[name])
                        self.end_headers()
                        for chunk in response.iter_bytes(65536):
                            parent.deadline()
                            with parent._lock:
                                parent.bytes_read += len(chunk)
                                if parent.bytes_read > parent.max_bytes:
                                    parent.failure = "VIDEO_RANGE_NETWORK_BYTE_LIMIT"
                                    return
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # FFmpeg legitimately closes old HTTP reads when seeking.
                    pass
                except httpx.HTTPError:
                    parent.failure = "VIDEO_RANGE_NETWORK_FAILURE"
                except Exception:
                    parent.failure = "VIDEO_RANGE_DOWNLOAD_INTERRUPTED"

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="bili-range-inputs")
        self._thread.start()
        port = self._server.server_address[1]
        self.urls = [f"http://127.0.0.1:{port}/{token}/{index}"
            if urlparse(str(item["url"])).scheme in ("http", "https") else str(item["url"])
            for index, item in enumerate(self.inputs)]
        return self

    def __exit__(self, *args):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
        if self._client:
            self._client.close()


def cut_segment(ffmpeg, inputs, directory, time_range, *, timeout, max_bytes, deadline):
    """FFmpeg input seeking fetches a selected range; no full media file is saved.

    Remote headers and signed URLs remain inside a job-local Range reader, so
    cookies/signatures never appear in process arguments or files. Accurate seek
    reencodes only the selected continuous interval, retaining its audio.
    """
    start, end = time_range
    directory = Path(directory)
    output = directory / "media.mp4"
    command = [str(ffmpeg), "-v", "error", "-nostdin", "-y", "-filter_threads", "1"]
    try:
        with MediaRangeInputs(inputs, max_bytes=max_bytes, deadline=deadline) as source:
            for media_url in source.urls:
                command.extend(["-threads", "1", "-rw_timeout", "15000000", "-ss", str(start),
                    "-t", str(end - start), "-i", media_url])
            command.extend(["-map", "0:v:0", "-map", "1:a:0" if len(inputs) == 2 else "0:a:0",
                "-t", str(end - start), "-c:v", "libx264", "-threads:v", "1", "-preset", "ultrafast",
                "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac", "-threads:a", "1", "-b:a", "128k",
                "-fs", str(max_bytes), "-movflags", "+faststart", str(output)])
            deadline()
            result = subprocess.run(command, env=clean_child_environment(), capture_output=True,
                timeout=max(1, timeout), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline()
            if result.returncode or not output.exists():
                # Never expose stderr (which can contain source transport data).
                raise ValueError(source.failure or "VIDEO_RANGE_DOWNLOAD_OR_CUT_FAILED")
            return output
    except subprocess.TimeoutExpired:
        raise ValueError("VIDEO_RANGE_DOWNLOAD_TIMEOUT")
