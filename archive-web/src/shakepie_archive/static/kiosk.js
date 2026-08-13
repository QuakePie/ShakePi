// Kiosk wall display: continuous live waveforms, UTC/local clock, station status.
// Deliberately minimal — no controls to babysit, just the signal and the time.
(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const body = document.body;
  const liveUrl = body.dataset.liveUrl;
  const statusUrl = body.dataset.statusUrl;
  const channels = ["north", "east", "vertical"];
  const maximumHistory = Math.max(5, Number(body.dataset.liveHistory || 30));
  const refreshMilliseconds = Math.max(500, Number(body.dataset.liveRefresh || 1000));
  const requestedRate = Number(body.dataset.liveRate || 200);
  const history = {timestamps: [], sequences: [], north: [], east: [], vertical: []};
  const fullScaleCounts = 8388608;
  let sampleRate = 0;
  let loading = false;

  const cssColor = (name, fallback) => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  };

  const canvasContext = (canvas) => {
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const width = Math.max(200, canvas.clientWidth);
    const height = Math.max(60, canvas.clientHeight);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return {context, width, height};
  };

  const appendPayload = (payload) => {
    if (payload.reset) {
      history.timestamps.length = 0;
      history.sequences.length = 0;
      channels.forEach((channel) => { history[channel].length = 0; });
    }
    const count = payload.timestamps_ms.length;
    for (let index = 0; index < count; index += 1) {
      history.timestamps.push(payload.timestamps_ms[index]);
      history.sequences.push(payload.sequences[index]);
      channels.forEach((channel) => history[channel].push(payload[channel][index]));
    }
    if (payload.sample_rate_hz) sampleRate = payload.sample_rate_hz;
    if (!history.timestamps.length) return;
    const cutoff = history.timestamps[history.timestamps.length - 1] - maximumHistory * 1000;
    let remove = 0;
    while (remove < history.timestamps.length && history.timestamps[remove] < cutoff) remove += 1;
    if (remove) {
      history.timestamps.splice(0, remove);
      channels.forEach((channel) => history[channel].splice(0, remove));
    }
  };

  const drawChannel = (canvas, channel) => {
    const {context, width, height} = canvasContext(canvas);
    const background = cssColor("--canvas-bg", "#0d1117");
    const grid = cssColor("--canvas-grid", "#21283b");
    const ink = cssColor("--canvas-ink", "#8b949e");
    const wave = cssColor("--canvas-wave", "#4fb3c4");
    context.fillStyle = background;
    context.fillRect(0, 0, width, height);
    if (history.timestamps.length < 2) return;
    const end = history.timestamps[history.timestamps.length - 1];
    const start = end - maximumHistory * 1000;
    let peak = 1;
    for (let index = 0; index < history.timestamps.length; index += 1) {
      peak = Math.max(peak, Math.abs(history[channel][index]));
    }
    const limit = peak * 1.05;
    const plot = {left: 0, top: 6, width, height: height - 12};
    context.strokeStyle = grid;
    context.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach((fraction) => {
      const y = plot.top + plot.height * fraction;
      context.beginPath();
      context.moveTo(plot.left, y);
      context.lineTo(plot.left + plot.width, y);
      context.stroke();
    });
    context.fillStyle = ink;
    context.font = "11px system-ui, sans-serif";
    context.textAlign = "left";
    context.textBaseline = "top";
    const scaleLabel = limit >= 1000 ? `±${(limit / 1000).toFixed(1)}k counts` : `±${Math.round(limit)} counts`;
    context.fillText(scaleLabel, plot.left + 6, plot.top + 2);
    context.save();
    context.beginPath();
    context.rect(plot.left, plot.top, plot.width, plot.height);
    context.clip();
    context.strokeStyle = wave;
    context.lineWidth = 1.1;
    const xOf = (time) => plot.left + (time - start) * plot.width / (end - start);
    const yOf = (value) => plot.top + plot.height * (limit - value) / (2 * limit);
    if (history.timestamps.length <= plot.width * 2) {
      context.beginPath();
      for (let index = 0; index < history.timestamps.length; index += 1) {
        const x = xOf(history.timestamps[index]);
        const y = yOf(history[channel][index]);
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      context.stroke();
    } else {
      const bins = new Array(Math.max(1, Math.floor(plot.width)));
      for (let index = 0; index < history.timestamps.length; index += 1) {
        const bin = Math.max(0, Math.min(bins.length - 1, Math.floor(xOf(history.timestamps[index]) - plot.left)));
        const value = history[channel][index];
        if (!bins[bin]) bins[bin] = {minimum: value, maximum: value};
        else {
          bins[bin].minimum = Math.min(bins[bin].minimum, value);
          bins[bin].maximum = Math.max(bins[bin].maximum, value);
        }
      }
      context.beginPath();
      bins.forEach((bin, index) => {
        if (!bin) return;
        const x = plot.left + index + 0.5;
        context.moveTo(x, yOf(bin.minimum));
        context.lineTo(x, yOf(bin.maximum));
      });
      context.stroke();
    }
    context.restore();
  };

  const drawAll = () => {
    document.querySelectorAll("[data-kiosk-wave]").forEach((canvas) => {
      drawChannel(canvas, canvas.dataset.kioskWave);
    });
  };

  const refreshLive = async () => {
    if (loading) return;
    loading = true;
    try {
      const parameters = new URLSearchParams({sample_rate: String(requestedRate)});
      if (history.sequences.length) {
        parameters.set("after_sequence", String(history.sequences[history.sequences.length - 1]));
      }
      const response = await fetch(`${liveUrl}?${parameters}`, {cache: "no-store"});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      const dot = byId("kiosk-state-dot");
      const text = byId("kiosk-state-text");
      if (payload.state === "connected") {
        dot.className = "status-dot status-green";
        text.textContent = "Recording";
      } else if (payload.state === "connecting" || payload.state === "idle") {
        dot.className = "status-dot status-yellow";
        text.textContent = "Waiting for stream…";
      } else {
        dot.className = "status-dot status-red";
        text.textContent = payload.state === "disabled" ? "Live disabled" : "Stream unavailable";
      }
      appendPayload(payload);
      drawAll();
      byId("kiosk-error").textContent = "";
    } catch (error) {
      byId("kiosk-error").textContent = `Live feed: ${error instanceof Error ? error.message : error}`;
    } finally {
      loading = false;
    }
  };

  const refreshStatus = async () => {
    if (!statusUrl) return;
    try {
      const response = await fetch(statusUrl, {cache: "no-store"});
      if (!response.ok) return;
      const status = await response.json();
      byId("kiosk-rate").textContent = status.sampling_rate || "—";
      byId("kiosk-age").textContent = `Last sample ${status.last_sample_age || "—"} ago`;
      byId("kiosk-events").textContent =
        `${status.events_today ?? 0} event${status.events_today === 1 ? "" : "s"} today`;
      byId("kiosk-disk").textContent =
        `${status.disk_available || "—"} free · ${status.recording_days_remaining || "?"} days left`;
    } catch {
      /* the footer keeps its previous values */
    }
  };

  const tickClock = () => {
    const now = new Date();
    byId("kiosk-clock-utc").textContent = now.toISOString().slice(11, 19);
    byId("kiosk-clock-local").textContent = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Shanghai",
    }).format(now);
  };

  byId("kiosk-fullscreen").addEventListener("click", () => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      document.documentElement.requestFullscreen().catch(() => {});
    }
  });

  window.addEventListener("resize", drawAll);
  tickClock();
  window.setInterval(tickClock, 1000);
  refreshStatus();
  window.setInterval(refreshStatus, 15000);
  refreshLive();
  window.setInterval(refreshLive, refreshMilliseconds);
})();
