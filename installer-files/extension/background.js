// background.js — Service Worker (core brain of the extension)
// Handles network interception, storage, and message routing.
// Works with both net11.cc and net52.cc URL formats.

const LOG = (...a) => console.log('[NM:bg]', ...a);
const ERR = (...a) => console.error('[NM:bg]', ...a);

// ── URL helpers ───────────────────────────────────────────────────────────────

// Generates a video URL for a given resolution by replacing the resolution segment
function generateVideoUrlForResolution(originalUrl, newResolution) {
  const match = originalUrl.match(
    /^(https?:\/\/[^/]+)\/files\/([^/]+)\/([^/]+)\/[^/?]+\.m3u8(.*)$/
  );
  if (!match) return null;
  const [, base, fileId, , rest] = match;
  return `${base}/files/${fileId}/${newResolution}/${newResolution}.m3u8${rest}`;
}

// ── Storage helpers ───────────────────────────────────────────────────────────

function tabKey(id) { return `tab_data_${id}`; }

// Default empty state for a tab
function emptyTabData() {
  return {
    streams:        [],
    videoStreams:   [],
    audioTracks:   [],
    rawAudioTracks: [],
    qualityTracks:  [],
    meta:           {},
    currentFileId:  null,
    lastUpdated:    null,
    pageUrl:        '',
  };
}

async function getTabData(tabId) {
  const r = await chrome.storage.local.get(tabKey(tabId));
  return r[tabKey(tabId)] || emptyTabData();
}

async function setTabData(tabId, data) {
  data.lastUpdated = Date.now();
  await chrome.storage.local.set({ [tabKey(tabId)]: data });
}

async function clearTabData(tabId) {
  await chrome.storage.local.remove(tabKey(tabId));
  LOG(`Tab ${tabId} cleared`);
}

// ── URL filter helpers ────────────────────────────────────────────────────────

// Skip internal, tracker, and TS segment URLs — only care about playlist files
function shouldSkip(url) {
  return (
    url.startsWith('chrome-extension://') ||
    url.includes('127.0.0.1') ||
    url.includes('localhost') ||
    url.includes('google-analytics') ||
    url.includes('doubleclick') ||
    /\/\d+\.ts(\?|$)/.test(url) ||
    url.endsWith('.ts')
  );
}

// Extracts base, fileId, resolution from a known video URL pattern ending with ::kp
function parseKpVideoUrl(url) {
  if (!url.endsWith('::kp')) return null;

  const match = url.match(
    /^(https?:\/\/[^/]+)\/files\/([A-Za-z0-9]+)\/([^/]+)\/[^/?]+\.m3u8/
  );
  if (!match) return null;

  const [, base, fileId, resolution] = match;

  if (!/^\d{3,4}p$/i.test(resolution) && !/^4k$/i.test(resolution)) return null;

  return { base, fileId, resolution };
}

function getQuery(url) {
  const match = url.match(/(\?.*)$/);
  return match ? match[1] : '';
}

function isMediaUrl(url) {
  const l = url.toLowerCase();
  return (
    l.includes('.m3u8') ||
    l.includes('.mpd')  ||
    l.includes('.mp4')  ||
    l.includes('.m4a')  ||
    l.includes('.aac')
  );
}

// Derives the audio track URL from file host, fileId, track index, and query token
function deriveAudioUrl(base, fileId, trackIndex, queryToken = '') {
  return `${base}/files/${fileId}/a/${trackIndex}/${trackIndex}.m3u8${queryToken}`;
}

// Generates 6 fallback audio track entries when no real tracks are available
function generatedAudioTracks(base, fileId, queryToken = '', count = 6) {
  return Array.from({ length: count }, (_, index) => ({
    index,
    name:       `Audio ${index}`,
    ariaLabel:  `Audio ${index}`,
    isFallback: true,
    derivedUrl: deriveAudioUrl(base, fileId, index, queryToken),
  }));
}

// ── Deduplication ─────────────────────────────────────────────────────────────

