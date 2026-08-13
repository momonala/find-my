(() => {
  "use strict";

  // Colorblind-safe qualitative palette (Okabe-Ito) -- colors carry meaning
  // here (device identity), not decoration. Each entry pairs a background with
  // a foreground that's readable on it: several of these swatches (notably the
  // yellow) fail contrast against a flat white monogram.
  const PALETTE = [
    { bg: "#0072B2", fg: "#ffffff" },
    { bg: "#D55E00", fg: "#ffffff" },
    { bg: "#009E73", fg: "#ffffff" },
    { bg: "#CC79A7", fg: "#17181a" },
    { bg: "#E69F00", fg: "#17181a" },
    { bg: "#56B4E9", fg: "#17181a" },
    { bg: "#F0E442", fg: "#17181a" },
    { bg: "#999999", fg: "#17181a" },
  ];
  const HISTORY_LIMIT = 2000;
  const DEFAULT_ZOOM = 16;
  const FALLBACK_CENTER = [0, 0];
  const EARTH_RADIUS_M = 6371008.8;
  const STATUS_POLL_MS = 30_000;
  const TAB_KEYS = ["device", "item", "alert"];
  // A movement alert is an instantaneous event, not a standing state (unlike
  // proximity, which carries `is_active`) -- this is how long its marker
  // highlight and "Triggered" status stay shown after the fact.
  const ALERT_RECENT_MS = 10 * 60 * 1000;

  const state = {
    devices: [],
    colorByDeviceId: new Map(),
    selected: new Set(),
    sort: { key: "name", direction: "asc" },
    activeTab: "device",
    home: null,
    showHistory: false,
    alerts: [],
    // Set when an alert row is clicked, so the map keeps showing that one
    // item's full history route and alert-radius circles even though the
    // sidebar stays on the Alerts tab. Cleared by any selection change that
    // didn't come from an alert row.
    alertFocusDeviceId: null,
  };

  let map = null;
  let trackLayerGroup = null;
  let trackAbortController = null;
  // The device ids actually drawn last time, so a same-selection refresh
  // doesn't re-fit the map and discard wherever the user just panned/zoomed to.
  let lastFitDeviceIds = null;

  const lastUpdatedEl = document.getElementById("last-updated");
  const deviceListEl = document.getElementById("device-list");
  const deviceEmptyEl = document.getElementById("device-empty");
  const tabSwitcherEl = document.querySelector(".tab-switcher");
  const sortGroupEl = document.querySelector(".sort-group");
  const timeRangeEl = document.getElementById("time-range");
  const historyToggleEl = document.getElementById("history-toggle");
  const selectAllButton = document.getElementById("select-all");
  const selectNoneButton = document.getElementById("select-none");
  const trackEmptyEl = document.getElementById("track-empty");
  const errorBannerEl = document.getElementById("error-banner");
  const fatalBannerEl = document.getElementById("fatal-banner");
  const deviceToolbarEl = document.getElementById("device-toolbar");
  const alertEmptyEl = document.getElementById("alert-empty");
  const alertAddOpenButton = document.getElementById("alert-add-open");

  // --- Error banners ---------------------------------------------------------
  //
  // Two banners, not one: `fatalBannerEl` holds a standing problem (map init
  // failed) that stays up across refreshes, while `errorBannerEl` holds a
  // per-refresh problem (some tracks failed to load) that the next successful
  // refresh clears. Sharing one element meant loadTracks()'s first line wiped
  // the map-init warning within a tick of it appearing.

  function showError(message) {
    errorBannerEl.textContent = message;
    errorBannerEl.hidden = false;
  }

  function clearError() {
    errorBannerEl.hidden = true;
  }

  function showFatalError(message) {
    fatalBannerEl.textContent = message;
    fatalBannerEl.hidden = false;
  }

  function formatRelativeTime(isoString) {
    if (!isoString) return "no fix yet";
    const timestamp = new Date(isoString).getTime();
    if (Number.isNaN(timestamp)) return "no fix yet";

    const seconds = (Date.now() - timestamp) / 1000;
    if (seconds < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.round(hours / 24);
    return `${days} d ago`;
  }

  function haversineMeters(lat1, lon1, lat2, lon2) {
    const toRad = (deg) => (deg * Math.PI) / 180;
    const halfChord =
      Math.sin((toRad(lat2) - toRad(lat1)) / 2) ** 2 +
      Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin((toRad(lon2) - toRad(lon1)) / 2) ** 2;
    return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(halfChord));
  }

  function formatDistance(meters) {
    if (meters < 1000) return `${Math.round(meters)} meters`;
    return `${(meters / 1000).toFixed(1)} km`;
  }

  function isFiniteCoordinate(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function paletteFor(deviceId) {
    if (!state.colorByDeviceId.has(deviceId)) {
      const index = state.colorByDeviceId.size % PALETTE.length;
      state.colorByDeviceId.set(deviceId, PALETTE[index]);
    }
    return state.colorByDeviceId.get(deviceId);
  }

  function colorForDevice(deviceId) {
    return paletteFor(deviceId).bg;
  }

  function deviceFor(deviceId) {
    return state.devices.find((candidate) => candidate.id === deviceId) || null;
  }

  function deviceNameFor(deviceId) {
    return deviceFor(deviceId)?.name ?? deviceId;
  }

  function monogramFor(name) {
    return (name.trim()[0] || "?").toUpperCase();
  }

  function applyBadgeColors(element, deviceId) {
    const { bg, fg } = paletteFor(deviceId);
    element.style.backgroundColor = bg;
    element.style.color = fg;
  }

  function buildMarkerIcon(device) {
    const badge = document.createElement("span");
    badge.className = "device-marker-badge";
    badge.classList.toggle("has-active-alert", deviceHasActiveAlert(device.id));
    badge.classList.toggle("is-alert-focus", state.alertFocusDeviceId === device.id);
    applyBadgeColors(badge, device.id);
    badge.textContent = device.icon || monogramFor(device.name);
    return L.divIcon({ className: "device-marker", html: badge, iconSize: [28, 28], iconAnchor: [14, 14] });
  }

  function buildTooltipNode(deviceId, seenAt) {
    // Built as a real element rather than an HTML string: Leaflet's tooltip
    // assigns string content via innerHTML, and device names are user-supplied
    // (set in the Find My app on a phone), so a string here would be an XSS
    // sink. An Element is inserted as a node instead.
    const node = document.createElement("span");
    node.textContent = `${deviceNameFor(deviceId)} — ${new Date(seenAt).toLocaleString()}`;
    return node;
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = "";
      try {
        detail = (await response.json())?.description ?? "";
      } catch {
        // Body wasn't JSON (or empty) -- fall back to the status alone.
      }
      throw new Error(`Request to ${url} failed with status ${response.status}${detail ? `: ${detail}` : ""}`);
    }
    if (response.status === 204) return null; // e.g. DELETE /alerts/<id> -- no body to parse.
    return response.json();
  }

  // --- Header: last full poll cycle ---------------------------------------

  async function loadStatus() {
    try {
      const { last_updated: lastUpdated } = await fetchJson("/status");
      lastUpdatedEl.textContent = lastUpdated ? `Last updated ${formatRelativeTime(lastUpdated)}` : "Last updated —";
    } catch (error) {
      console.error("Failed to load /status", error);
      lastUpdatedEl.textContent = "Last updated —";
    }
  }

  // --- Map -----------------------------------------------------------------

  async function initMap() {
    let center = FALLBACK_CENTER;
    try {
      const config = await fetchJson("/config");
      if (!isFiniteCoordinate(config.home_latitude) || !isFiniteCoordinate(config.home_longitude)) {
        throw new Error("Server returned non-numeric home coordinates.");
      }
      state.home = { latitude: config.home_latitude, longitude: config.home_longitude };
      center = [config.home_latitude, config.home_longitude];
    } catch (error) {
      console.error("Failed to load /config", error);
      showFatalError("Could not load home coordinates; centering the map on (0, 0) and hiding distances.");
    }

    // Default zoom control sits top-left, right under the sidebar.
    map = L.map("map", { center, zoom: DEFAULT_ZOOM, zoomControl: false });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    // CartoDB "Dark Matter" -- OpenStreetMap data, simplified and dark to match the app.
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution:
        '&copy; <a href="https://carto.com/attributions">CARTO</a> ' +
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      subdomains: "abcd",
      maxZoom: 20,
    }).addTo(map);
    trackLayerGroup = L.layerGroup().addTo(map);
  }

  function renderTracks(tracksByDevice) {
    trackLayerGroup.clearLayers();

    const allPoints = [...tracksByDevice.values()].flat();
    trackEmptyEl.hidden = allPoints.length > 0;
    trackEmptyEl.textContent =
      state.selected.size === 0
        ? "Select a device to see its track."
        : "No location history for the selected devices in this range.";
    if (allPoints.length === 0) return;

    const allLatLngs = [];

    for (const [deviceId, points] of tracksByDevice) {
      if (points.length === 0) continue;
      const color = colorForDevice(deviceId);
      const isAlertFocus = deviceId === state.alertFocusDeviceId;
      const latLngs = points.map((point) => [point.latitude, point.longitude]);
      allLatLngs.push(...latLngs);

      if (latLngs.length > 1) {
        L.polyline(latLngs, {
          color,
          weight: isAlertFocus ? 5 : 3,
          opacity: isAlertFocus ? 1 : 0.85,
        }).addTo(trackLayerGroup);
      }

      const device = deviceFor(deviceId);

      points.forEach((point, index) => {
        const isLatest = index === points.length - 1;

        // The current position gets a labeled marker (emoji, or a monogram
        // fallback); earlier fixes stay plain dots so the track reads clearly.
        if (isLatest && device) {
          const marker = L.marker([point.latitude, point.longitude], { icon: buildMarkerIcon(device) })
            .addTo(trackLayerGroup)
            .bindTooltip(buildTooltipNode(deviceId, point.seen_at));
          marker.on("click", () => openAlertDialog(marker.getElement(), deviceId));
          return;
        }

        L.circleMarker([point.latitude, point.longitude], {
          radius: 5,
          color: "#ffffff",
          weight: 1,
          fillColor: color,
          fillOpacity: 0.9,
        })
          .addTo(trackLayerGroup)
          .bindTooltip(buildTooltipNode(deviceId, point.seen_at));
      });
    }

    const radiusCircles = renderAlertRadii();

    // Only re-fit when the set of rendered devices (or the alert focus, which
    // adds radius circles the same fit needs to cover) actually changed --
    // doing this on every render would hijack the viewport on each
    // poll-driven refresh, discarding wherever the user just panned or
    // zoomed to.
    const fitKey =
      [...tracksByDevice.keys()].sort().join(",") +
      (state.alertFocusDeviceId ? `|focus:${state.alertFocusDeviceId}` : "");
    if (fitKey !== lastFitDeviceIds) {
      lastFitDeviceIds = fitKey;
      const bounds = L.latLngBounds(allLatLngs);
      for (const circle of radiusCircles) bounds.extend(circle.getBounds());
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 18 });
    }
  }

  // Alerts configured for the item currently focused from the Alerts tab
  // (see state.alertFocusDeviceId), drawn as one radius circle per alert:
  // proximity alerts around home, movement alerts around the device's
  // current location -- movement alerts aren't tied to a fixed point, so
  // "how far it can move before triggering" is the closest visual analog.
  // Scoped to alert-tab focus only, not general device selection, so the
  // map doesn't sprout circles for every ordinary Devices/Items selection.
  function renderAlertRadii() {
    if (!state.alertFocusDeviceId) return [];

    const focusDevice = deviceFor(state.alertFocusDeviceId);
    const focusColor = colorForDevice(state.alertFocusDeviceId);
    const circles = [];

    for (const alert of state.alerts) {
      if (alert.device_id !== state.alertFocusDeviceId) continue;

      let center = null;
      if (alert.alert_type === "proximity" && state.home) {
        center = [state.home.latitude, state.home.longitude];
      } else if (
        alert.alert_type === "movement" &&
        focusDevice &&
        isFiniteCoordinate(focusDevice.latitude) &&
        isFiniteCoordinate(focusDevice.longitude)
      ) {
        center = [focusDevice.latitude, focusDevice.longitude];
      }
      if (!center) continue;

      circles.push(
        L.circle(center, {
          radius: alert.threshold_m,
          color: focusColor,
          weight: 2,
          dashArray: "6 6",
          fillOpacity: 0.06,
        }).addTo(trackLayerGroup),
      );
    }

    return circles;
  }

  // --- Device list: tabs, sorting, rows ------------------------------------

  function visibleDevices() {
    return state.devices.filter((device) => device.source === state.activeTab);
  }

  function sortedDevices() {
    const { key, direction } = state.sort;
    const sign = direction === "asc" ? 1 : -1;

    return visibleDevices().sort((a, b) => {
      if (key === "seen_at") {
        const aTime = a.seen_at ? new Date(a.seen_at).getTime() : -Infinity;
        const bTime = b.seen_at ? new Date(b.seen_at).getTime() : -Infinity;
        return sign * (aTime - bTime);
      }
      if (key === "distance") {
        const aDistance = distanceMeters(a) ?? Infinity;
        const bDistance = distanceMeters(b) ?? Infinity;
        return sign * (aDistance - bDistance);
      }
      return sign * String(a[key]).localeCompare(String(b[key]));
    });
  }

  function sortedAlerts() {
    return [...state.alerts].sort((a, b) => a.device_name.localeCompare(b.device_name));
  }

  function updateSortIndicators() {
    for (const chip of sortGroupEl.querySelectorAll(".sort-chip")) {
      const isActive = chip.dataset.sortKey === state.sort.key;
      chip.classList.toggle("is-active", isActive);
      chip.querySelector(".sort-indicator").textContent = isActive ? (state.sort.direction === "asc" ? "▲" : "▼") : "";
    }
  }

  function setSortKey(key) {
    if (state.sort.key === key) {
      state.sort.direction = state.sort.direction === "asc" ? "desc" : "asc";
    } else {
      state.sort = { key, direction: key === "seen_at" ? "desc" : "asc" };
    }
    renderDeviceList();
  }

  function setActiveTab(tab, { focus = false } = {}) {
    state.activeTab = tab;
    for (const button of tabSwitcherEl.querySelectorAll(".tab-button")) {
      const isActive = button.dataset.tab === tab;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-selected", String(isActive));
      button.tabIndex = isActive ? 0 : -1;
      if (isActive && focus) button.focus();
    }
    deviceListEl.setAttribute("aria-labelledby", `tab-${tab}`);
    deviceToolbarEl.hidden = tab === "alert";
    renderDeviceList();
  }

  function distanceMeters(device) {
    if (!state.home || !isFiniteCoordinate(device.latitude) || !isFiniteCoordinate(device.longitude)) return null;
    return haversineMeters(state.home.latitude, state.home.longitude, device.latitude, device.longitude);
  }

  function distanceLabel(device) {
    const meters = distanceMeters(device);
    return meters === null ? "—" : formatDistance(meters);
  }

  function buildDeviceRow(device) {
    const hasFix = isFiniteCoordinate(device.latitude) && isFiniteCoordinate(device.longitude);

    const li = document.createElement("li");
    li.className = "device-row";
    li.classList.toggle("is-selected", state.selected.has(device.id));
    li.classList.toggle("no-fix", !hasFix);
    li.style.setProperty("--row-accent", colorForDevice(device.id));

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "device-checkbox";
    checkbox.dataset.deviceId = device.id;
    checkbox.checked = state.selected.has(device.id);
    checkbox.setAttribute("aria-label", `Show ${device.name} on the map`);

    const avatarButton = document.createElement("button");
    avatarButton.type = "button";
    avatarButton.className = "avatar-button";
    avatarButton.dataset.action = "edit-icon";
    avatarButton.dataset.deviceId = device.id;
    avatarButton.setAttribute("aria-label", `Set a marker emoji for ${device.name}`);

    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.classList.toggle("has-active-alert", deviceHasActiveAlert(device.id));
    avatar.textContent = device.icon || monogramFor(device.name);
    avatar.setAttribute("aria-hidden", "true");
    avatarButton.append(avatar);

    const main = document.createElement("button");
    main.type = "button";
    main.className = "device-main";
    main.dataset.action = "isolate";
    main.dataset.deviceId = device.id;
    main.setAttribute("aria-label", `Show only ${device.name} on the map`);

    const text = document.createElement("span");
    text.className = "device-text";
    const name = document.createElement("span");
    name.className = "device-name";
    name.textContent = device.name;
    const subtitle = document.createElement("span");
    subtitle.className = "device-subtitle";
    const distance = distanceLabel(device);
    subtitle.textContent =
      distance === "—"
        ? `${device.kind} · ${formatRelativeTime(device.seen_at)}`
        : `${device.kind} · ${formatRelativeTime(device.seen_at)} · ${distance}`;
    text.append(name, subtitle);

    main.append(text);

    li.append(checkbox, avatarButton, main);
    return li;
  }

  function renderDeviceList() {
    deviceListEl.textContent = "";

    if (state.activeTab === "alert") {
      deviceEmptyEl.hidden = true;
      alertEmptyEl.hidden = state.alerts.length > 0;
      for (const alert of sortedAlerts()) {
        deviceListEl.append(buildAlertListRow(alert));
      }
      return;
    }

    alertEmptyEl.hidden = true;
    const devices = sortedDevices();
    deviceEmptyEl.hidden = devices.length > 0;
    updateSortIndicators();

    for (const device of devices) {
      deviceListEl.append(buildDeviceRow(device));
    }
  }

  function isolateDevice(deviceId, { alertFocus = false } = {}) {
    state.selected.clear();
    state.selected.add(deviceId);
    state.alertFocusDeviceId = alertFocus ? deviceId : null;
    renderDeviceList();
    reloadTracks();
  }

  // --- Icon editor: a small focus-managed dialog instead of window.prompt ----
  //
  // window.prompt is blocking, unstyleable, and doesn't return focus to the
  // control that opened it. A <dialog> is created once and reused.

  let iconDialog = null;
  let iconDialogInput = null;
  let iconDialogDeviceId = null;
  let iconDialogTrigger = null;

  function buildIconDialog() {
    const dialog = document.createElement("dialog");
    dialog.className = "icon-dialog";

    const form = document.createElement("form");
    form.method = "dialog";

    const label = document.createElement("label");
    label.className = "icon-dialog-label";
    const labelText = document.createElement("span");
    labelText.id = "icon-dialog-label-text";
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 16;
    input.autocomplete = "off";
    input.className = "icon-dialog-input";
    input.setAttribute("aria-labelledby", "icon-dialog-label-text");
    label.append(labelText, input);

    const actions = document.createElement("div");
    actions.className = "icon-dialog-actions";
    const clearButton = document.createElement("button");
    clearButton.type = "button";
    clearButton.className = "btn-ghost";
    clearButton.textContent = "Clear";
    clearButton.addEventListener("click", () => {
      input.value = "";
      form.requestSubmit();
    });
    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "btn-ghost";
    cancelButton.textContent = "Cancel";
    cancelButton.addEventListener("click", () => dialog.close());
    const saveButton = document.createElement("button");
    saveButton.type = "submit";
    saveButton.className = "btn-floating";
    saveButton.textContent = "Save";
    actions.append(clearButton, cancelButton, saveButton);

    form.append(label, actions);
    dialog.append(form);
    document.body.append(dialog);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const emoji = input.value.trim() || null;
      dialog.close();
      submitIcon(iconDialogDeviceId, emoji);
    });

    dialog.addEventListener("close", () => {
      iconDialogTrigger?.focus();
    });

    iconDialogInput = input;
    return dialog;
  }

  function openIconDialog(deviceId, triggerElement) {
    const device = deviceFor(deviceId);
    if (!device) return;

    iconDialog ??= buildIconDialog();
    iconDialogDeviceId = deviceId;
    iconDialogTrigger = triggerElement;
    iconDialog.querySelector("#icon-dialog-label-text").textContent = `Marker emoji for ${device.name}`;
    iconDialogInput.value = device.icon || "";
    iconDialog.showModal();
    iconDialogInput.focus();
  }

  async function submitIcon(deviceId, emoji) {
    try {
      await fetchJson(`/locations/${encodeURIComponent(deviceId)}/icon`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emoji }),
      });
      await loadDevices();
      reloadTracks();
    } catch (error) {
      handleFatalError(error);
    }
  }

  // --- Alerts: movement / proximity-to-home, configured per device -----------
  //
  // Configured alerts live in their own sidebar tab (rendered into the same
  // <ul> as the device/item tabs, styled the same way) with a delete button
  // per row. Adding one goes through a single "Add alert" button that opens
  // a focus-managed <dialog>, built once and reused like the icon-editor
  // dialog -- keeps the tab itself down to a list plus one button instead of
  // a permanently-visible form. Evaluation itself happens server-side
  // (src/alerts.py, from the poller); the frontend only reads
  // `is_active`/`triggered_at` off GET /alerts and manages config.

  const ALERT_TYPE_LABELS = { movement: "Moves more than", proximity: "Comes within" };

  function deviceHasActiveAlert(deviceId) {
    return state.alerts.some((alert) => {
      if (alert.device_id !== deviceId) return false;
      if (alert.alert_type === "proximity") return alert.is_active;
      if (!alert.triggered_at) return false;
      return Date.now() - new Date(alert.triggered_at).getTime() < ALERT_RECENT_MS;
    });
  }

  function alertStatusText(alert) {
    const triggered = alert.triggered_at ? formatRelativeTime(alert.triggered_at) : null;
    if (alert.alert_type === "proximity") {
      if (alert.is_active) return `Inside — triggered ${triggered}`;
      return triggered ? `OK — last triggered ${triggered}` : "OK";
    }
    return triggered ? `Triggered ${triggered}` : "No alert yet";
  }

  function isAlertCurrentlyFlagged(alert) {
    return alert.alert_type === "proximity" ? alert.is_active : deviceHasActiveAlert(alert.device_id);
  }

  // Built on the same device-row/device-text/device-name/device-subtitle
  // classes as buildDeviceRow(), minus the checkbox and avatar button (an
  // alert row isn't selectable or clickable), so the Alerts tab reads as the
  // same list, not a bolted-on widget.
  function buildAlertListRow(alert) {
    const li = document.createElement("li");
    li.className = "device-row alert-list-row";
    li.dataset.deviceId = String(alert.device_id);
    li.style.setProperty("--row-accent", colorForDevice(alert.device_id));

    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.classList.toggle("has-active-alert", isAlertCurrentlyFlagged(alert));
    avatar.textContent = alert.device_icon || "•";
    avatar.setAttribute("aria-hidden", "true");

    const text = document.createElement("span");
    text.className = "device-text";
    const name = document.createElement("span");
    name.className = "device-name";
    name.textContent = alert.device_name;
    const subtitle = document.createElement("span");
    subtitle.className = "device-subtitle";
    subtitle.classList.toggle("is-alert-active", isAlertCurrentlyFlagged(alert));
    subtitle.textContent = `${ALERT_TYPE_LABELS[alert.alert_type]} ${Math.round(alert.threshold_m)} m · ${alertStatusText(alert)}`;
    text.append(name, subtitle);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "btn-ghost alert-delete-button";
    deleteButton.dataset.action = "delete-alert";
    deleteButton.dataset.alertId = String(alert.id);
    deleteButton.setAttribute("aria-label", `Delete this alert for ${alert.device_name}`);
    deleteButton.textContent = "Delete";

    li.append(avatar, text, deleteButton);
    return li;
  }

  // Built once and reused, same pattern as the icon-editor dialog: a <dialog>
  // holding the device/type/threshold fields that used to sit permanently in
  // the alert toolbar.
  let alertDialog = null;
  let alertDialogDeviceSelect = null;
  let alertDialogTypeSelect = null;
  let alertDialogThresholdInput = null;
  let alertDialogThresholdUnit = null;
  let alertDialogTrigger = null;

  function updateAlertDialogThresholdUnitLabel() {
    alertDialogThresholdUnit.textContent =
      alertDialogTypeSelect.value === "proximity" ? "m from home" : "m between fixes";
  }

  function buildAlertDialog() {
    const dialog = document.createElement("dialog");
    dialog.className = "alert-dialog";

    const form = document.createElement("form");
    form.method = "dialog";

    const deviceLabel = document.createElement("label");
    deviceLabel.className = "alert-dialog-label";
    const deviceLabelText = document.createElement("span");
    deviceLabelText.textContent = "Device";
    const deviceSelect = document.createElement("select");
    deviceSelect.className = "alert-dialog-select";
    deviceLabel.append(deviceLabelText, deviceSelect);

    const typeLabel = document.createElement("label");
    typeLabel.className = "alert-dialog-label";
    const typeLabelText = document.createElement("span");
    typeLabelText.textContent = "Alert type";
    const typeSelect = document.createElement("select");
    typeSelect.className = "alert-dialog-select";
    for (const [value, text] of Object.entries(ALERT_TYPE_LABELS)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      typeSelect.append(option);
    }
    typeLabel.append(typeLabelText, typeSelect);

    const thresholdLabel = document.createElement("label");
    thresholdLabel.className = "alert-dialog-label";
    const thresholdLabelText = document.createElement("span");
    thresholdLabelText.textContent = "Threshold";
    const thresholdWrap = document.createElement("span");
    thresholdWrap.className = "alert-threshold-wrap";
    const thresholdInput = document.createElement("input");
    thresholdInput.type = "number";
    thresholdInput.min = "1";
    thresholdInput.step = "1";
    thresholdInput.value = "100";
    thresholdInput.className = "alert-threshold-input";
    const thresholdUnit = document.createElement("span");
    thresholdUnit.className = "alert-threshold-unit";
    thresholdWrap.append(thresholdInput, thresholdUnit);
    thresholdLabel.append(thresholdLabelText, thresholdWrap);

    const actions = document.createElement("div");
    actions.className = "alert-dialog-actions";
    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "btn-ghost";
    cancelButton.textContent = "Cancel";
    cancelButton.addEventListener("click", () => dialog.close());
    const addButton = document.createElement("button");
    addButton.type = "submit";
    addButton.className = "btn-floating";
    addButton.textContent = "Add";
    actions.append(cancelButton, addButton);

    form.append(deviceLabel, typeLabel, thresholdLabel, actions);
    dialog.append(form);
    document.body.append(dialog);

    typeSelect.addEventListener("change", updateAlertDialogThresholdUnitLabel);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const deviceId = deviceSelect.value;
      if (!deviceId) return;
      const thresholdM = Number(thresholdInput.value);
      if (!Number.isFinite(thresholdM) || thresholdM <= 0) return;
      dialog.close();
      createAlertRequest(deviceId, typeSelect.value, thresholdM);
    });

    dialog.addEventListener("close", () => {
      alertDialogTrigger?.focus();
    });

    alertDialogDeviceSelect = deviceSelect;
    alertDialogTypeSelect = typeSelect;
    alertDialogThresholdInput = thresholdInput;
    alertDialogThresholdUnit = thresholdUnit;
    return dialog;
  }

  function openAlertDialog(triggerElement, preselectDeviceId) {
    if (state.devices.length === 0) return;

    alertDialog ??= buildAlertDialog();
    alertDialogTrigger = triggerElement;

    alertDialogDeviceSelect.textContent = "";
    for (const device of state.devices) {
      const option = document.createElement("option");
      option.value = device.id;
      option.textContent = `${device.icon || "❓"} ${device.name}`;
      alertDialogDeviceSelect.append(option);
    }
    if (preselectDeviceId != null) alertDialogDeviceSelect.value = String(preselectDeviceId);

    alertDialogTypeSelect.value = "movement";
    alertDialogThresholdInput.value = "100";
    updateAlertDialogThresholdUnitLabel();

    alertDialog.showModal();
    alertDialogDeviceSelect.focus();
  }

  alertAddOpenButton.addEventListener("click", () => openAlertDialog(alertAddOpenButton));

  async function createAlertRequest(deviceId, alertType, thresholdM) {
    try {
      await fetchJson("/alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: deviceId, alert_type: alertType, threshold_m: thresholdM }),
      });
      await loadAlerts();
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      handleFatalError(error);
    }
  }

  async function deleteAlertRequest(alertId) {
    try {
      await fetchJson(`/alerts/${alertId}`, { method: "DELETE" });
      await loadAlerts();
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      handleFatalError(error);
    }
  }

  // No local try/catch: a failed fetch here should surface through the same
  // error banner as loadDevices()/loadTracks() failures, not get swallowed
  // into an empty alerts list that reads as "you have no alerts configured."
  async function loadAlerts() {
    state.alerts = await fetchJson("/alerts");
  }

  // Event delegation on shared ancestors instead of one listener per row/button.
  deviceListEl.addEventListener("change", (event) => {
    const checkbox = event.target.closest(".device-checkbox");
    if (!checkbox) return;
    const deviceId = checkbox.dataset.deviceId;
    if (checkbox.checked) {
      state.selected.add(deviceId);
    } else {
      state.selected.delete(deviceId);
    }
    state.alertFocusDeviceId = null;
    checkbox.closest(".device-row")?.classList.toggle("is-selected", checkbox.checked);
    reloadTracks();
  });

  deviceListEl.addEventListener("click", (event) => {
    const isolateButton = event.target.closest('button[data-action="isolate"]');
    if (isolateButton) {
      isolateDevice(isolateButton.dataset.deviceId);
      return;
    }
    const iconButton = event.target.closest('button[data-action="edit-icon"]');
    if (iconButton) {
      openIconDialog(iconButton.dataset.deviceId, iconButton);
      return;
    }
    const deleteAlertButton = event.target.closest('button[data-action="delete-alert"]');
    if (deleteAlertButton) {
      deleteAlertRequest(Number(deleteAlertButton.dataset.alertId));
      return;
    }
    const alertRow = event.target.closest(".alert-list-row");
    if (alertRow) {
      // Stays on the Alerts tab (unlike the Devices/Items rows above) --
      // the map highlights the item's full history route and alert-radius
      // circles in place instead of jumping the sidebar away from Alerts.
      isolateDevice(alertRow.dataset.deviceId, { alertFocus: true });
    }
  });

  tabSwitcherEl.addEventListener("click", (event) => {
    const button = event.target.closest(".tab-button");
    if (!button) return;
    setActiveTab(button.dataset.tab);
  });

  tabSwitcherEl.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const currentIndex = TAB_KEYS.indexOf(state.activeTab);
    const delta = event.key === "ArrowRight" ? 1 : -1;
    const nextTab = TAB_KEYS[(currentIndex + delta + TAB_KEYS.length) % TAB_KEYS.length];
    event.preventDefault();
    setActiveTab(nextTab, { focus: true });
  });

  sortGroupEl.addEventListener("click", (event) => {
    const chip = event.target.closest(".sort-chip");
    if (!chip) return;
    setSortKey(chip.dataset.sortKey);
  });

  selectAllButton.addEventListener("click", () => {
    visibleDevices().forEach((device) => state.selected.add(device.id));
    state.alertFocusDeviceId = null;
    renderDeviceList();
    reloadTracks();
  });

  selectNoneButton.addEventListener("click", () => {
    visibleDevices().forEach((device) => state.selected.delete(device.id));
    state.alertFocusDeviceId = null;
    renderDeviceList();
    reloadTracks();
  });

  timeRangeEl.addEventListener("change", () => reloadTracks());

  historyToggleEl.addEventListener("click", () => {
    state.showHistory = !state.showHistory;
    historyToggleEl.setAttribute("aria-pressed", String(state.showHistory));
    reloadTracks();
  });

  function refreshAll() {
    // loadAlerts() runs alongside loadDevices()/loadStatus() rather than
    // after -- renderDeviceList() runs again once all three land so the
    // sidebar's alert highlight isn't one refresh cycle stale, since
    // loadDevices() alone renders before loadAlerts() may have resolved.
    return Promise.all([loadDevices(), loadStatus(), loadAlerts()]).then(() => {
      renderDeviceList();
      return loadTracks();
    });
  }

  // --- Data loading ---------------------------------------------------------

  async function loadDevices() {
    const devices = await fetchJson("/locations");
    const currentIds = new Set(devices.map((device) => device.id));

    devices.forEach((device) => colorForDevice(device.id));

    // Drop selections for devices that no longer exist (e.g. an unpaired
    // AirTag) -- otherwise every later refresh 404s on their history and
    // shows a permanent "failed to load" banner with no way to dismiss it.
    for (const id of state.selected) {
      if (!currentIds.has(id)) state.selected.delete(id);
    }

    state.devices = devices;
    renderDeviceList();
  }

  function sinceParam() {
    const hours = Number(timeRangeEl.value);
    if (hours === 0) return null;
    return new Date(Date.now() - hours * 3600 * 1000).toISOString();
  }

  async function fetchHistory(deviceId, since, signal) {
    // An item focused from the Alerts tab always shows its full history
    // route, regardless of the History toggle -- that's the whole point of
    // clicking an alert instead of just reading its status in the list.
    const showHistory = state.showHistory || deviceId === state.alertFocusDeviceId;
    // With history off, ignore the time-range filter too -- the point is
    // always "wherever the device is right now", not "its latest fix within
    // the selected range" (which could be empty and show nothing).
    const limit = showHistory ? HISTORY_LIMIT : 1;
    const params = new URLSearchParams({ limit: String(limit) });
    if (since && showHistory) params.set("since", since);
    const points = await fetchJson(`/locations/${encodeURIComponent(deviceId)}/history?${params}`, { signal });
    if (points.length === HISTORY_LIMIT) {
      console.warn(`${deviceId}: history capped at ${HISTORY_LIMIT} points; older fixes were not fetched.`);
    }
    return points.slice().reverse(); // API returns newest first; draw oldest to newest.
  }

  async function loadTracks() {
    clearError();

    if (trackAbortController) trackAbortController.abort();
    trackAbortController = new AbortController();
    const { signal } = trackAbortController;

    const since = sinceParam();
    const deviceIds = [...state.selected];

    const results = await Promise.allSettled(deviceIds.map((deviceId) => fetchHistory(deviceId, since, signal)));

    if (signal.aborted) return; // A newer request superseded this one.

    const tracksByDevice = new Map();
    let hadFailure = false;
    results.forEach((result, index) => {
      if (result.status === "fulfilled") {
        tracksByDevice.set(deviceIds[index], result.value);
      } else {
        hadFailure = true;
        console.error(`Failed to load history for ${deviceIds[index]}`, result.reason);
      }
    });

    if (hadFailure) {
      showError("Some devices’ history failed to load; showing what succeeded.");
    }

    renderTracks(tracksByDevice);
  }

  function reloadTracks() {
    loadTracks().catch(handleFatalError);
  }

  function handleFatalError(error) {
    if (error?.name === "AbortError") return;
    console.error(error);
    showError(error instanceof Error ? error.message : String(error));
  }

  initMap()
    .then(() => {
      setActiveTab(state.activeTab);
      updateSortIndicators();
      return Promise.all([loadDevices(), loadStatus(), loadAlerts()]);
    })
    .then(() => {
      renderDeviceList();
      return loadTracks();
    })
    .catch(handleFatalError);

  // The poller writes independently of anyone viewing the dashboard, so keep
  // the view honest for a page left open across several fetch cycles -- but
  // only while the tab is actually visible, so a backgrounded tab doesn't
  // keep polling forever.
  setInterval(() => {
    if (document.visibilityState !== "visible") return;
    refreshAll().catch(handleFatalError);
  }, STATUS_POLL_MS);
})();
