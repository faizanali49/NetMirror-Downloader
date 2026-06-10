(() => {
  // Prevent duplicate injection
  if (window.__NETMIRROR_MAIN_INJECTED__) return;
  window.__NETMIRROR_MAIN_INJECTED__ = true;

  const LOG = (...a) => console.log("[NM:content_main]", ...a);

  function getJwPlayerData() {
    try {
      if (!window.jwplayer || typeof window.jwplayer !== "function") {
        return null;
      }

      const players = [];
      const jwElements = document.querySelectorAll(".jwplayer, [id^='jwplayer']");
      
      jwElements.forEach(el => {
        const id = el.id;
        if (id) {
          try {
            const player = window.jwplayer(id);
            if (player && typeof player.getAudioTracks === "function") {
              players.push({
                id: id,
                audioTracks: player.getAudioTracks() || [],
                qualityTracks: player.getQualityLevels() || []
              });
            }
          } catch (e) {
            // Ignore error for specific player ID
          }
        }
      });

      // Try fallback to default player instance if no elements matched
      if (players.length === 0) {
        try {
          const player = window.jwplayer();
          if (player && typeof player.getAudioTracks === "function") {
            players.push({
              id: "default",
              audioTracks: player.getAudioTracks() || [],
              qualityTracks: player.getQualityLevels() || []
            });
          }
        } catch (e) {
          // Ignore
        }
      }

      return players.length > 0 ? players[0] : null;
    } catch (e) {
      LOG("Error reading JW Player API data:", e.message);
      return null;
    }
  }

  // Listen for request events from isolated world content script
  document.addEventListener("NETMIRROR_GET_JW_DATA_REQUEST", () => {
    const data = getJwPlayerData();
    document.dispatchEvent(new CustomEvent("NETMIRROR_GET_JW_DATA_RESPONSE", {
      detail: data ? {
        audioTracks: data.audioTracks.map((t, idx) => ({
          index: idx,
          name: t.name || t.label || `Audio ${idx}`,
          ariaLabel: t.name || t.label || `Audio ${idx}`
        })),
        qualityTracks: data.qualityTracks
          .filter(q => q.label && q.label.toLowerCase() !== "auto")
          .map(q => ({
            label: q.label,
            ariaLabel: q.label
          }))
      } : null
    }));
  });

  LOG("Main world helper loaded successfully.");
})();