// Add a stream URL only once, cap list at 300 entries
function dedupeByUrl(arr, newItem) {
  if (arr.some(x => x.url === newItem.url)) return arr;
  const updated = [...arr, newItem];
  if (updated.length > 300) updated.shift();
  return updated;
}

// Replace existing resolution entry instead of duplicating
function dedupeVideoStream(arr, newItem) {
  const idx = arr.findIndex(x => x.resolution === newItem.resolution);
  if (idx === -1) return [...arr, newItem];
  const updated = [...arr];
  updated[idx] = newItem;
  return updated;
}

// ── Network interception ──────────────────────────────────────────────────────

chrome.webRequest.onBeforeRequest.addListener(
  async ({ url, tabId }) => {
    if (tabId < 0 || shouldSkip(url)) return;
    if (!isMediaUrl(url)) return;

    try {
      const data  = await getTabData(tabId);
      let changed = false;

      const prevLen = data.streams.length;
      data.streams  = dedupeByUrl(data.streams, { url, timestamp: Date.now() });
      if (data.streams.length !== prevLen) changed = true;

      const kp = parseKpVideoUrl(url);
      if (kp) {
        const queryToken = getQuery(url);

        data.currentFileId = kp.fileId;
        data.videoStreams   = dedupeVideoStream(data.videoStreams, {
          url,
          resolution: kp.resolution,
          fileId:     kp.fileId,
          base:       kp.base,
          timestamp:  Date.now(),
        });

        const hasRealTracks = data.audioTracks?.length > 0
                           && data.audioTracks.some(t => t.isReal);

        if (hasRealTracks) {
          // Re-derive audio URLs when the video fileId changes (e.g. episode switch)
          data.audioTracks = data.audioTracks.map(track => ({
            ...track,
            isReal:     true,
            derivedUrl: deriveAudioUrl(kp.base, kp.fileId, track.index, queryToken),
          }));
          LOG(`[${tabId}] Re-derived audio URLs for ${data.audioTracks.length} real tracks`);
        } else if (data.rawAudioTracks?.length > 0) {
          // Promote raw (scraped-before-video) tracks now that we have a video context
          data.audioTracks = data.rawAudioTracks.map(track => ({
            index:      track.index,
            name:       track.name,
            ariaLabel:  track.ariaLabel,
            isReal:     true,
            derivedUrl: deriveAudioUrl(kp.base, kp.fileId, track.index, queryToken),
          }));
          LOG(`[${tabId}] Promoted ${data.audioTracks.length} raw tracks → real`);
          data.rawAudioTracks = [];
        } else if (!data.audioTracks?.length || data.audioTracks.every(t => t.isFallback)) {
          // No real tracks yet — generate numbered fallbacks so UI stays usable
          data.audioTracks = generatedAudioTracks(kp.base, kp.fileId, queryToken);
          LOG(`[${tabId}] Generated ${data.audioTracks.length} fallback audio tracks`);
        }

        changed = true;
      }

      if (changed) await setTabData(tabId, data);
    } catch (e) {
      ERR('webRequest interception failure:', e);
    }
  },
  { urls: ['<all_urls>'] }
);

