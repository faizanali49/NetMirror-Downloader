
import sys
import os
import re
import sys
import time
import uuid
import queue
import signal
import logging
import threading
import subprocess
import shutil
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Optional, Any

import requests
from flask import Flask, jsonify, request, send_from_directory, render_template_string
from downloader import DownloaderEngine, DEFAULT_HEADERS

# 1. Determine base path location for core asset tracking
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Ensure FFmpeg binary path location can be found smoothly by system calls
os.environ["PATH"] += os.pathsep + APP_DIR

# 2. Local Storage Directories Setup
USER_HOME = os.path.expanduser("~")

# Temporary segments storage (keeps runtime trash folders out of user sight)
DOWNLOAD_DIR = os.path.join(USER_HOME, "AppData", "Local", "NetMirrorTemp")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Final Video Output Destination (User-friendly downloads section folder)
FINAL_OUTPUT_DIR = os.path.join(USER_HOME, "Downloads", "NetMirror Videos")
os.makedirs(FINAL_OUTPUT_DIR, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = 3
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("netmirror")
app = Flask(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


def clean_filename(value: str, fallback: str) -> str:
    value = (value or fallback).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:120] or fallback


def parse_ffmpeg_progress(line: str) -> Dict[str, object]:
    progress = {}
    time_match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    speed_match = re.search(r"speed=\s*([0-9.]+x)", line)
    fps_match = re.search(r"fps=\s*([0-9.]+)", line)

    if time_match:
        hours, minutes, seconds = time_match.groups()
        progress["time_seconds"] = (
            int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        )
        progress["time_text"] = f"{hours}:{minutes}:{seconds}"
    if speed_match:
        progress["speed"] = speed_match.group(1)
    if fps_match:
        progress["fps"] = fps_match.group(1)
    return progress


def build_ffmpeg_command(video_url: str, audio_url: str, output_path: str):
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    headers = "Referer: https://net11.cc/\r\nOrigin: https://net11.cc/\r\n"
    
    # ADVANCED NETWORK OPTIMIZATION FLAGS
    input_flags = [
        "-extension_picky", "0",
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,crypto,data,http,https,tls,tcp",
        "-user_agent", user_agent,
        "-headers", headers,
        
        # Performance & Speed Boosts
        "-http_persistent", "1",          # Keep HTTP connections open to reduce handshake overhead
        "-multiple_requests", "1",        # Allow multiple requests over a single connection
        "-reconnect", "1",                # Enable auto-reconnect if the server drops chunks
        "-reconnect_at_eof", "1",          # Force reconnect at end-of-file anomalies
        "-reconnect_streamed", "1",       # Reconnect if stream connection halts
        "-reconnect_delay_max", "2",      # Limit retry backoff delay to 2 seconds
        "-rw_timeout", "10000000",        # Socket read/write timeout in microseconds (10s)
    ]
    
    return [
        "ffmpeg", "-y",
        "-threads", "0",                  # Force FFmpeg to use all available CPU cores
        *input_flags, "-i", video_url,
        *input_flags, "-i", audio_url,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",                   # Keeps video copy fast (no transcode)
        
        # SPEED OPTIMIZATION FOR AUDIO:
        # Instead of transcoding to aac ("-c:a", "aac"), copy the stream directly 
        # if the source is already AAC/MP3. This saves massive CPU overhead.
        "-c:a", "copy",                   # Change from "aac" to "copy" if streams support it
        
        "-strict", "experimental",
        "-movflags", "+faststart",
        output_path,
    ]


@dataclass
class DownloadJob:
    id: str
    video_url: str
    audio_url: str
    title: str
    episode: str
    audio_label: str
    resolution: str
    thumbnail: str
    output_path: str
    filename: str
    page_url: str = "https://net11.cc/"
    status: str = "queued"
    progress: int = 0
    speed: str = "-"
    eta: str = "-"
    fps: str = "-"
    time_text: str = "0:00:00"
    size_mb: float = 0.0
    downloaded_mb: float = 0.0
    error: str = ""
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)
    started_at: Optional[int] = None
    finished_at: Optional[int] = None
    attempts: int = 0
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    cancel_requested: bool = False
    pause_requested: bool = False

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "episode": self.episode,
            "audio_label": self.audio_label,
            "resolution": self.resolution,
            "thumbnail": self.thumbnail,
            "filename": self.filename,
            "path": self.output_path,
            "status": self.status,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "fps": self.fps,
            "time_text": self.time_text,
            "size_mb": self.size_mb,
            "downloaded_mb": self.downloaded_mb,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": self.attempts,
        }


