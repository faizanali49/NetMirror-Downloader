// content.js — Page-level content script (isolated world)
// Scrapes JW Player quality and audio track names by clicking the player UI.
// Works with both net11.cc and net52.cc because selectors cover both ID naming patterns.

(() => {
  if (window.__NETMIRROR_INJECTED__) return;
  window.__NETMIRROR_INJECTED__ = true;

  const LOG = (...a) => console.log('[NM:content]', ...a);
  const ERR = (...a) => console.error('[NM:content]', ...a);

  // DOM selectors covering both site variants (net11 uses jw-jw- prefix, net52 uses jw-player-)
  const SEL = {
    settingsBtn: '[aria-label="Settings"].jw-icon-settings, .jw-icon-settings[role="button"]',

    qualityMenu: [
      '#jw-jw-settings-submenu-quality',
      '#jw-player-settings-submenu-quality',
      '[id$="-settings-submenu-quality"]',
      '.jw-settings-submenu-quality',
    ].join(', '),

    audioBtn: [
      '[style*="audiomenu"]',
      '[aria-label="Audio Menu"]',
      '[aria-label="Audio Tracks"][role="button"]',
      '.jw-settings-audioTracks[role="button"]',
      '.jw-submenu-audioTracks[role="button"]',
    ].join(', '),

    audioMenu: [
      '#jw-jw-settings-submenu-audioTracks',
      '#jw-player-settings-submenu-audioTracks',
      '[id$="-settings-submenu-audioTracks"]',
      '.jw-settings-submenu-audioTracks',
    ].join(', '),

    trackItem:  '.jw-settings-content-item',
    jwPlayer:   '.jwplayer, [id^="jwplayer"], [id^="jw-player"]',
    playerBack: '.btn-payer-back',
  };

  const TIMEOUT  = 5000;
  const DEBOUNCE = 800;

  let audioExtracted   = false;
  let qualityExtracted = false;
  let inProgress       = false;
  let debounceTimer    = null;

  const delay = ms => new Promise(res => setTimeout(res, ms));

  // ── Extension context safety ──────────────────────────────────────────────

  function shutdownScript() {
    try {
      if (domObserver) domObserver.disconnect();
      clearTimeout(debounceTimer);
    } catch (e) {}
  }

  // Wraps sendMessage so a dead extension context doesn't throw uncaught errors
  function safeSendMessage(message) {
    try {
      if (!chrome.runtime || !chrome.runtime.id) { shutdownScript(); return; }
      const result = chrome.runtime.sendMessage(message);
      if (result?.catch) {
        result.catch(err => {
          if (err?.message?.includes('context invalidated')) shutdownScript();
        });
      }
    } catch (e) {
      if (e.message.includes('context invalidated')) shutdownScript();
      else ERR('Extension context unavailable:', e.message);
    }
  }

  // ── Page metadata ─────────────────────────────────────────────────────────

  // Reads title, episode, thumbnail, and URL from the page DOM
  function pageMeta() {
    let title   = '';
    let episode = '';

    const bottomTitleEl = document.querySelector('.player-bottom-title');
    if (bottomTitleEl) {
      const bTag = bottomTitleEl.querySelector('b');
      if (bTag) episode = bTag.textContent.trim();
      const clone  = bottomTitleEl.cloneNode(true);
      const cloneB = clone.querySelector('b');
      if (cloneB) cloneB.remove();
      title = clone.textContent.replace(/\s+/g, ' ').replace(/^–\s*|\s*–$/, '').trim();
    }

    if (!title) {
      title = document.querySelector('h1')?.textContent?.trim()
           || document.querySelector('[data-title]')?.getAttribute('data-title')
           || document.title
           || 'NetMirror Video';
    }

    if (!episode) {
      episode = document.querySelector('.episode.active, .ep.active')?.textContent?.trim()
             || document.querySelector('[data-episode]')?.getAttribute('data-episode')
             || document.querySelector('[data-episode]')?.textContent?.trim()
             || '';
    }

    let thumbnail = document.querySelector('video')?.getAttribute('poster') || '';
    if (!thumbnail) {
      const jwPreview = document.querySelector('.jw-preview');
      const bgImg = jwPreview?.style?.backgroundImage || '';
      if (bgImg && bgImg !== 'none') {
        const m = bgImg.match(/url\s*\(\s*["']?(.*?)["']?\s*\)/);
        if (m && m[1]) thumbnail = m[1];
      }
    }

    return {
      title:     title.trim(),
      episode:   episode.trim(),
      thumbnail: thumbnail.trim(),
      url:       location.href,
    };
  }

  // ── DOM wait helper ───────────────────────────────────────────────────────

  // Waits for a selector to appear in the DOM, times out after `timeout` ms
  function waitForElement(selector, timeout = TIMEOUT) {
    return new Promise((resolve, reject) => {
      const existing = document.querySelector(selector);
      if (existing) { resolve(existing); return; }

      const obs = new MutationObserver(() => {
        if (!chrome.runtime || !chrome.runtime.id) {
          obs.disconnect(); shutdownScript();
          reject(new Error('Context invalidated during observation'));
          return;
        }
        const found = document.querySelector(selector);
        if (found) { obs.disconnect(); resolve(found); }
      });

      obs.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => { obs.disconnect(); reject(new Error('Timeout: ' + selector)); }, timeout);
    });
  }

  // Sends Escape key to cleanly collapse JW Player settings without leaving an invisible overlay
  function closeMenus() {
    const settingsBtn = document.querySelector(SEL.settingsBtn);
    if (!settingsBtn) return;
    settingsBtn.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', keyCode: 27, code: 'Escape', which: 27, bubbles: true, cancelable: true,
    }));
  }

  // ── Quality track scraper ─────────────────────────────────────────────────

  async function extractQualityTracks() {
    if (qualityExtracted) return;

    const settingsBtn = document.querySelector(SEL.settingsBtn);
    if (!settingsBtn) {
      LOG('No settings button — sending meta only');
      safeSendMessage({ type: 'PAGE_META_CAPTURED', meta: pageMeta() });
      return;
    }

    try {
      const isAlreadyExpanded = settingsBtn.getAttribute('aria-expanded') === 'true';
      if (!document.querySelector(SEL.qualityMenu) && !isAlreadyExpanded) {
        settingsBtn.click();
        const menu = await waitForElement(SEL.qualityMenu);
        await delay(150);

        const qualities = [...menu.querySelectorAll(SEL.trackItem)]
          .map(btn => (btn.getAttribute('aria-label') || btn.textContent || '').trim())
          .filter(label => label && label.toLowerCase() !== 'auto')
          .map(label => {
            const m = label.match(/^(\d{3,4}p|4k)/i);
            const cleanLabel = m ? m[1] : label;
            return { label: cleanLabel, ariaLabel: cleanLabel };
          });

        if (!qualities.length) {
          LOG('Quality menu opened but no entries found');
          return;
        }

        qualityExtracted = true;
        LOG('Quality tracks scraped:', qualities.map(q => q.label));
        safeSendMessage({
          type: 'QUALITY_TRACKS_CAPTURED',
          qualities,
          meta: pageMeta(),
          url:  location.href,
        });
      }
    } catch (e) {
      if (!e.message.includes('Context invalidated')) {
        ERR('Quality extraction failed:', e.message);
        safeSendMessage({ type: 'PAGE_META_CAPTURED', meta: pageMeta() });
      }
    }
  }

  // ── Audio track scraper ───────────────────────────────────────────────────

  async function extractAudioTrackNames() {
    if (audioExtracted) return;

    const audioBtn = document.querySelector(SEL.audioBtn);
    if (!audioBtn) {
      LOG('No audio button (single-audio content or not loaded yet)');
      return;
    }

    try {
      LOG('Clicking audio button:', audioBtn.getAttribute('aria-label') || audioBtn.className);
      audioBtn.click();

      const menu = await waitForElement(SEL.audioMenu);
      await delay(150);

      const tracks = [...menu.querySelectorAll(SEL.trackItem)]
        .map((btn, index) => {
          const name = (btn.getAttribute('aria-label') || btn.textContent || '').trim()
                    || `Audio ${index}`;
          return { index, name, ariaLabel: name };
        })
        .filter(t => t.name);

      if (!tracks.length) {
        LOG('Audio submenu opened but no tracks found');
        return;
      }

      // Deduplicate tracks with identical names (e.g. same language listed twice)
      const seen = {};
      const deduped = tracks.map(t => {
        if (!seen[t.name]) {
          seen[t.name] = 1;
          return t;
        }
        seen[t.name]++;
        return { ...t, name: `${t.name} (${seen[t.name]})`, ariaLabel: `${t.ariaLabel} (${seen[t.name]})` };
      });

      audioExtracted = true;
      LOG('Audio tracks scraped:', deduped.map(t => `${t.index}:${t.name}`));
      safeSendMessage({
        type:   'AUDIO_TRACKS_CAPTURED',
        tracks: deduped,
        meta:   pageMeta(),
        url:    location.href,
      });
    } catch (e) {
      if (!e.message.includes('Context invalidated')) {
        ERR('Audio extraction failed:', e.message);
      }
    }
  }

  // ── Main orchestrator ─────────────────────────────────────────────────────

  async function runExtraction() {
    if (inProgress) return;
    inProgress = true;
    LOG('Starting extraction...');

    try {
      const settingsBtn = document.querySelector(SEL.settingsBtn);
      if (!settingsBtn) { inProgress = false; return; }

      await extractQualityTracks();
      await extractAudioTrackNames();

      // Give async background processes time to read the DOM before we collapse the UI
      await delay(1000);

      closeMenus();
      await delay(50);

      if (document.activeElement) document.activeElement.blur();

      // Restore pointer events on the player in case JW left an invisible click-blocker
      const playerContainer = document.querySelector(SEL.jwPlayer);
      if (playerContainer) {
        playerContainer.style.pointerEvents = 'auto';
        playerContainer.dispatchEvent(new MouseEvent('mousemove', {
          bubbles: true, cancelable: true, clientX: 0, clientY: 0,
        }));
      }

    } catch (e) {
      if (!e.message?.includes('Context invalidated')) {
        ERR('runExtraction error:', e.message);
      }
    }

    inProgress = false;
    LOG('Extraction complete.');
  }

  // ── Message listener ──────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener(msg => {
    if (msg.type === 'TRIGGER_AUDIO_EXTRACT') {
      audioExtracted   = false;
      qualityExtracted = false;
      runExtraction();
    }
  });

  // ── Player back button → full state reset ─────────────────────────────────

  document.addEventListener('click', event => {
    if (!event.target.closest(SEL.playerBack)) return;
    audioExtracted   = false;
    qualityExtracted = false;
    safeSendMessage({ type: 'PLAYER_BACK_RESET', url: location.href });
    LOG('Player back clicked — state reset');
  }, true);

  // ── DOM observer: trigger extraction when JW Player mounts ───────────────

  const domObserver = new MutationObserver(() => {
    if (!chrome.runtime || !chrome.runtime.id) { shutdownScript(); return; }
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      safeSendMessage({ type: 'PAGE_META_CAPTURED', meta: pageMeta() });
      if (document.querySelector(SEL.jwPlayer)) runExtraction();
    }, DEBOUNCE);
  });

  domObserver.observe(document.documentElement, { childList: true, subtree: true });
  safeSendMessage({ type: 'PAGE_META_CAPTURED', meta: pageMeta() });
  LOG('Initialized on', location.href);
})();