// ── Message router ────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tid    = sender.tab?.id ?? msg.tabId;
  const handle = fn => { fn(); return true; };

  switch (msg.type) {

    case 'PAGE_META_CAPTURED':
      return handle(async () => {
        try {
          const data   = await getTabData(tid);
          data.meta    = msg.meta || {};
          data.pageUrl = msg.meta?.url || msg.url || data.pageUrl;
          await setTabData(tid, data);
          sendResponse({ ok: true });
        } catch (e) { sendResponse({ ok: false }); }
      });

    case 'AUDIO_TRACKS_CAPTURED':
      return handle(async () => {
        try {
          const data = await getTabData(tid);

          if (msg.tracks && Array.isArray(msg.tracks) && msg.tracks.length > 0) {
            // Use the most recently seen video stream as the reference for URL derivation
            const refVideo = data.videoStreams?.length > 0
              ? [...data.videoStreams].sort((a, b) => b.timestamp - a.timestamp)[0]
              : null;

            if (refVideo) {
              const queryToken = getQuery(refVideo.url);
              data.audioTracks = msg.tracks.map(track => ({
                index:      track.index,
                name:       track.name,
                ariaLabel:  track.ariaLabel,
                isReal:     true,
                derivedUrl: deriveAudioUrl(refVideo.base, refVideo.fileId, track.index, queryToken),
              }));
              LOG(`[${tid}] Audio tracks stored (${data.audioTracks.length} tracks)`);
            } else {
              // Park them — they'll be promoted when the first video URL arrives
              data.rawAudioTracks = msg.tracks.map(t => ({ ...t, isReal: true }));
              LOG(`[${tid}] Audio tracks parked (no video context yet)`);
            }
          }

          data.pageUrl = msg.url  || data.pageUrl;
          data.meta    = msg.meta || data.meta;
          await setTabData(tid, data);
          sendResponse({ ok: true });
        } catch (e) { ERR('AUDIO_TRACKS_CAPTURED error:', e); sendResponse({ ok: false }); }
      });

    case 'QUALITY_TRACKS_CAPTURED':
      return handle(async () => {
        try {
          const data = await getTabData(tid);

          const processedQualities = (msg.qualities || []).map(q => {
            const m = (q.label || '').match(/^(\d{3,4}p|4k)/i);
            const cleanLabel = m ? m[1] : (q.label || '');
            return { ...q, label: cleanLabel };
          }).filter(q => q.label && q.label.toLowerCase() !== 'auto');

          data.qualityTracks = processedQualities;
          data.meta    = msg.meta || data.meta || {};
          data.pageUrl = msg.url  || data.pageUrl;
          await setTabData(tid, data);
          sendResponse({ ok: true });
        } catch (e) { ERR('QUALITY_TRACKS_CAPTURED error:', e); sendResponse({ ok: false }); }
      });

    case 'PLAYER_BACK_RESET':
      // User clicked back on the player — wipe state for this tab
      return handle(async () => {
        try {
          const fresh   = emptyTabData();
          fresh.pageUrl = msg.url || '';
          await setTabData(tid, fresh);
          sendResponse({ ok: true });
        } catch (e) { sendResponse({ ok: false }); }
      });

    case 'GET_TAB_DATA':
      return handle(async () => {
        try {
          const data = await getTabData(tid);
          sendResponse({ ok: true, data });
        } catch (e) { sendResponse({ ok: false, data: emptyTabData() }); }
      });

    case 'CLEAR_TAB_DATA':
      return handle(async () => {
        try { await clearTabData(tid); sendResponse({ ok: true }); }
        catch (e) { sendResponse({ ok: false }); }
      });

    case 'TRIGGER_AUDIO_EXTRACT':
      return handle(async () => {
        try {
          await chrome.tabs.sendMessage(tid, { type: 'TRIGGER_AUDIO_EXTRACT' });
          sendResponse({ ok: true });
        } catch (e) { sendResponse({ ok: false, error: e.message }); }
      });

    case 'DELETE_STREAM':
      return handle(async () => {
        try {
          const data = await getTabData(tid);
          data.streams = data.streams.filter(s => s.url !== msg.url);
          await setTabData(tid, data);
          sendResponse({ ok: true });
        } catch (e) { sendResponse({ ok: false }); }
      });

    default:
      return false;
  }
});

// ── Tab lifecycle ─────────────────────────────────────────────────────────────

// Clean up storage when a tab is closed
chrome.tabs.onRemoved.addListener(id => clearTabData(id).catch(() => {}));

// Clear data on full page reload (not SPA navigation)
chrome.webNavigation.onCommitted.addListener(({ tabId, frameId, transitionType }) => {
  if (frameId === 0 && transitionType === 'reload') {
    clearTabData(tabId).catch(() => {});
  }
});

LOG('Service worker ready');