class DownloadManager:
    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self.jobs: Dict[str, DownloadJob] = {}
        self.engines: Dict[str, DownloaderEngine] = {}
        self.pending = queue.Queue()
        self.lock = threading.RLock()
        self.shutdown = False
        self.dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self.dispatcher.start()

    def create_job(self, payload: dict) -> DownloadJob:
        video_url = (payload.get("video_url") or "").strip()
        audio_url = (payload.get("audio_url") or "").strip()
        if not video_url or not audio_url:
            raise ValueError("Both video_url and audio_url are required")

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        job_id = uuid.uuid4().hex[:12]
        title = clean_filename(payload.get("title"), "NetMirror Video")
        episode = clean_filename(payload.get("episode"), "")
        audio_label = payload.get("audio_label") or "Audio"
        resolution = payload.get("resolution") or ""
        thumbnail = payload.get("thumbnail") or ""
        suffix = f" - {episode}" if episode else ""
        filename = clean_filename(f"{title}{suffix} [{audio_label}] {job_id}.mp4", f"merged_{job_id}.mp4")
        output_path = os.path.join(DOWNLOAD_DIR, filename)
        page_url = payload.get("page_url", "https://net11.cc/")
        job = DownloadJob(
            id=job_id,
            video_url=video_url,
            audio_url=audio_url,
            title=title,
            episode=episode,
            audio_label=audio_label,
            resolution=resolution,
            thumbnail=thumbnail,
            output_path=output_path,
            filename=filename,
            page_url=page_url,
        )
        with self.lock:
            self.jobs[job.id] = job
            self.pending.put(job.id)
        log.info("[DOWNLOAD QUEUED] %s %s", job.id, filename)
        return job

    def list_jobs(self):
        with self.lock:
            return sorted(
                [job.to_dict() for job in self.jobs.values()],
                key=lambda item: item["created_at"],
                reverse=True,
            )

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        with self.lock:
            return self.jobs.get(job_id) 
        Schnaps

    def pause_job(self, job_id: str) -> DownloadJob:
        job = self._require_job(job_id)
        with self.lock:
            if job.status == "queued":
                job.status = "paused"
                job.updated_at = now_ms()
                log.info("[PAUSED / RESUMED] %s queued job paused", job.id)
                return job
            if job.status != "running":
                return job
            job.pause_requested = True
            engine = self.engines.get(job_id)
            if engine:
                engine.request_pause()
            
            # If FFmpeg remux is running, terminate it
            self._terminate_process(job)
            
            job.status = "paused"
            job.updated_at = now_ms()
        log.info("[PAUSED / RESUMED] %s engine and processes paused", job.id)
        return job

    def resume_job(self, job_id: str) -> DownloadJob:
        job = self._require_job(job_id)
        with self.lock:
            if job.status not in ("paused", "error"):
                return job
            job.status = "queued"
            job.error = ""
            job.pause_requested = False
            job.cancel_requested = False
            job.updated_at = now_ms()
            self.pending.put(job.id)
        log.info("[PAUSED / RESUMED] %s queued for restart", job.id)
        return job

    def retry_job(self, job_id: str) -> DownloadJob:
        job = self._require_job(job_id)
        with self.lock:
            if job.status not in ("error", "cancelled", "done", "paused"):
                return job
            job.status = "queued"
            job.progress = 0
            job.speed = "-"
            job.eta = "-"
            job.downloaded_mb = 0.0
            job.size_mb = 0.0
            job.error = ""
            job.cancel_requested = False
            job.pause_requested = False
            job.updated_at = now_ms()
            self.pending.put(job.id)
        log.info("[PAUSED / RESUMED] %s retry queued", job.id)
        return job

    def cancel_job(self, job_id: str) -> DownloadJob:
        job = self._require_job(job_id)
        with self.lock:
            job.cancel_requested = True
            engine = self.engines.get(job_id)
            if engine:
                engine.request_cancel()
            self._terminate_process(job)
            job.status = "cancelled"
            job.updated_at = now_ms()
            
            # Clean up temporary downloads directory
            temp_dir = os.path.join(DOWNLOAD_DIR, f"temp_{job_id}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            
        log.info("[PAUSED / RESUMED] %s cancelled", job.id)
        return job

    def _require_job(self, job_id: str) -> DownloadJob:
        job = self.get_job(job_id)
        if not job:
            raise KeyError(f"Unknown job: {job_id}")
        return job

    def _running_count(self) -> int:
        return sum(1 for job in self.jobs.values() if job.status == "running")

    def _dispatch_loop(self):
        while not self.shutdown:
            try:
                job_id = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue

            while not self.shutdown:
                with self.lock:
                    job = self.jobs.get(job_id)
                    can_start = job and job.status == "queued" and self._running_count() < self.max_workers
                if not job or job.status != "queued":
                    break
                if can_start:
                    threading.Thread(target=self._run_job, args=(job_id,), daemon=True).start()
                    break
                time.sleep(0.25)

    def _run_job(self, job_id: str):
        job = self.get_job(job_id)
        if not job:
            return

        with self.lock:
            if job.status != "queued":
                return
            job.status = "running"
            job.started_at = now_ms()
            job.updated_at = job.started_at
            job.attempts += 1
            job.pause_requested = False
            job.cancel_requested = False

        log.info("[DOWNLOAD STARTED] %s using multi-threaded downloader", job.id)
        temp_dir = os.path.join(DOWNLOAD_DIR, f"temp_{job.id}")
        engine = DownloaderEngine(job.id, job.video_url, job.audio_url, temp_dir, job.page_url, max_workers=16)
        
        with self.lock:
            self.engines[job.id] = engine

        # Start download in worker thread
        download_thread = threading.Thread(target=engine.start_download, daemon=True)
        download_thread.start()

        # Poll downloader status and update job details
        try:
            while download_thread.is_alive():
                time.sleep(0.4)
                with self.lock:
                    job.progress = engine.progress
                    job.speed = engine.get_speed_text()
                    job.eta = engine.get_eta_text()
                    job.size_mb = engine.get_size_mb()
                    job.downloaded_mb = engine.get_downloaded_mb()
                    job.updated_at = now_ms()
                    
                    if engine.status == "paused":
                        job.status = "paused"
                        break
                    elif engine.status == "cancelled":
                        job.status = "cancelled"
                        break
                    elif engine.status == "error":
                        job.status = "error"
                        job.error = engine.error_message
                        break

            download_thread.join()

            # Inspect outcomes
            with self.lock:
                status = engine.status
                err_msg = engine.error_message

            if status == "paused":
                log.info("[DOWNLOAD PAUSED] %s", job.id)
                return
            elif status == "cancelled":
                log.info("[DOWNLOAD CANCELLED] %s", job.id)
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            elif status == "error":
                log.error("[DOWNLOAD ERROR] %s: %s", job.id, err_msg)
                with self.lock:
                    job.status = "error"
                    job.error = err_msg
                return

            # Download complete -> Proceed to local FFmpeg muxing
            log.info("[DOWNLOAD COMPLETE - REMUXING] %s", job.id)
            with self.lock:
                job.speed = "Muxing (0%)"
                job.progress = 99
                job.updated_at = now_ms()

            local_video_playlist = os.path.join(temp_dir, "video", "local.m3u8")
            local_audio_playlist = os.path.join(temp_dir, "audio", "local.m3u8")
            
            # Construct local remuxing command
            cmd = [
                "ffmpeg", "-y",
                "-protocol_whitelist", "file,crypto,data,http,https,tls,tcp",
                "-i", local_video_playlist,
                "-i", local_audio_playlist,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-movflags", "+faststart",
                job.output_path,
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            with self.lock:
                job.process = process

            log.info(f"[{job.id}] Streaming FFmpeg live console output:")

            # Real-time line-by-line log streaming & parsing loop
            for line in process.stdout:
                clean_line = line.strip()
                if clean_line:
                    print(f"   └── [FFmpeg {job.id}]: {clean_line}", flush=True)
                    
                    progress_info = parse_ffmpeg_progress(clean_line)
                    if progress_info:
                        with self.lock:
                            if "time_text" in progress_info:
                                job.time_text = progress_info["time_text"]
                            if "speed" in progress_info:
                                job.speed = f"Muxing ({progress_info['speed']})"
                            if "fps" in progress_info:
                                job.fps = progress_info["fps"]
                            job.updated_at = now_ms()

            rc = process.wait()
            with self.lock:
                job.process = None

            if job.cancel_requested:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return

            if rc == 0 and os.path.exists(job.output_path):
                size_mb = os.path.getsize(job.output_path) / (1024 * 1024)
                with self.lock:
                    job.status = "done"
                    job.progress = 100
                    job.size_mb = round(size_mb, 2)
                    job.downloaded_mb = round(size_mb, 2)
                    job.finished_at = now_ms()
                    job.updated_at = job.finished_at
                
                # Delete HLS temporary segments directory
                shutil.rmtree(temp_dir, ignore_errors=True)
                log.info("[REMUX SUCCESS] %s merged into final MP4: %.2f MB", job.id, size_mb)
            else:
                with self.lock:
                    job.status = "error"
                    job.error = f"FFmpeg local remux failed (code {rc})"
                    job.updated_at = now_ms()
                log.error("[REMUX ERROR] %s FFmpeg remux failed with code %s.", job.id, rc)

        except Exception as exc:
            with self.lock:
                job.status = "error"
                job.error = str(exc)
                job.updated_at = now_ms()
            log.exception("[ERROR] %s unexpected download/mux failure", job.id)


    def _terminate_process(self, job: DownloadJob):
        process = job.process
        if not process or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception as exc:
            log.error("[ERROR] %s terminate failed: %s", job.id, exc)


manager = DownloadManager(MAX_CONCURRENT_DOWNLOADS)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return response


@app.route("/merge", methods=["OPTIONS"])
@app.route("/jobs/<job_id>/<action>", methods=["OPTIONS"])
def preflight(job_id=None, action=None):
    return "", 200


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({
        "status": "ok",
        "server": "NetMirror Mux Server v3",
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
    }), 200


@app.route("/merge", methods=["POST"])
def merge():
    payload = request.get_json(silent=True) or {}
    try:
        job = manager.create_job(payload)
        return jsonify({"message": "Job queued", "job": job.to_dict(), "job_id": job.id}), 202
    except ValueError as exc:
        log.error("[ERROR] bad merge request: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("[ERROR] unexpected merge request failure")
        return jsonify({"error": str(exc)}), 500


@app.route("/jobs", methods=["GET"])
def jobs():
    return jsonify({"jobs": manager.list_jobs(), "max_concurrent": MAX_CONCURRENT_DOWNLOADS}), 200


@app.route("/jobs/<job_id>", methods=["GET"])
def job_detail(job_id):
    job = manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": job.to_dict()}), 200


@app.route("/jobs/<job_id>/<action>", methods=["POST"])
def job_action(job_id, action):
    try:
        if action == "pause":
            job = manager.pause_job(job_id)
        elif action == "resume":
            job = manager.resume_job(job_id)
        elif action == "retry":
            job = manager.retry_job(job_id)
        elif action == "cancel":
            job = manager.cancel_job(job_id)
        else:
            return jsonify({"error": "Unknown action"}), 400
        return jsonify({"job": job.to_dict()}), 200
    except KeyError:
        return jsonify({"error": "Job not found"}), 404
    except Exception as exc:
        log.exception("[ERROR] job action failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/jobs/<job_id>/key", methods=["GET"])
def key_proxy(job_id):
    url = request.args.get("url")
    if not url:
        return "Missing url parameter", 400
    try:
        job = manager.get_job(job_id)
        page_url = job.page_url if job else "https://net11.cc/"
        
        headers = DEFAULT_HEADERS.copy()
        parsed = urllib.parse.urlparse(page_url)
        headers["Referer"] = page_url
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}/"

        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        response = app.make_response(r.content)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Content-Type"] = "application/octet-stream"
        return response
    except Exception as e:
        log.error(f"Failed to fetch decryption key for job {job_id}: {e}")
        return str(e), 500


@app.route("/stream/<job_id>/master.m3u8", methods=["GET"])
def stream_master(job_id):
    job = manager.get_job(job_id)
    if not job:
        return "Job not found", 404
        
    temp_dir = os.path.join(DOWNLOAD_DIR, f"temp_{job_id}")
    has_video = os.path.exists(os.path.join(temp_dir, "video", "local.m3u8"))
    has_audio = os.path.exists(os.path.join(temp_dir, "audio", "local.m3u8"))
    
    if not has_video:
        return "Preview not ready yet", 404
        
    lines = []
    lines.append("#EXTM3U")
    if has_audio:
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,URI="/stream/{job_id}/audio.m3u8"')
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH=5000000,AUDIO="audio"')
    else:
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH=5000000')
        
    lines.append(f"/stream/{job_id}/video.m3u8")
    
    response = app.make_response("\n".join(lines) + "\n")
    response.headers["Content-Type"] = "application/x-mpegURL"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/stream/<job_id>/<stream_type>.m3u8", methods=["GET"])
def stream_playlist(job_id, stream_type):
    if stream_type not in ("video", "audio"):
        return "Invalid stream type", 400
        
    playlist_path = os.path.join(DOWNLOAD_DIR, f"temp_{job_id}", stream_type, "local.m3u8")
    if not os.path.exists(playlist_path):
        return "Playlist not found or download not started yet", 404
        
    with open(playlist_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    response = app.make_response(content)
    response.headers["Content-Type"] = "application/x-mpegURL"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/stream/<job_id>/<stream_type>/<segment_file>", methods=["GET"])
def stream_segment(job_id, stream_type, segment_file):
    if stream_type not in ("video", "audio"):
        return "Invalid stream type", 400
    if not re.match(r"^\d+\.ts$", segment_file):
        return "Invalid segment file", 400
        
    dir_path = os.path.join(DOWNLOAD_DIR, f"temp_{job_id}", stream_type)
    response = send_from_directory(dir_path, segment_file)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/preview/<job_id>", methods=["GET"])
def preview_page(job_id):
    job = manager.get_job(job_id)
    if not job:
        return "Job not found", 404
        
    template_path = os.path.join(os.path.dirname(__file__), "templates", "preview.html")
    if not os.path.exists(template_path):
        return "Preview template not found", 500
        
    with open(template_path, "r", encoding="utf-8") as f:
        template_html = f.read()
        
    return render_template_string(template_html, job=job.to_dict())


if __name__ == "__main__":
    log.info("NetMirror Mux Server starting on http://%s:%s", SERVER_HOST, SERVER_PORT)
    log.info("Max concurrent downloads: %s", MAX_CONCURRENT_DOWNLOADS)
    log.info("Press Ctrl+C to stop")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)