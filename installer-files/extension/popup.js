// popup.js — Extension popup UI controller
// Handles tab switching, stream card rendering, job list polling, and download submission.
// Developed by Faizan Ali — https://github.com/faizanali49

const API = 'http://127.0.0.1:5000';

document.addEventListener('DOMContentLoaded', async () => {

  // Grab all DOM references up front
  const els = {
    serverPill:    document.getElementById('server-pill'),
    tabPill:       document.getElementById('tab-pill'),
    refreshBtn:    document.getElementById('refresh-btn'),
    clearBtn:      document.getElementById('clear-btn'),
    downloadBtn:   document.getElementById('download-btn'),
    selectionText: document.getElementById('selection-text'),
    metaThumb:     document.getElementById('meta-thumb'),
    metaTitle:     document.getElementById('meta-title'),
    metaSub:       document.getElementById('meta-sub'),
    videoGrid:     document.getElementById('video-grid'),
    audioGrid:     document.getElementById('audio-grid'),
    jobsList:      document.getElementById('jobs-list'),
    networkList:   document.getElementById('network-list'),
  };

  let currentTabId    = null;
  let state           = emptyData();
  let selectedVideo   = null;
  let selectedAudio   = null;
  let jobsTimer       = null;
  let trackedVideoUrl = ''; // tracks which video asset is currently shown so we don't overwrite user title edits

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  currentTabId = tab.id;
  els.tabPill.textContent = `tab ${currentTabId}`;

  // ── Tab switcher ──────────────────────────────────────────────────────────

  document.querySelectorAll('.tab').forEach(tabEl => {
    tabEl.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tabEl));
      document.querySelectorAll('.panel-view').forEach(view => {
        view.classList.toggle('active', view.id === `view-${tabEl.dataset.view}`);
      });
    });
  });

  // ── Button event bindings ─────────────────────────────────────────────────

  els.refreshBtn.addEventListener('click', refreshAll);

  els.clearBtn.addEventListener('click', async () => {
    await chrome.runtime.sendMessage({ type: 'CLEAR_TAB_DATA', tabId: currentTabId });
    selectedVideo   = null;
    selectedAudio   = null;
    trackedVideoUrl = '';
    await refreshAll();
  });

  els.downloadBtn.addEventListener('click', submitDownload);

  // Live-update when background storage changes while popup is open
  chrome.storage.onChanged.addListener(changes => {
    const key = `tab_data_${currentTabId}`;
    if (changes[key]) {
      state = changes[key].newValue || emptyData();
      reconcileSelections();
      renderCapture();
      renderNetwork();
    }
  });

  await refreshAll();
  jobsTimer = setInterval(refreshJobs, 1500);
  window.addEventListener('unload', () => clearInterval(jobsTimer));

  // ── Data refresh ──────────────────────────────────────────────────────────

  async function refreshAll() {
    await Promise.all([refreshCapture(), refreshJobs(), pingServer()]);
  }

  async function refreshCapture() {
    try {
      const res = await chrome.runtime.sendMessage({ type: 'GET_TAB_DATA', tabId: currentTabId });
      state = res?.ok ? res.data : await storageFallback();
    } catch {
      state = await storageFallback();
    }
    reconcileSelections();
    renderCapture();
    renderNetwork();
  }

  // Read directly from chrome.storage if the message bus fails
  async function storageFallback() {
    const key    = `tab_data_${currentTabId}`;
    const stored = await chrome.storage.local.get(key);
    return stored[key] || emptyData();
  }

  async function pingServer() {
    try {
      const res  = await fetch(`${API}/ping`);
      const data = await res.json();
      els.serverPill.textContent = `server ok / ${data.max_concurrent || 3}`;
      els.serverPill.style.color = 'var(--green)';
    } catch {
      els.serverPill.textContent = 'server offline';
      els.serverPill.style.color = 'var(--red)';
    }
  }

  async function refreshJobs() {
    try {
      const res  = await fetch(`${API}/jobs`);
      const data = await res.json();
      renderJobs(data.jobs || []);
    } catch {
      els.jobsList.innerHTML = `<div class="empty">Server offline. Start backend/server.py to monitor downloads.</div>`;
    }
  }

  // ── Render: Capture tab ───────────────────────────────────────────────────

  function renderCapture() {
    const meta = state.meta || {};
    els.metaSub.textContent = meta.episode || meta.url || 'Play a video to capture stream URLs.';

    // Only reset the title field when the user switches to a completely new video
    if (meta.url !== trackedVideoUrl) {
      trackedVideoUrl   = meta.url || '';
      els.metaTitle.value = meta.title || 'No video metadata yet';
    }

    if (meta.thumbnail) {
      els.metaThumb.innerHTML = `<img src="${esc(meta.thumbnail)}" alt="">`;
    } else {
      els.metaThumb.textContent = 'thumb';
    }

    // Video stream cards (sorted highest resolution first)
    const videos = [...(state.videoStreams || [])].sort(sortResolution);
    els.videoGrid.innerHTML = videos.length ? '' : `<div class="empty">No video streams captured yet.</div>`;
    videos.forEach(video => {
      const card = document.createElement('div');
      card.className = `card${selectedVideo?.url === video.url ? ' selected' : ''}`;
      card.innerHTML = `<div class="big">${esc(video.resolution)}</div><div class="small">file ${esc(video.fileId || '')}</div>`;
      card.addEventListener('click', () => {
        selectedVideo = selectedVideo?.url === video.url ? null : video;
        renderCapture();
      });
      els.videoGrid.appendChild(card);
    });

    // Audio track cards
    const audios = state.audioTracks || [];
    els.audioGrid.innerHTML = audios.length ? '' : `<div class="empty">Generated audio URLs appear after a video stream is parsed.</div>`;
    audios.forEach(audio => {
      const card = document.createElement('div');
      card.className = `card${selectedAudio?.index === audio.index ? ' selected' : ''}`;
      card.innerHTML = `<div class="big">#${audio.index}</div><div class="small">${esc(audio.name || `Audio ${audio.index}`)}</div>`;
      card.addEventListener('click', () => {
        selectedAudio = selectedAudio?.index === audio.index ? null : audio;
        renderCapture();
      });
      els.audioGrid.appendChild(card);
    });

    updateSelection();
  }

  // ── Render: Network tab ───────────────────────────────────────────────────

  function renderNetwork() {
    const streams = [...(state.streams || [])].sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
    els.networkList.innerHTML = streams.length ? '' : `<div class="empty">No network media URLs captured yet.</div>`;
    streams.forEach(stream => {
      const item = document.createElement('div');
      item.className = 'stream';
      item.textContent = stream.url;
      els.networkList.appendChild(item);
    });
  }

  // ── Render: Jobs/Downloads tab ────────────────────────────────────────────

  function renderJobs(jobs) {
    els.jobsList.innerHTML = jobs.length ? '' : `<div class="empty">No downloads yet.</div>`;
    jobs.forEach(job => {
      const row    = document.createElement('div');
      row.className = 'job';

      const thumb = job.thumbnail
        ? `<img src="${esc(job.thumbnail)}" alt="">`
        : `<div class="thumb" style="display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:9px;font-weight:600;background:linear-gradient(135deg,#1e293b,#0f172a);">thumb</div>`;

      const statusPill = `<span class="status ${esc(job.status)}">${esc(job.status)}</span>`;

      // Build action buttons based on current job status
      let actionButtons = '';
      if (job.status === 'running') {
        actionButtons = `
          <button class="btn amber" data-action="pause">Pause</button>
          <button class="btn red"   data-action="cancel">Cancel</button>
          <button class="btn primary" data-action="preview">Play Preview</button>`;
      } else if (job.status === 'paused') {
        actionButtons = `
          <button class="btn green" data-action="resume">Resume</button>
          <button class="btn red"   data-action="cancel">Cancel</button>
          <button class="btn primary" data-action="preview">Play Preview</button>`;
      } else if (job.status === 'queued') {
        actionButtons = `
          <button class="btn amber" data-action="pause">Pause</button>
          <button class="btn red"   data-action="cancel">Cancel</button>`;
      } else if (job.status === 'error') {
        actionButtons = `
          <button class="btn green" data-action="resume">Resume</button>
          <button class="btn"       data-action="retry">Retry</button>
          <button class="btn red"   data-action="cancel">Cancel</button>`;
      } else if (job.status === 'cancelled') {
        actionButtons = `<button class="btn" data-action="retry">Retry</button>`;
      } else if (job.status === 'done') {
        actionButtons = `<button class="btn primary" data-action="preview">Play Video</button>`;
      }

      const errorSub = (job.status === 'error' && job.error)
        ? `<div class="sub" style="color:var(--red);font-weight:600;white-space:normal;overflow:visible;text-overflow:clip;max-width:none;margin-top:6px;">Error: ${esc(job.error)}</div>`
        : '';

      row.innerHTML = `
        ${thumb}
        <div style="flex:1;overflow:hidden;">
          <div class="status-row">
            <div class="job-title" title="${esc(job.title)}">${esc(job.title || 'NetMirror Video')}</div>
            ${statusPill}
          </div>
          <div class="sub">${esc(job.episode || job.filename || job.id)}</div>
          <div class="bar"><div class="fill" style="width:${Number(job.progress || 0)}%"></div></div>
          <div class="metrics-row">
            <div class="metric">
              <span class="metric-label">Progress</span>
              <span class="metric-value" style="color:var(--blue);">${Number(job.progress || 0)}%</span>
            </div>
            <div class="metric">
              <span class="metric-label">Downloaded</span>
              <span class="metric-value">${Number(job.downloaded_mb || 0).toFixed(1)} / ${Number(job.size_mb || 0).toFixed(1)} MB</span>
            </div>
            <div class="metric">
              <span class="metric-label">Speed</span>
              <span class="metric-value">${esc(job.speed || '-')}</span>
            </div>
            <div class="metric">
              <span class="metric-label">ETA</span>
              <span class="metric-value">${esc(job.eta || '-')}</span>
            </div>
          </div>
          <div class="meta-details">
            <span>${esc(job.resolution || '')}</span>
            <span class="divider"></span>
            <span>${esc(job.audio_label || '')}</span>
          </div>
          ${errorSub}
          <div class="job-actions">${actionButtons}</div>
        </div>`;

      // Wire up job control buttons
      row.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', () => {
          const action = btn.dataset.action;
          if (action === 'preview') {
            window.open(`${API}/preview/${job.id}`, '_blank');
          } else {
            controlJob(job.id, action);
          }
        });
      });

      els.jobsList.appendChild(row);
    });
  }

  // ── Download submission ───────────────────────────────────────────────────

  async function submitDownload() {
    if (!selectedVideo || !selectedAudio) return;
    const meta        = state.meta || {};
    const customTitle = els.metaTitle.value.trim() || meta.title || 'NetMirror Video';

    els.downloadBtn.disabled = true;
    try {
      console.log('[DOWNLOAD STARTED]', selectedVideo.url, selectedAudio.derivedUrl);
      await fetch(`${API}/merge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_url:   selectedVideo.url,
          audio_url:   selectedAudio.derivedUrl,
          title:       customTitle,
          episode:     meta.episode || '',
          thumbnail:   meta.thumbnail || '',
          resolution:  selectedVideo.resolution || '',
          audio_label: selectedAudio.name || `Audio ${selectedAudio.index}`,
        }),
      });
      await refreshJobs();
      activateView('jobs');
    } catch (e) {
      els.selectionText.innerHTML = `<b>Server error:</b> ${esc(e.message)}`;
    } finally {
      updateSelection();
    }
  }

  async function controlJob(jobId, action) {
    try {
      await fetch(`${API}/jobs/${jobId}/${action}`, { method: 'POST' });
      await refreshJobs();
    } catch {
      await pingServer();
    }
  }

  // ── Utilities ─────────────────────────────────────────────────────────────

  function activateView(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.view === name));
    document.querySelectorAll('.panel-view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
  }

  // Drop selections if the underlying stream/track was removed from state
  function reconcileSelections() {
    const videos = state.videoStreams || [];
    const audios = state.audioTracks  || [];
    if (selectedVideo && !videos.some(v => v.url   === selectedVideo.url))   selectedVideo = null;
    if (selectedAudio && !audios.some(a => a.index === selectedAudio.index)) selectedAudio = null;
  }

  function updateSelection() {
    if (selectedVideo && selectedAudio) {
      els.selectionText.innerHTML = `<b>${esc(selectedVideo.resolution)}</b> + <b>${esc(selectedAudio.name || `Audio ${selectedAudio.index}`)}</b>`;
      els.downloadBtn.disabled = false;
    } else {
      els.selectionText.textContent = 'Select one video stream and one generated audio track.';
      els.downloadBtn.disabled = true;
    }
  }

  // Sort cards highest resolution first (handles "4k", "1080p", "720p", etc.)
  function sortResolution(a, b) {
    const rank = value => {
      if (!value) return 0;
      if (String(value).toLowerCase() === '4k') return 4000;
      return Number(String(value).match(/\d+/)?.[0] || 0);
    };
    return rank(b.resolution) - rank(a.resolution);
  }

  function emptyData() {
    return { streams: [], videoStreams: [], audioTracks: [], qualityTracks: [], meta: {} };
  }

  // Basic HTML escaping to prevent XSS from raw URLs or titles
  function esc(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
});
