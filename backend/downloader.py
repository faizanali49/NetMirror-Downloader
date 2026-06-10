# downloader.py — HLS segment downloader engine
# Handles playlist parsing, parallel segment download, and local playlist generation for preview.
# Developed by Faizan Ali — https://github.com/faizanali49

import os
import re
import time
import logging
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

log = logging.getLogger('netmirror.downloader')

# Browser-like headers to avoid being blocked by CDN servers
DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://net11.cc/',
    'Origin':  'https://net11.cc/',
}


def get_session(page_url='https://net11.cc/'):
    """Creates a requests session with spoofed Referer/Origin based on the source page URL."""
    session = requests.Session()
    headers = DEFAULT_HEADERS.copy()

    parsed   = urllib.parse.urlparse(page_url)
    base_url = f'{parsed.scheme}://{parsed.netloc}/'

    headers['Referer'] = page_url
    headers['Origin']  = base_url

    session.headers.update(headers)
    return session


# ─────────────────────────────────────────────────────────────────────────────
# HLS Playlist Parser
# ─────────────────────────────────────────────────────────────────────────────

class HLSPlaylistParser:
    """Reads an HLS media playlist text and extracts segment URLs, key info, and target duration."""

    def __init__(self, playlist_url, text):
        self.playlist_url   = playlist_url
        self.text           = text
        self.segments       = []
        self.key_info       = None
        self.target_duration = 10
        self.parse()

    def parse(self):
        lines       = self.text.splitlines()
        current_inf = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith('#EXT-X-TARGETDURATION:'):
                try:
                    self.target_duration = int(line.split(':')[1])
                except Exception:
                    pass

            elif line.startswith('#EXT-X-KEY:'):
                self.key_info = self._parse_key_tag(line)

            elif line.startswith('#EXTINF:'):
                current_inf = line

            elif not line.startswith('#'):
                # This is a segment URL (relative or absolute)
                segment_url = urllib.parse.urljoin(self.playlist_url, line)
                duration    = 5.0

                if current_inf:
                    m = re.search(r'#EXTINF:\s*([0-9.]+)', current_inf)
                    if m:
                        duration = float(m.group(1))

                self.segments.append({
                    'url':           segment_url,
                    'duration':      duration,
                    'original_line': line,
                })
                current_inf = None

    def _parse_key_tag(self, line):
        """Parses #EXT-X-KEY tag into a dict with method, uri, and iv."""
        content      = line[len('#EXT-X-KEY:'):].strip()
        method_match = re.search(r'METHOD=([^,]+)', content)
        uri_match    = re.search(r'URI="([^"]+)"', content) or re.search(r'URI=([^,]+)', content)
        iv_match     = re.search(r'IV=([^,]+)', content)

        if not method_match:
            return None

        method = method_match.group(1)
        uri    = uri_match.group(1) if uri_match else None
        iv     = iv_match.group(1)  if iv_match  else None

        if uri:
            uri = urllib.parse.urljoin(self.playlist_url, uri)

        return {'method': method, 'uri': uri, 'iv': iv, 'raw_line': line}


# ─────────────────────────────────────────────────────────────────────────────
# Single Segment Downloader
# ─────────────────────────────────────────────────────────────────────────────

