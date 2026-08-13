(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);

  // Dark mode: system preference by default, manual toggle stored locally.
  const THEME_KEY = "shakepie-theme";
  const cssColor = (name, fallback) => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  };
  const plotTheme = () => ({
    bg: cssColor("--canvas-bg", "#ffffff"),
    grid: cssColor("--canvas-grid", "#e3e9ed"),
    ink: cssColor("--canvas-ink", "#60707f"),
    axis: cssColor("--canvas-axis", "#9eacb6"),
    wave: cssColor("--canvas-wave", "#176b87"),
  });
  const storedTheme = () => localStorage.getItem(THEME_KEY);
  const effectiveTheme = () =>
    document.documentElement.dataset.theme
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const applyTheme = () => {
    const stored = storedTheme();
    if (stored === "dark" || stored === "light") {
      document.documentElement.dataset.theme = stored;
    } else {
      delete document.documentElement.dataset.theme;
    }
    document.querySelectorAll("#theme-toggle .theme-icon-moon").forEach((icon) => {
      icon.classList.toggle("hidden", effectiveTheme() === "dark");
    });
    document.querySelectorAll("#theme-toggle .theme-icon-sun").forEach((icon) => {
      icon.classList.toggle("hidden", effectiveTheme() !== "dark");
    });
  };
  applyTheme();
  document.querySelectorAll("#theme-toggle").forEach((button) => {
    button.addEventListener("click", () => {
      localStorage.setItem(THEME_KEY, effectiveTheme() === "dark" ? "light" : "dark");
      applyTheme();
      window.dispatchEvent(new Event("shakepie-theme-change"));
    });
  });

  const canvasContext = (canvas) => {
    const width = Math.max(280, canvas.clientWidth);
    const height = Math.max(220, canvas.clientHeight);
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(height * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return {context, width, height};
  };

  const plotFrame = (
    context, width, height, xStart, xEnd, yTicks, yLabel, xFormatter = null, rightMargin = 14
  ) => {
    const theme = plotTheme();
    const margin = {left: 66, right: rightMargin, top: 12, bottom: 34};
    const plot = {
      left: margin.left,
      top: margin.top,
      width: width - margin.left - margin.right,
      height: height - margin.top - margin.bottom,
    };
    context.fillStyle = theme.bg;
    context.fillRect(0, 0, width, height);
    context.font = "12px system-ui, sans-serif";
    context.lineWidth = 1;
    context.strokeStyle = theme.grid;
    context.fillStyle = theme.ink;
    context.textAlign = "right";
    context.textBaseline = "middle";
    yTicks.forEach(({label, fraction}) => {
      const y = plot.top + plot.height * (1 - fraction);
      context.beginPath();
      context.moveTo(plot.left, y);
      context.lineTo(plot.left + plot.width, y);
      context.stroke();
      context.fillText(label, plot.left - 8, y);
    });
    context.textAlign = "center";
    context.textBaseline = "top";
    for (let tick = 0; tick <= 4; tick += 1) {
      const fraction = tick / 4;
      const x = plot.left + plot.width * fraction;
      const timestamp = xStart + (xEnd - xStart) * fraction;
      context.beginPath();
      context.moveTo(x, plot.top);
      context.lineTo(x, plot.top + plot.height);
      context.stroke();
      const label = xFormatter
        ? xFormatter(timestamp)
        : `${Math.round((timestamp - xEnd) / 1000)}s`;
      context.fillText(label, x, plot.top + plot.height + 8);
    }
    context.save();
    context.translate(14, plot.top + plot.height / 2);
    context.rotate(-Math.PI / 2);
    context.textAlign = "center";
    context.textBaseline = "top";
    context.fillText(yLabel, 0, 0);
    context.restore();
    context.strokeStyle = theme.axis;
    context.strokeRect(plot.left, plot.top, plot.width, plot.height);
    return plot;
  };

  const niceLimit = (value) => {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    const normalized = value / power;
    const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return step * power;
  };

  const heatColor = (decibels) => {
    const value = Math.max(0, Math.min(1, (decibels + 120) / 100));
    // Perceptually ordered Magma palette: dark means low energy, light means strong energy.
    const stops = [
      [0, 0, 4], [28, 16, 68], [79, 18, 123], [129, 37, 129], [181, 54, 122],
      [229, 80, 100], [251, 135, 97], [254, 194, 135], [252, 253, 191],
    ];
    const scaled = value * (stops.length - 1);
    const index = Math.min(stops.length - 2, Math.floor(scaled));
    const fraction = scaled - index;
    const color = stops[index].map((component, componentIndex) => Math.round(
      component + (stops[index + 1][componentIndex] - component) * fraction
    ));
    return `rgb(${color[0]},${color[1]},${color[2]})`;
  };

  const deleteDialog = byId("delete-dialog");
  document.querySelectorAll(".js-delete").forEach((button) => {
    button.addEventListener("click", () => {
      byId("delete-form").action = button.dataset.action;
      byId("delete-filename").textContent = button.dataset.filename;
      byId("delete-confirm-filename").value = button.dataset.filename;
      deleteDialog.showModal();
    });
  });

  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog").close());
  });

  ["delete-form"].forEach((formId) => {
    const form = byId(formId);
    if (!form) return;
    form.addEventListener("submit", () => {
      const submit = form.querySelector('button[type="submit"]');
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Working…";
      }
    });
  });

  // Events page: remove a false detection from the list after confirmation.
  const removeEventDialog = byId("remove-event-dialog");
  if (removeEventDialog) {
    const removeForm = byId("remove-event-form");
    document.querySelectorAll(".js-remove-event").forEach((button) => {
      button.addEventListener("click", () => {
        removeForm.action = button.dataset.action;
        byId("remove-event-time").textContent = button.dataset.time;
        removeEventDialog.showModal();
      });
    });
    removeForm.addEventListener("submit", () => {
      const submit = removeForm.querySelector('button[type="submit"]');
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Removing…";
      }
    });
  }

  // Events page: multi-select removal of false detections.
  const eventsTable = document.querySelector("[data-events-table]");
  if (eventsTable) {
    const selectAll = byId("events-select-all");
    const bulkBar = byId("events-bulk-bar");
    const bulkButton = byId("events-remove-selected");
    const countLabel = byId("events-selected-count");
    const checkboxes = () => [...eventsTable.querySelectorAll(".event-check")];
    const csrfToken = (document.querySelector('input[name="csrf_token"]') || {value: ""}).value;
    const checkedIds = () =>
      checkboxes().filter((box) => box.checked).map((box) => Number(box.dataset.id));

    const updateBulkBar = () => {
      const checked = checkboxes().filter((box) => box.checked);
      bulkBar.classList.toggle("hidden", checked.length === 0);
      bulkButton.disabled = checked.length === 0;
      countLabel.textContent = `${checked.length} selected`;
      if (selectAll) {
        selectAll.checked = checked.length > 0 && checked.length === checkboxes().length;
        selectAll.indeterminate = checked.length > 0 && checked.length < checkboxes().length;
      }
    };

    if (selectAll) {
      selectAll.addEventListener("change", () => {
        checkboxes().forEach((box) => { box.checked = selectAll.checked; });
        updateBulkBar();
      });
    }
    checkboxes().forEach((box) => box.addEventListener("change", updateBulkBar));

    bulkButton.addEventListener("click", async () => {
      const ids = checkedIds();
      if (!ids.length) return;
      if (!window.confirm(`Remove ${ids.length} event(s) from the list? The recorded data is not affected.`)) {
        return;
      }
      bulkButton.disabled = true;
      bulkButton.textContent = "Removing…";
      try {
        const response = await fetch(eventsTable.dataset.bulkUrl, {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
          body: JSON.stringify({ids}),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        window.location.reload();
      } catch {
        bulkButton.textContent = "Failed - try again";
        bulkButton.disabled = false;
      }
    });
    updateBulkBar();
  }

  // Play-as-sound: load the recording as audible audio (speed-up ~40x, normalized).
  const audioChannel = byId("audio-channel");
  const recordingAudio = byId("recording-audio");
  if (recordingAudio && audioChannel) {
    const recordedFile = byId("recorded-file");
    const updateAudioSource = () => {
      const recordingId = recordedFile ? recordedFile.value : "";
      if (recordingId && !recordedFile.disabled) {
        recordingAudio.src = `/recordings/${recordingId}/audio?channel=${audioChannel.value}`;
      } else {
        recordingAudio.pause();
        recordingAudio.removeAttribute("src");
      }
    };
    audioChannel.addEventListener("change", updateAudioSource);
    if (recordedFile) {
      recordedFile.addEventListener("change", () => {
        recordingAudio.pause();
        updateAudioSource();
      });
    }
    window.addEventListener("shakepie-recording-loaded", updateAudioSource);
  }

  // Shared calendar day picker: days containing finalized recordings are selectable.
  const daysUrl = document.body.dataset.daysUrl;
  const calendarButtons = document.querySelectorAll("[data-calendar-for]");
  if (daysUrl && calendarButtons.length) {
    const MONTH_LABELS = ["January", "February", "March", "April", "May", "June", "July",
      "August", "September", "October", "November", "December"];
    const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
    let availableDays = null; // { "YYYY-MM-DD": recordingCount }
    let viewYear = 0;
    let viewMonth = 0; // 0-based
    let activeInput = null;
    let activeMode = "navigate";

    const popover = document.createElement("div");
    popover.id = "calendar-popover";
    popover.className = "calendar-popover hidden";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", "Choose a UTC day");
    document.body.append(popover);

    const iso = (year, month, day) => `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;

    const choose = (value) => {
      closeCalendar();
      if (activeMode === "input" && activeInput) {
        activeInput.value = value;
        activeInput.dispatchEvent(new Event("change"));
        return;
      }
      const url = new URL(window.location.href);
      url.searchParams.set("date", value);
      url.searchParams.delete("page");
      window.location.assign(url.toString());
    };

    const renderCalendar = () => {
      const todayIso = new Date().toISOString().slice(0, 10);
      const selected = activeInput && activeInput.value ? activeInput.value : "";
      const firstDay = new Date(Date.UTC(viewYear, viewMonth, 1));
      const startOffset = (firstDay.getUTCDay() + 6) % 7; // Monday-first grid
      const daysInMonth = new Date(Date.UTC(viewYear, viewMonth + 1, 0)).getUTCDate();
      const cells = WEEKDAYS.map((name) => `<span class="calendar-weekday">${name}</span>`);
      for (let blank = 0; blank < startOffset; blank += 1) cells.push("<span></span>");
      for (let day = 1; day <= daysInMonth; day += 1) {
        const value = iso(viewYear, viewMonth, day);
        const count = availableDays ? availableDays[value] : undefined;
        const classes = ["calendar-day"];
        if (value === todayIso) classes.push("calendar-today");
        if (value === selected) classes.push("calendar-selected");
        if (count !== undefined) {
          classes.push("calendar-available");
          cells.push(
            `<button type="button" class="${classes.join(" ")}" data-date="${value}" `
            + `title="${count} recording${count === 1 ? "" : "s"}">${day}</button>`
          );
        } else {
          classes.push("calendar-empty");
          cells.push(`<span class="${classes.join(" ")}" aria-disabled="true">${day}</span>`);
        }
      }
      popover.innerHTML = `
        <div class="calendar-head">
          <button type="button" class="icon-button" data-calendar-nav="-1" aria-label="Previous month">‹</button>
          <strong>${MONTH_LABELS[viewMonth]} ${viewYear}</strong>
          <button type="button" class="icon-button" data-calendar-nav="1" aria-label="Next month">›</button>
        </div>
        <div class="calendar-grid">${cells.join("")}</div>
        <div class="calendar-foot">
          <span class="muted">Filled days contain recordings</span>
          <button type="button" class="button button-small button-quiet" data-calendar-close>Close</button>
        </div>`;
      popover.querySelectorAll("[data-date]").forEach((cell) => {
        cell.addEventListener("click", () => choose(cell.dataset.date));
      });
      popover.querySelectorAll("[data-calendar-nav]").forEach((nav) => {
        nav.addEventListener("click", () => {
          const next = new Date(Date.UTC(viewYear, viewMonth + Number(nav.dataset.calendarNav), 1));
          viewYear = next.getUTCFullYear();
          viewMonth = next.getUTCMonth();
          renderCalendar();
        });
      });
      const closer = popover.querySelector("[data-calendar-close]");
      if (closer) closer.addEventListener("click", closeCalendar);
    };

    const closeCalendar = () => {
      popover.classList.add("hidden");
      document.removeEventListener("click", outsideClick, true);
    };

    const outsideClick = (event) => {
      if (!popover.contains(event.target) && !event.target.closest("[data-calendar-for]")) {
        closeCalendar();
      }
    };

    const openCalendar = (inputId, mode, anchor) => {
      activeInput = byId(inputId);
      activeMode = mode;
      const open = () => {
        const rect = anchor.getBoundingClientRect();
        popover.classList.remove("hidden");
        const width = Math.min(340, window.innerWidth - 16);
        popover.style.top = `${Math.min(window.innerHeight - popover.offsetHeight - 8, rect.bottom + 8)}px`;
        popover.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
        popover.style.width = `${width}px`;
      };
      const initial = activeInput && activeInput.value
        ? activeInput.value
        : new Date().toISOString().slice(0, 10);
      viewYear = Number(initial.slice(0, 4));
      viewMonth = Number(initial.slice(5, 7)) - 1;
      if (availableDays) {
        renderCalendar();
        open();
        return;
      }
      fetch(daysUrl, {headers: {Accept: "application/json"}, cache: "no-store"})
        .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
        .then((days) => {
          availableDays = {};
          days.forEach((entry) => { availableDays[entry.date] = entry.recordings; });
          renderCalendar();
          open();
        })
        .catch(() => { /* the text input remains usable without the picker */ });
    };

    calendarButtons.forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!popover.classList.contains("hidden")) {
          closeCalendar();
          return;
        }
        openCalendar(button.dataset.calendarFor, button.dataset.calendarMode || "navigate", button);
        document.addEventListener("click", outsideClick, true);
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeCalendar();
    });
  }

  // DataView-style dayplot interaction: click a plot line to open the recorded data
  // covering that time of day. The static image has plot margins, so the mapping is
  // intentionally coarse (one row of the helicorder).
  const dayplotStack = document.querySelector(".plot-stack[data-recorded-url]");
  if (dayplotStack) {
    const interval = Number(dayplotStack.dataset.interval || 60);
    dayplotStack.querySelectorAll("img.dayplot-image").forEach((img) => {
      img.style.cursor = "crosshair";
      img.title = "Click to open the recorded data covering this time";
      img.addEventListener("click", (event) => {
        const rect = img.getBoundingClientRect();
        const rows = Math.max(1, Math.round(1440 / interval));
        const row = Math.min(rows - 1, Math.max(0, Math.floor(((event.clientY - rect.top) / rect.height) * rows)));
        const minutes = row * interval;
        const at = `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}:00`;
        const url = new URL(dayplotStack.dataset.recordedUrl, window.location.origin);
        if (dayplotStack.dataset.day) url.searchParams.set("date", dayplotStack.dataset.day);
        url.searchParams.set("at", at);
        window.location.assign(url.toString());
      });
    });
  }

  // AnyShake-style confirm gate: applying recorder settings restarts services.
  const settingsForm = byId("settings-form");
  if (settingsForm) {
    settingsForm.addEventListener("submit", (event) => {
      if (!window.confirm("Apply the new recorder settings? Acquisition pauses briefly while the recorder restarts.")) {
        event.preventDefault();
      }
    });
  }

  // Regenerating every archived day queues hours of worker work on the Pi; ask first.
  document.querySelectorAll(".js-regenerate-all").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm("Queue dayplot regeneration for every archived day? The worker plots them one by one, which can take hours.")) {
        event.preventDefault();
      }
    });
  });

  // Clipboard: navigator.clipboard exists only in secure contexts, and the recorder is
  // served over plain HTTP on the LAN, so a textarea fallback is required there. If the
  // browser rejects even that, a prompt shows the URL for a manual copy.
  const copyTextToClipboard = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return "copied";
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    area.setSelectionRange(0, text.length);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    area.remove();
    if (copied) return "copied";
    const manual = window.prompt(
      "Automatic copy is blocked by the browser. Copy the link here:",
      text
    );
    return manual !== null ? "manual" : "cancelled";
  };

  const copyLink = byId("recorded-copy-link");
  if (copyLink) {
    copyLink.addEventListener("click", async () => {
      try {
        const outcome = await copyTextToClipboard(window.location.href);
        copyLink.textContent = outcome === "manual" ? "Copied manually" : "Link copied";
      } catch {
        copyLink.textContent = "Copy failed";
      }
      window.setTimeout(() => { copyLink.textContent = "Copy link"; }, 2500);
    });
  }

  // Sensor zero calibration panel (settings page): live status while measuring.
  const calibrationUrl = document.body.dataset.calibrationUrl;
  const calibrationPanel = byId("calibration-panel");
  if (calibrationUrl && calibrationPanel) {
    const state = byId("calib-state");
    const offsets = byId("calib-offsets");
    const start = byId("calib-start");
    const clear = byId("calib-clear");
    const refresh = async () => {
      try {
        const response = await fetch(calibrationUrl, {headers: {Accept: "application/json"}, cache: "no-store"});
        if (!response.ok) return;
        const status = await response.json();
        const phase = status.calibration_phase || "idle";
        const outcome = status.calibration_outcome || "none";
        const label = phase === "measuring"
          ? `Measuring… ${status.calibration_progress || 0}%`
          : phase === "failed"
            ? "Failed — offset beyond the safety limit; offsets unchanged"
            : outcome === "applied"
              ? "Zero level applied"
              : outcome === "cleared"
                ? "Offsets cleared — recording raw"
                : outcome === "disabled"
                  ? "Calibration is disabled in the recorder configuration"
                  : "Idle";
        state.textContent = label;
        offsets.textContent = `North ${Number(status.zero_offset_north || 0).toLocaleString()} · `
          + `East ${Number(status.zero_offset_east || 0).toLocaleString()} · `
          + `Vertical ${Number(status.zero_offset_vertical || 0).toLocaleString()} counts`;
        const measuring = phase === "measuring";
        if (start) start.disabled = measuring;
        if (clear) clear.disabled = measuring;
      } catch {
        state.textContent = "Status unavailable";
      }
    };
    refresh();
    window.setInterval(refresh, 2000);
  }

  const statusUrl = document.body.dataset.statusUrl;
  if (statusUrl) {
    const setStatus = (status) => {
      document.querySelectorAll("[data-status-field]").forEach((element) => {
        const value = status[element.dataset.statusField];
        if (value !== undefined && value !== null) element.textContent = value;
      });
      document.querySelectorAll("[data-status-level]").forEach((element) => {
        const level = status[element.dataset.statusLevel] || "gray";
        element.className = `status-dot status-${level}`;
      });
      document.querySelectorAll("[data-status-progress]").forEach((element) => {
        const value = Math.max(0, Math.min(100, Number(status[element.dataset.statusProgress] || 0)));
        element.value = value;
        element.textContent = `${value}%`;
      });
      const warning = byId("health-warning");
      if (status.health_error || status.health_stale) {
        warning.textContent = status.health_error
          ? `Health information is unavailable: ${status.health_error}`
          : "Recorder health information is stale. Verify the recorder service.";
        warning.classList.remove("hidden");
      } else {
        warning.classList.add("hidden");
      }
    };
    const refresh = async () => {
      const button = byId("refresh-status");
      button.disabled = true;
      try {
        const response = await fetch(statusUrl, {headers: {Accept: "application/json"}, cache: "no-store"});
        if (response.ok) setStatus(await response.json());
      } finally {
        button.disabled = false;
      }
    };
    byId("refresh-status").addEventListener("click", refresh);
    const seconds = Math.max(15, Number(document.body.dataset.refreshSeconds || 15));
    window.setInterval(refresh, seconds * 1000);
  }

  const liveUrl = document.body.dataset.liveUrl;
  if (liveUrl) {
    const channels = ["north", "east", "vertical"];
    const fullScaleCounts = 8388608;
    const fftSize = 256;
    const fftHop = 128;
    const maximumHistory = Math.max(5, Number(document.body.dataset.liveHistory || 30));
    const maximumFrequency = Math.max(1, Number(document.body.dataset.liveFrequency || 100));
    const refreshMilliseconds = Math.max(500, Number(document.body.dataset.liveRefresh || 1000));
    const windowSelect = byId("live-window");
    const fmaxSelect = byId("live-fmax");
    const toggle = byId("live-toggle");
    const errorBox = byId("live-error");
    const live = {timestamps: [], sequences: [], north: [], east: [], vertical: []};
    let visibleSeconds = Math.min(30, maximumHistory);
    const requestedRate = Number(document.body.dataset.liveRate || 200);
    let sampleRate = 0;
    let paused = false;
    let loading = false;
    // User-adjustable spectrogram frequency limit (stored between visits). Clamped to
    // the Nyquist frequency once the first sample rate arrives.
    let viewFrequency = Math.min(
      Math.max(1, Number(localStorage.getItem("shakepie-live-fmax") || maximumFrequency)),
      maximumFrequency
    );

    [...windowSelect.options].forEach((option) => {
      option.disabled = Number(option.value) > maximumHistory;
    });
    windowSelect.value = String(visibleSeconds);
    if (fmaxSelect) {
      [...fmaxSelect.options].forEach((option) => {
        option.disabled = Number(option.value) > maximumFrequency;
      });
      if (![...fmaxSelect.options].some(
        (option) => Number(option.value) === viewFrequency && !option.disabled
      )) {
        viewFrequency = maximumFrequency;
      }
      fmaxSelect.value = String(viewFrequency);
      fmaxSelect.addEventListener("change", () => {
        viewFrequency = Math.min(Number(fmaxSelect.value), maximumFrequency);
        localStorage.setItem("shakepie-live-fmax", String(viewFrequency));
        drawLive();
      });
    }
    const setLiveStatus = (payload) => {
      const state = payload.state || "unavailable";
      const labels = {
        connected: "Streaming",
        connecting: "Connecting…",
        idle: "Waiting for stream…",
        unavailable: "Stream unavailable",
        disabled: "Disabled",
      };
      const levels = {connected: "green", connecting: "yellow", idle: "gray", unavailable: "red", disabled: "gray"};
      byId("live-status-text").textContent = labels[state] || state;
      byId("live-status-dot").className = `status-dot status-${levels[state] || "gray"}`;
      byId("live-status-dot").classList.toggle("hidden", state === "connecting" || state === "idle");
      byId("live-spinner").classList.toggle("hidden", state !== "connecting" && state !== "idle");
      toggle.disabled = state !== "connected";
      byId("live-rate").textContent = payload.sample_rate_hz ? `${payload.sample_rate_hz.toFixed(1)} Hz` : "—";
      const sourceRate = byId("live-source-rate");
      if (sourceRate) {
        const differs = payload.source_sample_rate_hz && payload.sample_rate_hz
          && Math.abs(payload.source_sample_rate_hz - payload.sample_rate_hz) > 0.05;
        sourceRate.textContent = differs ? `${payload.source_sample_rate_hz.toFixed(1)} Hz` : "matches";
      }
      byId("live-age").textContent = payload.age_ms === null || payload.age_ms === undefined
        ? "—"
        : payload.age_ms < 1000 ? `${Math.round(payload.age_ms)} ms` : `${(payload.age_ms / 1000).toFixed(1)} s`;
      byId("live-gaps").textContent = String(payload.sample_gaps || 0);
      if (payload.error) {
        errorBox.textContent = `Recorder stream: ${payload.error}`;
        errorBox.classList.remove("hidden");
      } else {
        errorBox.classList.add("hidden");
      }
    };

    const appendPayload = (payload) => {
      if (payload.reset) {
        live.timestamps.length = 0;
        live.sequences.length = 0;
        channels.forEach((channel) => { live[channel].length = 0; });
      }
      const count = payload.timestamps_ms.length;
      for (let index = 0; index < count; index += 1) {
        live.timestamps.push(payload.timestamps_ms[index]);
        live.sequences.push(payload.sequences[index]);
        channels.forEach((channel) => live[channel].push(payload[channel][index]));
      }
      if (payload.sample_rate_hz) sampleRate = payload.sample_rate_hz;
      if (count) {
        document.querySelectorAll(".plot-placeholder").forEach((item) => item.classList.add("hidden"));
      }
      if (!live.timestamps.length) return;
      const cutoff = live.timestamps[live.timestamps.length - 1] - maximumHistory * 1000;
      let remove = 0;
      while (remove < live.timestamps.length && live.timestamps[remove] < cutoff) remove += 1;
      if (remove) {
        live.timestamps.splice(0, remove);
        live.sequences.splice(0, remove);
        channels.forEach((channel) => live[channel].splice(0, remove));
      }
    };

    const visibleRange = () => {
      const latest = live.timestamps.length ? live.timestamps[live.timestamps.length - 1] : Date.now();
      return {start: latest - visibleSeconds * 1000, end: latest};
    };

    const firstVisibleIndex = (start) => {
      let low = 0;
      let high = live.timestamps.length;
      while (low < high) {
        const middle = Math.floor((low + high) / 2);
        if (live.timestamps[middle] < start) low = middle + 1;
        else high = middle;
      }
      return low;
    };

    const sharedWaveformLimit = (first) => {
      let peak = 1;
      for (let index = first; index < live.timestamps.length; index += 1) {
        channels.forEach((channel) => { peak = Math.max(peak, Math.abs(live[channel][index])); });
      }
      return niceLimit(peak * 1.05);
    };

    const drawWaveform = (canvas, channel, range, first, limit) => {
      const {context, width, height} = canvasContext(canvas);
      const theme = plotTheme();
      const ticks = [-limit, -limit / 2, 0, limit / 2, limit].map((value) => ({
        value,
        label: Math.abs(value) >= 1000 ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)}k` : String(value),
        fraction: (value + limit) / (2 * limit),
      }));
      const plot = plotFrame(context, width, height, range.start, range.end, ticks, "ADC counts");
      if (first >= live.timestamps.length) return;
      const xOf = (time) => plot.left + (time - range.start) * plot.width / (range.end - range.start);
      const yOf = (value) => plot.top + plot.height * (limit - value) / (2 * limit);
      const count = live.timestamps.length - first;
      context.save();
      context.beginPath();
      context.rect(plot.left, plot.top, plot.width, plot.height);
      context.clip();
      context.strokeStyle = theme.wave;
      context.lineWidth = 1.15;
      if (count <= plot.width * 2) {
        context.beginPath();
        let drawing = false;
        for (let index = first; index < live.timestamps.length; index += 1) {
          const continuous = index > first
            && live.timestamps[index] > live.timestamps[index - 1]
            && (!sampleRate || live.timestamps[index] - live.timestamps[index - 1] < 3000 / sampleRate);
          const x = xOf(live.timestamps[index]);
          const y = yOf(live[channel][index]);
          if (!drawing || !continuous) context.moveTo(x, y);
          else context.lineTo(x, y);
          drawing = true;
        }
        context.stroke();
      } else {
        const bins = new Array(Math.max(1, Math.floor(plot.width)));
        for (let index = first; index < live.timestamps.length; index += 1) {
          const bin = Math.max(0, Math.min(bins.length - 1, Math.floor(xOf(live.timestamps[index]) - plot.left)));
          const value = live[channel][index];
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

    const fft = (real, imaginary) => {
      const length = real.length;
      for (let index = 1, reversed = 0; index < length; index += 1) {
        let bit = length >> 1;
        while (reversed & bit) { reversed ^= bit; bit >>= 1; }
        reversed ^= bit;
        if (index < reversed) {
          [real[index], real[reversed]] = [real[reversed], real[index]];
          [imaginary[index], imaginary[reversed]] = [imaginary[reversed], imaginary[index]];
        }
      }
      for (let size = 2; size <= length; size <<= 1) {
        const angle = -2 * Math.PI / size;
        for (let start = 0; start < length; start += size) {
          for (let offset = 0; offset < size / 2; offset += 1) {
            const cosine = Math.cos(angle * offset);
            const sine = Math.sin(angle * offset);
            const even = start + offset;
            const odd = even + size / 2;
            const oddReal = real[odd] * cosine - imaginary[odd] * sine;
            const oddImaginary = real[odd] * sine + imaginary[odd] * cosine;
            real[odd] = real[even] - oddReal;
            imaginary[odd] = imaginary[even] - oddImaginary;
            real[even] += oddReal;
            imaginary[even] += oddImaginary;
          }
        }
      }
    };

    const spectrumFrames = (channel, first) => {
      if (!sampleRate) return [];
      const frames = [];
      for (let start = first; start + fftSize <= live.timestamps.length; start += fftHop) {
        let continuous = true;
        let mean = 0;
        for (let offset = 0; offset < fftSize; offset += 1) {
          mean += live[channel][start + offset];
          if (offset && (
            live.timestamps[start + offset] <= live.timestamps[start + offset - 1]
            || live.timestamps[start + offset] - live.timestamps[start + offset - 1] >= 3000 / sampleRate
          )) continuous = false;
        }
        if (!continuous) continue;
        mean /= fftSize;
        const real = new Float64Array(fftSize);
        const imaginary = new Float64Array(fftSize);
        for (let offset = 0; offset < fftSize; offset += 1) {
          const windowValue = 0.5 - 0.5 * Math.cos(2 * Math.PI * offset / (fftSize - 1));
          real[offset] = (live[channel][start + offset] - mean) * windowValue;
        }
        fft(real, imaginary);
        const spectrumMaximum = Math.min(maximumFrequency, sampleRate / 2);
        const maximumBin = Math.min(Math.floor(spectrumMaximum * fftSize / sampleRate), fftSize / 2);
        const decibels = new Float32Array(maximumBin + 1);
        for (let bin = 0; bin <= maximumBin; bin += 1) {
          const magnitude = 4 * Math.hypot(real[bin], imaginary[bin]) / fftSize / fullScaleCounts;
          decibels[bin] = 20 * Math.log10(Math.max(magnitude, 1e-12));
        }
        frames.push({time: live.timestamps[start + Math.floor(fftSize / 2)], decibels});
      }
      return frames;
    };

    const drawSpectrogram = (canvas, channel, range, first) => {
      const {context, width, height} = canvasContext(canvas);
      // The view limit (user choice) may zoom below the available spectrum; bins above
      // it are simply not drawn and the axis rescales.
      const axisMaximum = Math.min(
        viewFrequency,
        sampleRate ? Math.min(maximumFrequency, sampleRate / 2) : maximumFrequency
      );
      const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
        label: String(Math.round(axisMaximum * fraction)),
        fraction,
      }));
      // Same right margin as the waveform so both plot areas line up exactly; the color
      // legend lives in the page legend strip instead of inside the plot.
      const plot = plotFrame(context, width, height, range.start, range.end, ticks, "Frequency (Hz)");
      const frames = spectrumFrames(channel, first);
      if (!frames.length || !sampleRate) return;
      const binHeight = plot.height * (sampleRate / fftSize) / axisMaximum;
      const frameWidth = Math.max(1, plot.width * (fftHop / sampleRate * 1000) / (range.end - range.start) + 0.5);
      context.save();
      context.beginPath();
      context.rect(plot.left, plot.top, plot.width, plot.height);
      context.clip();
      frames.forEach((frame) => {
        const x = plot.left + (frame.time - range.start) * plot.width / (range.end - range.start);
        frame.decibels.forEach((decibels, bin) => {
          const frequency = bin * sampleRate / fftSize;
          if (frequency > axisMaximum) return;
          const y = plot.top + plot.height * (1 - frequency / axisMaximum);
          context.fillStyle = heatColor(decibels);
          context.fillRect(x - frameWidth / 2, y - binHeight, frameWidth, Math.max(1, binHeight + 0.5));
        });
      });
      context.restore();
      context.strokeStyle = plotTheme().axis;
      context.strokeRect(plot.left, plot.top, plot.width, plot.height);
    };

    const drawLive = () => {
      const range = visibleRange();
      const first = firstVisibleIndex(range.start);
      const limit = sharedWaveformLimit(first);
      byId("live-scale").textContent = `±${limit.toLocaleString()} counts`;
      channels.forEach((channel) => {
        drawWaveform(document.querySelector(`[data-live-waveform="${channel}"]`), channel, range, first, limit);
        drawSpectrogram(document.querySelector(`[data-live-spectrogram="${channel}"]`), channel, range, first);
      });
    };

    const refreshLive = async () => {
      if (paused || loading) return;
      loading = true;
      try {
        const parameters = new URLSearchParams({sample_rate: String(requestedRate)});
        if (live.sequences.length) {
          parameters.set("after_sequence", String(live.sequences[live.sequences.length - 1]));
        }
        const response = await fetch(`${liveUrl}?${parameters}`, {headers: {Accept: "application/json"}, cache: "no-store"});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        setLiveStatus(payload);
        appendPayload(payload);
        drawLive();
      } catch (error) {
        byId("live-status-text").textContent = "Live request failed";
        byId("live-status-dot").className = "status-dot status-red";
        errorBox.textContent = error instanceof Error ? error.message : String(error);
        errorBox.classList.remove("hidden");
      } finally {
        loading = false;
      }
    };

    windowSelect.addEventListener("change", () => {
      visibleSeconds = Math.min(maximumHistory, Number(windowSelect.value));
      drawLive();
    });
    window.addEventListener("shakepie-theme-change", drawLive);
    toggle.addEventListener("click", () => {
      paused = !paused;
      toggle.textContent = paused ? "Resume" : "Pause";
      toggle.setAttribute("aria-pressed", String(paused));
      if (!paused) refreshLive();
    });
    window.addEventListener("resize", drawLive);
    refreshLive();
    window.setInterval(refreshLive, refreshMilliseconds);
  }

  const recordingsUrl = document.body.dataset.recordingsUrl;
  if (recordingsUrl) {
    const channels = ["north", "east", "vertical"];
    const dateInput = byId("recorded-date");
    const recordingSelect = byId("recorded-file");
    const plotButton = byId("recorded-plot");
    const message = byId("recorded-message");
    const summary = byId("recorded-summary");
    const plotSection = byId("recorded-plots");
    const requestedRecording = document.body.dataset.recordingRequested;
    const requestedAt = document.body.dataset.recordedAt || "";
    const plotUrlTemplate = document.body.dataset.recordingPlotUrl;
    let plotData = null;
    let activeRequest = null;
    let requestVersion = 0;
    let recordingOptions = [];

    const syncUrl = () => {
      const url = new URL(window.location.href);
      url.searchParams.set("date", dateInput.value || "");
      if (recordingSelect.value) url.searchParams.set("recording", recordingSelect.value);
      else url.searchParams.delete("recording");
      url.searchParams.delete("at");
      window.history.replaceState(null, "", url.toString());
    };

    const recordingForTime = (recordings, at) => {
      let pick = recordings.find(
        (item) => item.start.slice(11, 19) <= at && at <= item.end.slice(11, 19)
      );
      if (!pick) {
        pick = [...recordings].reverse().find((item) => item.start.slice(11, 19) <= at);
      }
      return pick;
    };

    const showMessage = (text, level = "muted") => {
      message.textContent = text;
      message.className = `helper-text helper-${level}`;
    };

    const formatBytes = (value) => {
      let amount = Number(value);
      const units = ["B", "KiB", "MiB", "GiB", "TiB"];
      let unit = 0;
      while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
      return `${amount.toFixed(1)} ${units[unit]}`;
    };

    const compactDuration = (value) => {
      const [hours, minutes, seconds] = value.split(":").map(Number);
      const parts = [];
      if (hours) parts.push(`${hours}h`);
      if (minutes) parts.push(`${minutes}m`);
      parts.push(`${seconds}s`);
      return parts.join(" ");
    };

    const utcDateTime = (timestamp) => new Date(timestamp).toISOString()
      .replace("T", " ").replace(".000Z", " UTC");

    const clearRecordedPlot = () => {
      requestVersion += 1;
      if (activeRequest) activeRequest.abort();
      activeRequest = null;
      plotData = null;
      summary.classList.add("hidden");
      plotSection.classList.add("hidden");
      ["hodogram-figure", "hv-figure"].forEach((figureId) => {
        const figure = byId(figureId);
        if (figure) figure.classList.add("hidden");
      });
    };

    const utcTick = (timestamp) => new Date(timestamp).toISOString().slice(11, 19);

    const showSelectedMetadata = () => {
      const selected = recordingOptions.find((item) => String(item.id) === recordingSelect.value);
      if (!selected) return;
      byId("recorded-start").textContent = selected.start;
      byId("recorded-end").textContent = selected.end;
      byId("recorded-duration").textContent = compactDuration(selected.duration);
      byId("recorded-size").textContent = selected.size;
      byId("recorded-samples").textContent = selected.samples.toLocaleString();
      byId("recorded-rate").textContent = selected.rate_hz ? `${selected.rate_hz.toFixed(1)} Hz` : "—";
      byId("recorded-gaps").textContent = selected.gaps.toLocaleString();
      byId("recorded-validation").textContent = selected.validation;
      byId("recorded-name").textContent = selected.filename;
      summary.classList.remove("hidden");
    };

    const recordedLimit = (data) => {
      let peak = 1;
      channels.forEach((channel) => {
        ["minimum", "maximum"].forEach((kind) => {
          data.waveform[channel][kind].forEach((value) => {
            if (value !== null) peak = Math.max(peak, Math.abs(value));
          });
        });
      });
      return niceLimit(peak * 1.05);
    };

    const drawRecordedWaveform = (canvas, channel, data, limit) => {
      const {context, width, height} = canvasContext(canvas);
      const theme = plotTheme();
      const start = data.recording.start_ms;
      const end = data.recording.end_ms;
      const ticks = [-limit, -limit / 2, 0, limit / 2, limit].map((value) => ({
        label: Math.abs(value) >= 1000 ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)}k` : String(value),
        fraction: (value + limit) / (2 * limit),
      }));
      const plot = plotFrame(context, width, height, start, end, ticks, "ADC counts", utcTick);
      const times = data.waveform.times_ms;
      const minima = data.waveform[channel].minimum;
      const maxima = data.waveform[channel].maximum;
      context.save();
      context.beginPath();
      context.rect(plot.left, plot.top, plot.width, plot.height);
      context.clip();
      context.strokeStyle = theme.wave;
      context.lineWidth = 1.2;
      context.beginPath();
      for (let index = 0; index < times.length; index += 1) {
        if (minima[index] === null || maxima[index] === null) continue;
        const x = plot.left + (times[index] - start) * plot.width / (end - start);
        const yMinimum = plot.top + plot.height * (limit - minima[index]) / (2 * limit);
        const yMaximum = plot.top + plot.height * (limit - maxima[index]) / (2 * limit);
        context.moveTo(x, yMinimum);
        context.lineTo(x, yMaximum);
      }
      context.stroke();
      context.restore();
    };

    const drawRecordedSpectrogram = (canvas, channel, data) => {
      const {context, width, height} = canvasContext(canvas);
      const start = data.recording.start_ms;
      const end = data.recording.end_ms;
      const plotFrequencyMaximum = Number(data.frequency_max_hz);
      const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
        label: String(Math.round(plotFrequencyMaximum * fraction)),
        fraction,
      }));
      // Same right margin as the recorded waveform so time axes are directly comparable.
      const plot = plotFrame(context, width, height, start, end, ticks, "Frequency (Hz)", utcTick);
      const times = data.spectrogram.times_ms;
      const frequencies = data.spectrogram.frequencies_hz;
      const columns = data.spectrogram[channel];
      const columnWidth = Math.max(1, plot.width / Math.max(1, times.length) + 0.5);
      const frequencyStep = frequencies.length > 1 ? frequencies[1] - frequencies[0] : plotFrequencyMaximum;
      const binHeight = Math.max(1, plot.height * frequencyStep / plotFrequencyMaximum + 0.5);
      context.save();
      context.beginPath();
      context.rect(plot.left, plot.top, plot.width, plot.height);
      context.clip();
      columns.forEach((column, columnIndex) => {
        if (column === null) return;
        const x = plot.left + (times[columnIndex] - start) * plot.width / (end - start);
        column.forEach((decibels, bin) => {
          if (decibels === null) return;
          const y = plot.top + plot.height * (1 - frequencies[bin] / plotFrequencyMaximum);
          context.fillStyle = heatColor(decibels);
          context.fillRect(x - columnWidth / 2, y - binHeight, columnWidth, binHeight);
        });
      });
      context.restore();
      context.strokeStyle = plotTheme().axis;
      context.strokeRect(plot.left, plot.top, plot.width, plot.height);
    };

    const drawRecorded = () => {
      if (!plotData) return;
      const limit = recordedLimit(plotData);
      channels.forEach((channel) => {
        drawRecordedWaveform(
          document.querySelector(`[data-recorded-waveform="${channel}"]`), channel, plotData, limit
        );
        drawRecordedSpectrogram(
          document.querySelector(`[data-recorded-spectrogram="${channel}"]`), channel, plotData
        );
      });
    };

    const loadRecordings = async () => {
      clearRecordedPlot();
      recordingSelect.disabled = true;
      plotButton.disabled = true;
      recordingSelect.replaceChildren(new Option("Loading recordings…", ""));
      showMessage("Loading recordings for the selected UTC day…");
      const version = requestVersion;
      try {
        const response = await fetch(
          `${recordingsUrl}?date=${encodeURIComponent(dateInput.value)}`,
          {headers: {Accept: "application/json"}, cache: "no-store"}
        );
        if (!response.ok) throw new Error(`Could not load recordings (HTTP ${response.status}).`);
        const recordings = await response.json();
        if (version !== requestVersion) return;
        recordingOptions = recordings;
        recordingSelect.replaceChildren();
        if (!recordings.length) {
          recordingSelect.append(new Option("No validated recordings", ""));
          showMessage("No finalized, validated recordings are available for this UTC day.");
          return;
        }
        recordings.forEach((recording) => {
          const option = new Option(
            `${recording.start.slice(11, 19)} – ${recording.end.slice(11, 19)} · ${compactDuration(recording.duration)}`,
            String(recording.id)
          );
          option.title = recording.filename;
          recordingSelect.append(option);
        });
        if (requestedRecording && recordings.some((item) => String(item.id) === requestedRecording)) {
          recordingSelect.value = requestedRecording;
        } else if (requestedAt) {
          const pick = recordingForTime(recordings, requestedAt);
          if (pick) recordingSelect.value = String(pick.id);
        }
        recordingSelect.disabled = false;
        plotButton.disabled = false;
        showMessage(`${recordings.length} recording${recordings.length === 1 ? "" : "s"} available.`);
        showSelectedMetadata();
        syncUrl();
        if ((requestedRecording && recordingSelect.value === requestedRecording) || requestedAt) loadPlot();
      } catch (error) {
        if (version !== requestVersion) return;
        showMessage(error instanceof Error ? error.message : String(error), "error");
      }
    };

    const loadPlot = async () => {
      const recordingId = recordingSelect.value;
      if (!recordingId) return;
      clearRecordedPlot();
      const version = requestVersion;
      activeRequest = new AbortController();
      plotButton.disabled = true;
      plotButton.textContent = "Processing…";
      recordingSelect.disabled = true;
      dateInput.disabled = true;
      showMessage("Validating and processing the selected recording…");
      try {
        const url = plotUrlTemplate.replace("/0/plot", `/${recordingId}/plot`);
        const response = await fetch(url, {
          headers: {Accept: "application/json"},
          cache: "no-store",
          signal: activeRequest.signal,
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `Plot request failed (HTTP ${response.status}).`);
        if (version !== requestVersion || String(payload.recording.id) !== recordingId) return;
        plotData = payload;
        byId("recorded-name").textContent = payload.recording.filename;
        byId("recorded-start").textContent = utcDateTime(payload.recording.start_ms);
        byId("recorded-end").textContent = utcDateTime(payload.recording.end_ms);
        byId("recorded-duration").textContent = compactDuration(
          new Date(payload.recording.duration_seconds * 1000).toISOString().slice(11, 19)
        );
        byId("recorded-size").textContent = formatBytes(payload.recording.size_bytes);
        byId("recorded-samples").textContent = payload.recording.sample_count.toLocaleString();
        byId("recorded-rate").textContent = `${payload.recording.sample_rate_hz.toFixed(1)} Hz`;
        byId("recorded-gaps").textContent = payload.recording.sequence_gaps.toLocaleString();
        byId("recorded-validation").textContent = payload.recording.validation_state === "valid" ? "Valid" : "Invalid";
        summary.classList.remove("hidden");
        plotSection.classList.remove("hidden");
        showMessage("Recording ready. Blank regions mean no data.");
        syncUrl();
        drawRecorded();
        window.dispatchEvent(new Event("shakepie-recording-loaded"));
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (version === requestVersion) showMessage(error instanceof Error ? error.message : String(error), "error");
      } finally {
        if (version === requestVersion) {
          activeRequest = null;
          plotButton.disabled = false;
          plotButton.textContent = "View recording";
          recordingSelect.disabled = false;
          dateInput.disabled = false;
        }
      }
    };

    // MiniSEED export: a direct download link that follows the selected recording.
    const miniseedLink = byId("recorded-miniseed");
    const updateMiniseedLink = () => {
      if (!miniseedLink) return;
      const recordingId = recordingSelect.value;
      if (recordingId) {
        miniseedLink.href = `/recordings/${recordingId}/miniseed`;
        miniseedLink.classList.remove("hidden");
      } else {
        miniseedLink.removeAttribute("href");
        miniseedLink.classList.add("hidden");
      }
    };
    recordingSelect.addEventListener("change", updateMiniseedLink);
    window.addEventListener("shakepie-recording-loaded", updateMiniseedLink);

    // Particle-motion hodogram: equal-aspect N/E scatter colored by time, plus the
    // principal polarization line (a single-station back-azimuth hint for P waves).
    const drawHodogram = (payload) => {
      const canvas = byId("hodogram-canvas");
      const {context, width, height} = canvasContext(canvas);
      const theme = plotTheme();
      const margin = {left: 58, right: 16, top: 12, bottom: 38};
      const side = Math.max(60, Math.min(width - margin.left - margin.right, height - margin.top - margin.bottom));
      const plot = {
        left: margin.left,
        top: margin.top,
        width: side,
        height: side,
      };
      context.fillStyle = theme.bg;
      context.fillRect(0, 0, width, height);
      const points = payload.points || [];
      let peak = 1;
      points.forEach((point) => { peak = Math.max(peak, Math.abs(point[0]), Math.abs(point[1])); });
      const limit = niceLimit(peak * 1.05);
      const ticks = [-limit, -limit / 2, 0, limit / 2, limit].map((value) => ({
        value,
        label: Math.abs(value) >= 1000 ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)}k` : String(value),
        fraction: (value + limit) / (2 * limit),
      }));
      context.font = "12px system-ui, sans-serif";
      context.lineWidth = 1;
      context.strokeStyle = theme.grid;
      context.fillStyle = theme.ink;
      ticks.forEach(({label, fraction}) => {
        const y = plot.top + plot.height * (1 - fraction);
        context.beginPath();
        context.moveTo(plot.left, y);
        context.lineTo(plot.left + plot.width, y);
        context.stroke();
        context.textAlign = "right";
        context.textBaseline = "middle";
        context.fillText(label, plot.left - 8, y);
        const x = plot.left + plot.width * fraction;
        context.beginPath();
        context.moveTo(x, plot.top);
        context.lineTo(x, plot.top + plot.height);
        context.stroke();
        context.textAlign = "center";
        context.textBaseline = "top";
        context.fillText(label, x, plot.top + plot.height + 8);
      });
      context.strokeStyle = theme.axis;
      context.strokeRect(plot.left, plot.top, plot.width, plot.height);
      context.save();
      context.translate(14, plot.top + plot.height / 2);
      context.rotate(-Math.PI / 2);
      context.textAlign = "center";
      context.textBaseline = "top";
      context.fillText("North (counts)", 0, 0);
      context.restore();
      context.textAlign = "center";
      context.fillText("East (counts)", plot.left + plot.width / 2, plot.top + plot.height + 24);
      byId("hodogram-summary").textContent = points.length
        ? `${Math.round(payload.azimuth_deg)}\u00B0 · linearity ${Number(payload.linearity).toFixed(2)}`
        : "no samples in this window";
      if (!points.length) return;
      const xOf = (value) => plot.left + plot.width * (value + limit) / (2 * limit);
      const yOf = (value) => plot.top + plot.height * (limit - value) / (2 * limit);
      const startTime = points[0][2];
      const endTime = points[points.length - 1][2] || startTime + 1;
      context.save();
      context.beginPath();
      context.rect(plot.left, plot.top, plot.width, plot.height);
      context.clip();
      for (let index = 0; index < points.length; index += 1) {
        const fraction = (points[index][2] - startTime) / (endTime - startTime);
        const mix = (first, second) => Math.round(first + (second - first) * fraction);
        context.fillStyle = `rgb(${mix(23, 235)},${mix(107, 120)},${mix(135, 60)})`;
        context.fillRect(xOf(points[index][1]) - 1.2, yOf(points[index][0]) - 1.2, 2.4, 2.4);
      }
      if (payload.linearity > 0.05) {
        const azimuth = payload.azimuth_deg * Math.PI / 180;
        const direction = {x: Math.sin(azimuth), y: Math.cos(azimuth)};
        const extent = limit;
        context.strokeStyle = theme.axis;
        context.lineWidth = 1.4;
        context.setLineDash([6, 4]);
        context.beginPath();
        context.moveTo(xOf(-direction.x * extent), yOf(-direction.y * extent));
        context.lineTo(xOf(direction.x * extent), yOf(direction.y * extent));
        context.stroke();
        context.setLineDash([]);
      }
      context.restore();
    };

    // H/V spectral ratio: log-log plot (the standard H/V presentation). Both axes
    // span decades, so a resonance peak stays clearly visible even when the ratio
    // covers a wide range; the peak is marked and echoed in the caption.
    const drawHvRatio = (payload) => {
      const canvas = byId("hv-canvas");
      const {context, width, height} = canvasContext(canvas);
      const theme = plotTheme();
      const margin = {left: 58, right: 16, top: 12, bottom: 38};
      const plot = {
        left: margin.left,
        top: margin.top,
        width: width - margin.left - margin.right,
        height: height - margin.top - margin.bottom,
      };
      context.fillStyle = theme.bg;
      context.fillRect(0, 0, width, height);
      const frequencies = payload.frequencies_hz || [];
      const ratios = payload.h_over_v || [];
      byId("hv-summary").textContent = payload.peak_frequency_hz
        ? `${payload.peak_frequency_hz} Hz`
        : "none in 0.2–20 Hz";
      if (!frequencies.length) return;
      const logMin = Math.log10(frequencies[0]);
      const logMax = Math.log10(frequencies[frequencies.length - 1]);
      let ratioMin = Infinity;
      let ratioMax = 0;
      ratios.forEach((value) => {
        if (value > 0) {
          ratioMin = Math.min(ratioMin, value);
          ratioMax = Math.max(ratioMax, value);
        }
      });
      if (!Number.isFinite(ratioMin) || logMax <= logMin) return;
      const span = Math.max(0.6, Math.log10(ratioMax) - Math.log10(ratioMin));
      const yLow = Math.log10(ratioMin) - span * 0.08;
      const yHigh = Math.log10(ratioMax) + span * 0.18;
      const xOf = (frequency) => plot.left + plot.width * (Math.log10(frequency) - logMin) / (logMax - logMin);
      const yOf = (value) => plot.top + plot.height * (1 - (Math.log10(value) - yLow) / (yHigh - yLow));
      context.font = "12px system-ui, sans-serif";
      context.lineWidth = 1;
      context.strokeStyle = theme.grid;
      context.fillStyle = theme.ink;
      // 1-2-5 ticks per decade on the ratio axis; the largest mantissa gap
      // (0.4 decade) is smaller than the minimum span, so a tick always shows.
      for (let decade = Math.floor(yLow); decade <= Math.ceil(yHigh); decade += 1) {
        [1, 2, 5].forEach((mantissa) => {
          const value = mantissa * 10 ** decade;
          const log = Math.log10(value);
          if (log < yLow || log > yHigh) return;
          const y = yOf(value);
          context.beginPath();
          context.moveTo(plot.left, y);
          context.lineTo(plot.left + plot.width, y);
          context.stroke();
          context.textAlign = "right";
          context.textBaseline = "middle";
          context.fillText(String(value), plot.left - 8, y);
        });
      }
      [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100].forEach((frequency) => {
        const log = Math.log10(frequency);
        if (log < logMin || log > logMax) return;
        const x = xOf(frequency);
        context.beginPath();
        context.moveTo(x, plot.top);
        context.lineTo(x, plot.top + plot.height);
        context.stroke();
        context.textAlign = "center";
        context.textBaseline = "top";
        context.fillText(String(frequency), x, plot.top + plot.height + 8);
      });
      context.strokeStyle = theme.axis;
      context.strokeRect(plot.left, plot.top, plot.width, plot.height);
      context.save();
      context.translate(14, plot.top + plot.height / 2);
      context.rotate(-Math.PI / 2);
      context.textAlign = "center";
      context.textBaseline = "top";
      context.fillText("H/V ratio (log)", 0, 0);
      context.restore();
      context.textAlign = "center";
      context.fillText("Frequency (Hz)", plot.left + plot.width / 2, plot.top + plot.height + 24);
      context.save();
      context.beginPath();
      context.rect(plot.left, plot.top, plot.width, plot.height);
      context.clip();
      context.strokeStyle = theme.wave;
      context.lineWidth = 1.6;
      context.beginPath();
      frequencies.forEach((frequency, index) => {
        const x = xOf(frequency);
        const y = yOf(ratios[index]);
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      });
      context.stroke();
      if (payload.peak_frequency_hz) {
        context.fillStyle = "#e07830";
        context.beginPath();
        context.arc(xOf(payload.peak_frequency_hz), yOf(ratioMax), 4, 0, Math.PI * 2);
        context.fill();
      }
      context.restore();
    };

    const runAnalysis = async (path, query, draw, figureId, button) => {
      const recordingId = recordingSelect.value;
      if (!recordingId) return;
      button.disabled = true;
      const originalText = button.textContent;
      button.textContent = "Computing…";
      try {
        const response = await fetch(
          `/recordings/${recordingId}/${path}${query}`,
          {headers: {Accept: "application/json"}, cache: "no-store"}
        );
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        byId(figureId).classList.remove("hidden");
        draw(payload);
      } catch (error) {
        showMessage(error instanceof Error ? error.message : String(error), "error");
      } finally {
        button.disabled = false;
        button.textContent = originalText;
      }
    };

    const hodogramButton = byId("tool-hodogram");
    if (hodogramButton) {
      hodogramButton.addEventListener("click", () => {
        const start = Math.max(0, Number(byId("hodogram-start")?.value || 0));
        const seconds = Math.min(300, Math.max(1, Number(byId("hodogram-seconds")?.value || 60)));
        runAnalysis(
          "hodogram",
          `?start=${start}&seconds=${seconds}`,
          drawHodogram,
          "hodogram-figure",
          hodogramButton
        );
      });
    }
    const hvButton = byId("tool-hv");
    if (hvButton) {
      hvButton.addEventListener("click", () => {
        runAnalysis("hv", "", drawHvRatio, "hv-figure", hvButton);
      });
    }

    dateInput.addEventListener("change", loadRecordings);
    recordingSelect.addEventListener("change", () => {
      clearRecordedPlot();
      showSelectedMetadata();
      syncUrl();
      showMessage("Selection changed — press View recording.");
    });
    plotButton.addEventListener("click", loadPlot);
    window.addEventListener("resize", drawRecorded);
    window.addEventListener("shakepie-theme-change", drawRecorded);
    loadRecordings();
  }
})();