class SegmentDownloadTask:
    """Downloads a single .ts segment file with automatic retry on failure."""

    def __init__(self, segment_index, url, output_path, session, max_retries=5):
        self.segment_index  = segment_index
        self.url            = url
        self.output_path    = output_path
        self.session        = session
        self.max_retries    = max_retries
        self.bytes_downloaded = 0
        self.success        = False

    def run(self, cancel_check_fn):
        if cancel_check_fn():
            return False

        # Skip if already downloaded and non-empty (supports resume)
        if os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 0:
            self.bytes_downloaded = os.path.getsize(self.output_path)
            self.success = True
            return True

        temp_path = self.output_path + '.tmp'

        for attempt in range(self.max_retries):
            if cancel_check_fn():
                return False

            try:
                with self.session.get(self.url, stream=True, timeout=15) as r:
                    r.raise_for_status()
                    with open(temp_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if cancel_check_fn():
                                return False
                            if chunk:
                                f.write(chunk)
                                self.bytes_downloaded += len(chunk)

                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    if os.path.exists(self.output_path):
                        os.remove(self.output_path)
                    os.rename(temp_path, self.output_path)
                    self.success = True
                    return True

            except Exception as e:
                log.warning(f'Segment {self.segment_index} attempt {attempt+1}/{self.max_retries} failed: {e}')
                time.sleep(1)

            finally:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main Downloader Engine
# ─────────────────────────────────────────────────────────────────────────────

class DownloaderEngine:
    """
    Orchestrates parallel HLS segment download for a video + audio stream pair.
    Tracks progress, speed, and ETA. Supports pause, resume, and cancel.
    """

    def __init__(self, job_id, video_url, audio_url, output_dir,
                 page_url='https://net11.cc/', max_workers=16):
        self.job_id     = job_id
        self.video_url  = video_url
        self.audio_url  = audio_url
        self.output_dir = output_dir
        self.page_url   = page_url
        self.max_workers = max_workers

        self.video_dir = os.path.join(output_dir, 'video')
        self.audio_dir = os.path.join(output_dir, 'audio')

        # Download state
        self.status           = 'queued'
        self.progress         = 0
        self.speed_bytes_sec  = 0.0
        self.eta_seconds      = 0
        self.total_size_bytes = 0
        self.downloaded_bytes = 0
        self.error_message    = ''

        self.lock             = threading.RLock()
        self.cancel_requested = False
        self.pause_requested  = False

        self.video_parser = None
        self.audio_parser = None
        self.downloaded_video_segments = set()
        self.downloaded_audio_segments = set()

        # Rolling 5-second window for speed calculation
        self.speed_history = []

    # ── Control methods ───────────────────────────────────────────────────────

    def request_cancel(self):
        with self.lock:
            self.cancel_requested = True
            if self.status == 'running':
                self.status = 'cancelled'

    def request_pause(self):
        with self.lock:
            self.pause_requested = True
            if self.status == 'running':
                self.status = 'paused'

    def is_cancelled_or_paused(self):
        with self.lock:
            return self.cancel_requested or self.pause_requested

    # ── Stats tracking ────────────────────────────────────────────────────────

    def update_speed_and_eta(self, chunk_bytes):
        with self.lock:
            self.downloaded_bytes += chunk_bytes
            now = time.time()
            self.speed_history.append((now, self.downloaded_bytes))

            # Keep only last 5 seconds of history for speed calculation
            self.speed_history = [(t, b) for t, b in self.speed_history if now - t <= 5.0]

            if len(self.speed_history) >= 2:
                t_first, b_first = self.speed_history[0]
                t_last,  b_last  = self.speed_history[-1]
                time_diff = t_last - t_first
                self.speed_bytes_sec = max(0.0, (b_last - b_first) / time_diff) if time_diff > 0.1 else 0.0
            else:
                self.speed_bytes_sec = 0.0

            # Estimate total size from average segment size
            total_segs = 0
            done_segs  = 0
            if self.video_parser:
                total_segs += len(self.video_parser.segments)
                done_segs  += len(self.downloaded_video_segments)
            if self.audio_parser:
                total_segs += len(self.audio_parser.segments)
                done_segs  += len(self.downloaded_audio_segments)

            if done_segs > 0 and total_segs > 0:
                avg_seg_size        = self.downloaded_bytes / done_segs
                self.total_size_bytes = int(avg_seg_size * total_segs)
                remaining           = max(0, self.total_size_bytes - self.downloaded_bytes)
                self.eta_seconds    = int(remaining / self.speed_bytes_sec) if self.speed_bytes_sec > 50000 else -1
            else:
                self.total_size_bytes = 0
                self.eta_seconds      = -1

            self.progress = int((done_segs / total_segs) * 99) if total_segs > 0 else 0

    def get_speed_text(self):
        s = self.speed_bytes_sec
        if s >= 1024 * 1024:
            return f'{s / (1024 * 1024):.2f} MB/s'
        if s >= 1024:
            return f'{s / 1024:.1f} KB/s'
        return f'{s:.0f} B/s'

    def get_eta_text(self):
        eta = self.eta_seconds
        if eta == -1: return 'Unknown'
        if eta < 0:   return '0s'
        if eta >= 3600:
            return f'{eta // 3600}h {(eta % 3600) // 60}m {eta % 60}s'
        if eta >= 60:
            return f'{eta // 60}m {eta % 60}s'
        return f'{eta}s'

    def get_size_mb(self):
        return round(self.total_size_bytes / (1024 * 1024), 2)

    def get_downloaded_mb(self):
        return round(self.downloaded_bytes / (1024 * 1024), 2)

    # ── Local playlist writer (enables preview during download) ───────────────

    def write_local_playlists(self):
        """Writes local .m3u8 files pointing to segments already on disk.
        Called after each segment finishes so the preview player has something to read."""
        with self.lock:
            if self.video_parser:
                self._write_playlist(self.video_parser, self.downloaded_video_segments, self.video_dir, 'video')
            if self.audio_parser:
                self._write_playlist(self.audio_parser, self.downloaded_audio_segments, self.audio_dir, 'audio')

    def _write_playlist(self, parser, downloaded_set, folder, stream_type):
        os.makedirs(folder, exist_ok=True)
        playlist_path = os.path.join(folder, 'local.m3u8')

        # Only list contiguous segments starting from 0 — HLS requires sequential order
        contiguous = 0
        for i in range(len(parser.segments)):
            if i in downloaded_set:
                contiguous += 1
            else:
                break

        lines = [
            '#EXTM3U',
            '#EXT-X-VERSION:3',
            f'#EXT-X-TARGETDURATION:{parser.target_duration}',
            '#EXT-X-MEDIA-SEQUENCE:0',
        ]

        # Proxy the decryption key URL through our Flask server
        if parser.key_info:
            encoded_key = urllib.parse.quote(parser.key_info['uri'])
            proxy_uri   = f'/jobs/{self.job_id}/key?url={encoded_key}'
            key_line    = f'#EXT-X-KEY:METHOD={parser.key_info["method"]},URI="{proxy_uri}"'
            if parser.key_info.get('iv'):
                key_line += f',IV={parser.key_info["iv"]}'
            lines.append(key_line)

        for i in range(contiguous):
            seg = parser.segments[i]
            lines.append(f'#EXTINF:{seg["duration"]:.3f},')
            lines.append(f'{i}.ts')

        is_done = (contiguous == len(parser.segments)) and (self.status == 'done' or self.progress >= 99)
        if is_done:
            lines.append('#EXT-X-ENDLIST')

        # Write to a temp file first then rename to avoid partial reads by the preview player
        temp = playlist_path + '.tmp'
        try:
            with open(temp, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')

            for attempt in range(5):
                try:
                    os.replace(temp, playlist_path)
                    break
                except Exception:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)  # wait for Windows file lock to release

        except Exception as e:
            log.warning(f'[{self.job_id}] Playlist write deferred: {e}')

    # ── Main download loop ────────────────────────────────────────────────────

    def start_download(self):
        """Fetches playlists, builds segment task list, and runs them in a thread pool."""
        self.status         = 'running'
        self.error_message  = ''
        self.speed_history  = [(time.time(), 0)]

        os.makedirs(self.video_dir, exist_ok=True)
        os.makedirs(self.audio_dir, exist_ok=True)

        session = get_session(self.page_url)

        try:
            log.info(f'[{self.job_id}] Fetching playlists...')

            r_video = session.get(self.video_url, timeout=15)
            r_video.raise_for_status()
            self.video_parser = HLSPlaylistParser(self.video_url, r_video.text)

            r_audio = session.get(self.audio_url, timeout=15)
            r_audio.raise_for_status()
            self.audio_parser = HLSPlaylistParser(self.audio_url, r_audio.text)

            # Build task lists, skipping segments already on disk
            video_tasks = []
            for i, seg in enumerate(self.video_parser.segments):
                seg_path = os.path.join(self.video_dir, f'{i}.ts')
                task     = SegmentDownloadTask(i, seg['url'], seg_path, session)
                video_tasks.append(task)
                if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                    self.downloaded_video_segments.add(i)
                    self.downloaded_bytes += os.path.getsize(seg_path)

            audio_tasks = []
            for i, seg in enumerate(self.audio_parser.segments):
                seg_path = os.path.join(self.audio_dir, f'{i}.ts')
                task     = SegmentDownloadTask(i, seg['url'], seg_path, session)
                audio_tasks.append(task)
                if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                    self.downloaded_audio_segments.add(i)
                    self.downloaded_bytes += os.path.getsize(seg_path)

            self.write_local_playlists()

            # Interleave video and audio tasks so preview starts playing sooner
            all_tasks = []
            max_len = max(len(video_tasks), len(audio_tasks))
            for idx in range(max_len):
                if idx < len(video_tasks) and idx not in self.downloaded_video_segments:
                    all_tasks.append((video_tasks[idx], 'video'))
                if idx < len(audio_tasks) and idx not in self.downloaded_audio_segments:
                    all_tasks.append((audio_tasks[idx], 'audio'))

            if not all_tasks:
                log.info(f'[{self.job_id}] All segments already on disk.')
                self.progress = 100
                self.write_local_playlists()
                return True

            log.info(f'[{self.job_id}] Downloading {len(all_tasks)} segments...')

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {}
                for task, stream_type in all_tasks:
                    future = executor.submit(task.run, self.is_cancelled_or_paused)
                    futures[future] = (task, stream_type)

                for future in as_completed(futures):
                    if self.is_cancelled_or_paused():
                        break

                    task, stream_type = futures[future]
                    try:
                        success = future.result()
                        if success:
                            with self.lock:
                                if stream_type == 'video':
                                    self.downloaded_video_segments.add(task.segment_index)
                                else:
                                    self.downloaded_audio_segments.add(task.segment_index)

                            self.update_speed_and_eta(task.bytes_downloaded)
                            self.write_local_playlists()
                        else:
                            if not self.is_cancelled_or_paused():
                                raise Exception(f'Segment {task.segment_index} ({stream_type}) failed after all retries')

                    except Exception as e:
                        log.error(f'[{self.job_id}] Segment error: {e}')
                        raise

            with self.lock:
                if self.cancel_requested:
                    self.status = 'cancelled'
                    log.info(f'[{self.job_id}] Cancelled.')
                    return False
                elif self.pause_requested:
                    self.status = 'paused'
                    log.info(f'[{self.job_id}] Paused.')
                    return False
                else:
                    self.status   = 'running'
                    self.progress = 99  # will be set to 100 after FFmpeg mux
                    self.write_local_playlists()
                    return True

        except Exception as e:
            with self.lock:
                if not (self.cancel_requested or self.pause_requested):
                    self.status        = 'error'
                    self.error_message = str(e)
                    log.error(f'[{self.job_id}] DownloaderEngine error: {e}', exc_info=True)
            return False
