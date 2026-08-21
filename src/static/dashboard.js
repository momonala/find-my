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
  // Web Mercator uses the sphere at the equator, not the mean radius above.
  const EARTH_CIRCUMFERENCE_M = 40_075_016.686;
  // Alert radius rings: roughly how long one dash-plus-gap should be, and how
  // long the dashes take to travel all the way around the ring -- a lap, not a
  // tile, so the apparent spin doesn't change with zoom (see tuneRadiusDashes).
  const TARGET_DASH_PERIOD_PX = 12;
  const MARCH_LAP_SECONDS = 78;
  const STATUS_POLL_MS = 30_000;
  const TAB_KEYS = ["item", "device", "alert"];
  // Which device/item is auto-selected on first load, per tab -- lets the
  // dashboard open with a sensible default view instead of an empty map.
  const DEFAULT_SELECTED_NAME_BY_SOURCE = { item: "Ema" };
  // A movement alert is an instantaneous event, not a standing state (unlike
  // enter/exit, which carry `is_active`) -- this is how long its marker
  // highlight and "Triggered" status stay shown after the fact.
  const ALERT_RECENT_MS = 10 * 60 * 1000;

  const state = {
    devices: [],
    colorByDeviceId: new Map(),
    selected: new Set(),
    sort: { key: "name", direction: "asc" },
    activeTab: "item",
    home: null,
    showHistory: true,
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
  // Markers carrying the permanent "current position" tooltip, so a map
  // click can close them all (see initMap's map.on("click", ...)).
  let latestPositionMarkers = [];
  // Alert radius circles currently on the map, kept so a zoom can refit their
  // dash pattern (see tuneRadiusDashes).
  let alertRadiusCircles = [];
  // The device ids actually drawn last time, so a same-selection refresh
  // doesn't re-fit the map and discard wherever the user just panned/zoomed to.
  let lastFitDeviceIds = null;
  // Only apply the default selection once -- otherwise every poll refresh
  // would stomp on whatever the user has since selected.
  let didApplyDefaultSelection = false;
  // The alert just created, so its row animates in on the next render only.
  // Cleared as soon as that row is built: renderDeviceList() also runs on the
  // 30s poll, and leaving this set would replay the animation every cycle.
  let enteringAlertId = null;

  const lastUpdatedEl = document.getElementById("last-updated");
  const deviceListEl = document.getElementById("device-list");
  const deviceEmptyEl = document.getElementById("device-empty");
  const tabSwitcherEl = document.querySelector(".tab-switcher");
  const tabSwitcherPillEl = document.querySelector(".tab-switcher-pill");
  const sortGroupEl = document.querySelector(".sort-group");
  const timeRangeEl = document.getElementById("time-range");
  let historyToggleEl = null; // built inside the map style dialog
  const selectAllButton = document.getElementById("select-all");
  const selectNoneButton = document.getElementById("select-none");
  const trackEmptyEl = document.getElementById("track-empty");
  const errorBannerEl = document.getElementById("error-banner");
  const fatalBannerEl = document.getElementById("fatal-banner");
  const warningBannerEl = document.getElementById("warning-banner");
  const deviceToolbarEl = document.getElementById("device-toolbar");
  const sidebarEl = document.querySelector(".sidebar");
  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const sidebarBackdropEl = document.getElementById("sidebar-backdrop");
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

  // Standing (not per-refresh) notice, same lifecycle as the fatal banner --
  // set once from /config and left up, since the condition (no Telegram
  // token/chat id in .env) doesn't change without a service restart.
  function showWarning(message) {
    warningBannerEl.textContent = message;
    warningBannerEl.hidden = false;
  }

  function formatRelativeTime(isoString) {
    if (!isoString) return "no fix yet";
    const timestamp = new Date(isoString).getTime();
    if (Number.isNaN(timestamp)) return "no fix yet";

    const seconds = (Date.now() - timestamp) / 1000;
    if (seconds < 60) return "just now";
    const totalMinutes = Math.round(seconds / 60);
    if (totalMinutes < 60) return `${totalMinutes}m ago`;
    const totalHours = Math.floor(totalMinutes / 60);
    const remainderMinutes = totalMinutes % 60;
    if (totalHours < 24) return `${totalHours}h ${remainderMinutes}m ago`;
    const days = Math.floor(totalHours / 24);
    const remainderHours = totalHours % 24;
    return `${days}d ${remainderHours}h ago`;
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

  function formatSeenAt(isoString) {
    const date = new Date(isoString);
    const day = date.getDate();
    const month = date.toLocaleString(undefined, { month: "short" });
    const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    const yearSuffix =
      date.getFullYear() === new Date().getFullYear() ? "" : ` '${String(date.getFullYear()).slice(-2)}`;
    return `${day} ${month}${yearSuffix} · ${time}`;
  }

  function buildTooltipNode(deviceId, seenAt) {
    // Built as a real element rather than an HTML string: Leaflet's tooltip
    // assigns string content via innerHTML, and device names are user-supplied
    // (set in the Find My app on a phone), so a string here would be an XSS
    // sink. An Element is inserted as a node instead.
    const node = document.createElement("span");
    node.textContent = `${deviceNameFor(deviceId)} · ${formatSeenAt(seenAt)} (${formatRelativeTime(seenAt)})`;
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

  // --- Motion --------------------------------------------------------------
  //
  // Read durations back out of the stylesheet rather than repeating them here,
  // so retuning a token in dashboard.css can't leave JS waiting the old
  // amount of time and cutting an animation off part-way.

  const REDUCED_MOTION_QUERY = window.matchMedia("(prefers-reduced-motion: reduce)");

  function motionDurationMs(token) {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
    if (raw.endsWith("ms")) return parseFloat(raw);
    if (raw.endsWith("s")) return parseFloat(raw) * 1000;
    return 0;
  }

  // Slides a list row out and collapses its height so the rows below close the
  // gap. Without the collapse the row would fade in place and everything under
  // it would snap upwards the moment the re-render dropped it -- which is the
  // jank this is here to avoid. Resolves once the row is done animating.
  function collapseRow(row) {
    if (REDUCED_MOTION_QUERY.matches) return Promise.resolve();
    row.style.height = `${row.offsetHeight}px`;
    void row.offsetHeight; // commit the measured height, so collapsing to 0 has something to tween from
    row.classList.add("is-removing");
    row.style.height = "0px";
    return new Promise((resolve) => setTimeout(resolve, motionDurationMs("--duration-quick")));
  }

  // --- Header: last full poll cycle ---------------------------------------

  function formatLastUpdatedText(isoString) {
    return isoString ? formatRelativeTime(isoString) : "—";
  }

  async function loadStatus() {
    try {
      const { last_updated: lastUpdated } = await fetchJson("/status");
      lastUpdatedEl.textContent = formatLastUpdatedText(lastUpdated);
    } catch (error) {
      console.error("Failed to load /status", error);
      lastUpdatedEl.textContent = "—";
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
      if (!config.telegram_configured) {
        showWarning("Telegram alerts aren't configured on the server -- triggered alerts will only show here.");
      }
      addMapTilerStyles(config.maptiler_key);
    } catch (error) {
      console.error("Failed to load /config", error);
      showFatalError("Could not load home coordinates; centering the map on (0, 0) and hiding distances.");
    }

    // Default zoom control sits top-left, right under the sidebar.
    map = L.map("map", { center, zoom: DEFAULT_ZOOM, zoomControl: false });
    L.control.zoom({ position: "bottomright" }).addTo(map);
    applyMapStyle(loadMapStyleKey());
    trackLayerGroup = L.layerGroup().addTo(map);
    // Clicking the map (not a marker) dismisses the default-shown current-
    // position tooltips; they come back on the next render (poll refresh or
    // selection change), which re-binds them permanent again.
    map.on("click", () => {
      latestPositionMarkers.forEach((marker) => marker.closeTooltip());
    });
    // A circle's pixel size changes with zoom, and the dash pattern is fitted
    // to that size (see tuneRadiusDashes), so it has to be refitted after a
    // zoom -- renderTracks doesn't run for a plain zoom.
    map.on("zoomend", () => {
      alertRadiusCircles.forEach(tuneRadiusDashes);
    });
  }

  // --- Map style picker ------------------------------------------------------

  let MAP_STYLES = [
    {
      key: "voyager",
      label: "Voyager",
      url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
      attribution: '&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
    {
      key: "dark",
      label: "Dark Matter",
      url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      attribution: '&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
    {
      key: "positron",
      label: "Positron",
      url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      attribution: '&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
    {
      key: "osm",
      label: "OpenStreetMap",
      url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  ];

  // MapTiler tiles are far more reliable than raw OSM (their own CDN, generous
  // free tier), but need an API key -- only offer them once the server says
  // one is configured (see MAPTILER_API_KEY in src/env.py).
  const MAPTILER_ATTRIBUTION =
    '&copy; <a href="https://www.maptiler.com/copyright/" target="_blank">MapTiler</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  function addMapTilerStyles(key) {
    if (!key || MAP_STYLES.some((s) => s.key.startsWith("maptiler-"))) return;
    MAP_STYLES = [
      ...MAP_STYLES,
      {
        key: "maptiler-streets",
        label: "MapTiler Streets",
        url: `https://api.maptiler.com/maps/streets-v2/{z}/{x}/{y}.png?key=${key}`,
        attribution: MAPTILER_ATTRIBUTION,
      },
      {
        key: "maptiler-outdoor",
        label: "MapTiler Outdoor",
        url: `https://api.maptiler.com/maps/outdoor-v2/{z}/{x}/{y}.png?key=${key}`,
        attribution: MAPTILER_ATTRIBUTION,
      },
      {
        key: "maptiler-satellite",
        label: "MapTiler Satellite",
        url: `https://api.maptiler.com/maps/satellite/{z}/{x}/{y}.jpg?key=${key}`,
        attribution: MAPTILER_ATTRIBUTION,
      },
      {
        key: "maptiler-dataviz",
        label: "MapTiler Dataviz",
        url: `https://api.maptiler.com/maps/dataviz/{z}/{x}/{y}.png?key=${key}`,
        attribution: MAPTILER_ATTRIBUTION,
      },
    ];
    updateMapStyleDialog();
  }

  const MAP_STYLE_KEY = "mapStyle";
  let activeTileLayer = null;

  function loadMapStyleKey() {
    return localStorage.getItem(MAP_STYLE_KEY) || "voyager";
  }

  function applyMapStyle(key) {
    const style = MAP_STYLES.find((s) => s.key === key) || MAP_STYLES[0];
    if (activeTileLayer) map.removeLayer(activeTileLayer);
    activeTileLayer = L.tileLayer(style.url, {
      attribution: style.attribution,
      subdomains: "abcd",
      maxZoom: 20,
    }).addTo(map);
    localStorage.setItem(MAP_STYLE_KEY, style.key);
  }

  let mapStyleDialog = null;
  let mapStyleDialogTrigger = null;

  function buildMapStyleDialog() {
    const dialog = document.createElement("dialog");
    dialog.className = "icon-dialog";

    // Map style section
    const styleTitle = document.createElement("p");
    styleTitle.className = "map-style-dialog-title";
    styleTitle.textContent = "Map style";

    const list = document.createElement("select");
    list.className = "map-style-select";

    for (const style of MAP_STYLES) {
      const option = document.createElement("option");
      option.value = style.key;
      option.textContent = style.label;
      list.append(option);
    }

    list.addEventListener("change", () => {
      applyMapStyle(list.value);
      updateMapStyleDialog();
    });

    // History toggle section
    const historyTitle = document.createElement("p");
    historyTitle.className = "map-style-dialog-title";
    historyTitle.style.marginTop = "var(--space-4)";
    historyTitle.textContent = "Options";

    const historyRow = document.createElement("label");
    historyRow.className = "map-style-toggle-row";

    const historyLabel = document.createElement("span");
    historyLabel.textContent = "Show history trail";

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "toggle-switch";
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", "true");
    toggle.addEventListener("click", () => {
      state.showHistory = !state.showHistory;
      toggle.setAttribute("aria-checked", String(state.showHistory));
      reloadTracks();
    });
    historyToggleEl = toggle;

    historyRow.append(historyLabel, toggle);

    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "btn-ghost";
    cancelButton.textContent = "Close";
    cancelButton.addEventListener("click", () => dialog.close());

    const actions = document.createElement("div");
    actions.className = "icon-dialog-actions";
    actions.append(cancelButton);

    dialog.append(styleTitle, list, historyTitle, historyRow, actions);
    document.body.append(dialog);

    dialog.addEventListener("close", () => mapStyleDialogTrigger?.focus());
    return dialog;
  }

  function updateMapStyleDialog() {
    if (!mapStyleDialog) return;
    const select = mapStyleDialog.querySelector(".map-style-select");
    if (select) select.value = loadMapStyleKey();
  }

  function openMapStyleDialog(triggerElement) {
    mapStyleDialog ??= buildMapStyleDialog();
    mapStyleDialogTrigger = triggerElement;
    updateMapStyleDialog();
    mapStyleDialog.showModal();
  }

  function renderTracks(tracksByDevice) {
    trackLayerGroup.clearLayers();
    latestPositionMarkers = [];

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
            .bindTooltip(buildTooltipNode(deviceId, point.seen_at), { permanent: true, direction: "top" });
          marker.on("click", (event) => {
            L.DomEvent.stopPropagation(event);
            openAlertDialog(marker.getElement(), deviceId);
          });
          latestPositionMarkers.push(marker);
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
  // enter/exit alerts around their own anchor point (a fixed custom point if
  // one was set at creation, else home), movement alerts around the device's
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
      if (RADIUS_ALERT_TYPES.has(alert.alert_type)) {
        if (isFiniteCoordinate(alert.anchor_lat) && isFiniteCoordinate(alert.anchor_lon)) {
          center = [alert.anchor_lat, alert.anchor_lon];
        } else if (state.home) {
          center = [state.home.latitude, state.home.longitude];
        }
      } else if (
        alert.alert_type === "movement" &&
        focusDevice &&
        isFiniteCoordinate(focusDevice.latitude) &&
        isFiniteCoordinate(focusDevice.longitude)
      ) {
        center = [focusDevice.latitude, focusDevice.longitude];
      }
      if (!center) continue;

      const circle = L.circle(center, {
        radius: alert.threshold_m,
        color: focusColor,
        weight: 2,
        fillOpacity: 0.06,
        // Marching dashes (see .alert-radius in dashboard.css) -- the crawl
        // reads as a live perimeter rather than a static annotation. The dash
        // pattern itself is set by tuneRadiusDashes, not here.
        className: "alert-radius",
      }).addTo(trackLayerGroup);

      // Vary the speed a little per ring so several alerts on one device drift
      // out of phase instead of marching as a rigid stack. Derived from the
      // alert id rather than random, so a poll-driven re-render redraws each
      // ring at the same speed it already had.
      circle.marchJitter = ((hashString(String(alert.id)) % 41) - 20) / 100;
      circle.marchReversed = circles.length % 2 === 1;
      tuneRadiusDashes(circle);

      circles.push(circle);
    }

    alertRadiusCircles = circles;
    return circles;
  }

  // Fit the dash pattern to the ring so the dashes tile its circumference a
  // whole number of times. Without that the pattern doesn't meet itself at the
  // start of the path, leaving a seam that reads as the ring resetting once per
  // cycle. Because the tile length varies per ring, so does the distance the
  // animation has to travel to loop -- hence --march-period.
  function tuneRadiusDashes(circle) {
    const path = circle.getElement();
    if (!path) return;

    const pixelRadius = circle.getRadius() / metersPerPixel(circle.getLatLng().lat);
    const circumference = 2 * Math.PI * pixelRadius;
    // Round to whole tiles, but never so few that the dashes read as segments
    // of the circle rather than a dashed line.
    const tiles = Math.max(12, Math.round(circumference / TARGET_DASH_PERIOD_PX));
    const period = circumference / tiles;

    // Time one tile so that a full lap always takes the same wall-clock time,
    // whatever the ring's pixel size. A fixed per-tile duration instead holds
    // the dashes to a fixed px/s, which makes a small (zoomed-out) ring appear
    // to spin faster and faster the further you zoom out.
    const duration = (MARCH_LAP_SECONDS / tiles) * (1 + circle.marchJitter);

    circle.setStyle({ dashArray: `${(period / 2).toFixed(3)} ${(period / 2).toFixed(3)}` });
    path.style.setProperty("--march-period", `${period.toFixed(3)}px`);
    path.style.setProperty("--march-duration", `${duration.toFixed(4)}s`);
    path.style.setProperty("--march-direction", circle.marchReversed ? "reverse" : "normal");
  }

  // Web Mercator ground resolution -- the basemaps are all EPSG:3857, and
  // Leaflet gives no public accessor for this.
  function metersPerPixel(latitude) {
    return (EARTH_CIRCUMFERENCE_M * Math.cos((latitude * Math.PI) / 180)) / 2 ** (map.getZoom() + 8);
  }

  // Small stable hash, used to derive per-alert values that must survive a
  // re-render (unlike Math.random, which would change on every poll).
  function hashString(value) {
    let hash = 0;
    for (let index = 0; index < value.length; index += 1) {
      hash = (hash * 31 + value.charCodeAt(index)) | 0;
    }
    return Math.abs(hash);
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

  // Slides the pill behind the active tab. `animate: false` (first paint,
  // resize) suspends the transition so the pill snaps into place instead of
  // sweeping in from its zero-width starting position.
  function moveTabPill(button, { animate = true } = {}) {
    if (!tabSwitcherPillEl || !button) return;

    const write = () => {
      tabSwitcherPillEl.style.transform = `translateX(${button.offsetLeft}px)`;
      tabSwitcherPillEl.style.width = `${button.offsetWidth}px`;
    };

    if (animate) {
      write();
      return;
    }
    const previousTransition = tabSwitcherPillEl.style.transition;
    tabSwitcherPillEl.style.transition = "none";
    write();
    void tabSwitcherPillEl.offsetWidth; // flush, so restoring below can't animate this write
    tabSwitcherPillEl.style.transition = previousTransition;
  }

  function setActiveTab(tab, { focus = false, animatePill = true } = {}) {
    state.activeTab = tab;
    let activeButton = null;
    for (const button of tabSwitcherEl.querySelectorAll(".tab-button")) {
      const isActive = button.dataset.tab === tab;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-selected", String(isActive));
      button.tabIndex = isActive ? 0 : -1;
      if (isActive) activeButton = button;
      if (isActive && focus) button.focus();
    }
    moveTabPill(activeButton, { animate: animatePill });
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

  // AirTags/trackers only -- src/tracking.py's TrackedItem.battery_level is
  // None for iCloud devices, so this dot never shows for the "device" tab.
  const BATTERY_SLUG = { Full: "full", Medium: "medium", Low: "low", "Very Low": "very-low" };

  function batterySlug(device) {
    return BATTERY_SLUG[device.battery_level] || null;
  }

  function buildDeviceRow(device) {
    const hasFix = isFiniteCoordinate(device.latitude) && isFiniteCoordinate(device.longitude);

    const li = document.createElement("li");
    li.className = "device-row";
    li.classList.toggle("is-selected", state.selected.has(device.id));
    li.classList.toggle("no-fix", !hasFix);
    li.style.setProperty("--row-accent", colorForDevice(device.id));

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
    main.setAttribute(
      "aria-label",
      `Show only ${device.name} on the map -- cmd/ctrl+click to add to the current selection instead`,
    );

    const text = document.createElement("span");
    text.className = "device-text";
    const name = document.createElement("span");
    name.className = "device-name";
    name.textContent = device.name;

    const subtitle = document.createElement("span");
    subtitle.className = "device-subtitle";
    const distance = distanceLabel(device);
    const parts = [device.kind, formatRelativeTime(device.seen_at)];
    if (distance !== "—") parts.push(distance);
    subtitle.append(parts.join(" · "));

    const slug = batterySlug(device);
    if (slug) {
      subtitle.append(" · ");
      const dot = document.createElement("span");
      dot.className = `battery-dot battery-dot--${slug}`;
      dot.title = `Battery: ${device.battery_level}`;
      dot.setAttribute("aria-label", `Battery: ${device.battery_level}`);
      subtitle.append(dot);
    }

    text.append(name, subtitle);

    main.append(text);

    li.append(avatarButton, main);
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
    // Isolating means "show me this one on the map" -- on mobile the drawer
    // is covering that map, so close it rather than leaving the user to.
    if (MOBILE_QUERY.matches) setSidebarOpen(false);
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

  // --- Alerts: movement / enter-radius / exit-radius, configured per device --
  //
  // Configured alerts live in their own sidebar tab (rendered into the same
  // <ul> as the device/item tabs, styled the same way) with a delete button
  // per row. Adding one goes through a single "Add alert" button that opens
  // a focus-managed <dialog>, built once and reused like the icon-editor
  // dialog -- keeps the tab itself down to a list plus one button instead of
  // a permanently-visible form. Evaluation itself happens server-side
  // (src/alerts.py, from the poller, with a cooldown between repeat
  // notifications for the same alert); the frontend only reads
  // `is_active`/`triggered_at` off GET /alerts and manages config.

  const ALERT_TYPE_LABELS = { movement: "Moves more than", enter: "Enters within", exit: "Leaves beyond" };
  const RADIUS_ALERT_TYPES = new Set(["enter", "exit"]);

  // `is_active` always means "currently inside the anchor radius" (see
  // src/alerts.py). That's the alarm condition for `enter` alerts, but for
  // `exit` alerts the alarm condition is the opposite -- currently *outside*.
  function isRadiusAlertAlarmed(alert) {
    return alert.alert_type === "enter" ? alert.is_active : !alert.is_active;
  }

  function deviceHasActiveAlert(deviceId) {
    return state.alerts.some((alert) => {
      if (alert.device_id !== deviceId) return false;
      if (RADIUS_ALERT_TYPES.has(alert.alert_type)) return isRadiusAlertAlarmed(alert);
      if (!alert.triggered_at) return false;
      return Date.now() - new Date(alert.triggered_at).getTime() < ALERT_RECENT_MS;
    });
  }

  // Kept separate from the "last triggered" time (below) rather than one
  // combined string: the combined version got squeezed off the end of the
  // single-line, ellipsis-truncated subtitle, making the triggered time
  // effectively invisible. Splitting it onto its own line keeps it visible.
  function alertStateText(alert) {
    if (RADIUS_ALERT_TYPES.has(alert.alert_type)) {
      if (isRadiusAlertAlarmed(alert)) return alert.alert_type === "enter" ? "Inside" : "Outside";
      return "OK";
    }
    return alert.triggered_at ? "Triggered" : "No alert yet";
  }

  function alertTriggeredText(alert) {
    return alert.triggered_at ? `Last triggered ${formatRelativeTime(alert.triggered_at)}` : null;
  }

  function isAlertCurrentlyFlagged(alert) {
    return RADIUS_ALERT_TYPES.has(alert.alert_type) ? isRadiusAlertAlarmed(alert) : deviceHasActiveAlert(alert.device_id);
  }

  // Built on the same device-row/device-text/device-name/device-subtitle
  // classes as buildDeviceRow(), minus the avatar button (an alert row
  // isn't selectable or clickable), so the Alerts tab reads as the same
  // list, not a bolted-on widget.
  function buildAlertListRow(alert) {
    const li = document.createElement("li");
    li.className = "device-row alert-list-row";
    li.dataset.deviceId = String(alert.device_id);
    li.dataset.alertId = String(alert.id);
    if (alert.id === enteringAlertId) {
      li.classList.add("is-entering");
      enteringAlertId = null;
    }
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
    subtitle.textContent = `${ALERT_TYPE_LABELS[alert.alert_type]} ${Math.round(alert.threshold_m)} m · ${alertStateText(alert)}`;
    text.append(name, subtitle);

    const triggeredText = alertTriggeredText(alert);
    if (triggeredText) {
      const triggered = document.createElement("span");
      triggered.className = "device-triggered";
      triggered.textContent = triggeredText;
      text.append(triggered);
    }

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
  let alertDialogAnchorLabel = null;
  let alertDialogAnchorSelect = null;
  let alertDialogSubmitButton = null;
  let alertDialogTrigger = null;
  // Set to the alert's id while editing an existing alert, null while adding
  // a new one -- the one dialog/form is reused for both (mirrors the create
  // vs update branch in the submit handler below).
  let alertDialogEditingId = null;

  // Only enter/exit alerts have an anchor point -- movement alerts measure
  // between consecutive fixes, not from a fixed point, so the field is
  // hidden rather than shown-but-irrelevant for that type.
  function updateAlertDialogFieldsForType() {
    const isRadiusAlert = RADIUS_ALERT_TYPES.has(alertDialogTypeSelect.value);
    alertDialogThresholdUnit.textContent = isRadiusAlert ? "m from anchor" : "m between fixes";
    alertDialogAnchorLabel.hidden = !isRadiusAlert;
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

    const anchorLabel = document.createElement("label");
    anchorLabel.className = "alert-dialog-label";
    const anchorLabelText = document.createElement("span");
    anchorLabelText.textContent = "Measured from";
    const anchorSelect = document.createElement("select");
    anchorSelect.className = "alert-dialog-select";
    for (const [value, text] of [
      ["home", "Home"],
      ["current", "Current location"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      anchorSelect.append(option);
    }
    anchorLabel.append(anchorLabelText, anchorSelect);

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
    const submitButton = document.createElement("button");
    submitButton.type = "submit";
    submitButton.className = "btn-floating";
    submitButton.textContent = "Add";
    actions.append(cancelButton, submitButton);

    form.append(deviceLabel, typeLabel, anchorLabel, thresholdLabel, actions);
    dialog.append(form);
    document.body.append(dialog);

    typeSelect.addEventListener("change", updateAlertDialogFieldsForType);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const deviceId = deviceSelect.value;
      if (!deviceId) return;
      const thresholdM = Number(thresholdInput.value);
      if (!Number.isFinite(thresholdM) || thresholdM <= 0) return;
      dialog.close();
      const anchor = RADIUS_ALERT_TYPES.has(typeSelect.value) ? anchorSelect.value : "home";
      if (alertDialogEditingId != null) {
        updateAlertRequest(alertDialogEditingId, typeSelect.value, thresholdM, anchor);
      } else {
        createAlertRequest(deviceId, typeSelect.value, thresholdM, anchor);
      }
    });

    dialog.addEventListener("close", () => {
      alertDialogTrigger?.focus();
    });

    alertDialogDeviceSelect = deviceSelect;
    alertDialogTypeSelect = typeSelect;
    alertDialogThresholdInput = thresholdInput;
    alertDialogThresholdUnit = thresholdUnit;
    alertDialogAnchorLabel = anchorLabel;
    alertDialogAnchorSelect = anchorSelect;
    alertDialogSubmitButton = submitButton;
    return dialog;
  }

  // `existingAlert` is omitted when adding a new alert, and passed when
  // double-clicking an alert row to edit it -- the device is fixed for an
  // edit (the API only lets you change type/threshold/anchor), so its
  // dropdown is preselected and disabled rather than left editable.
  function openAlertDialog(triggerElement, preselectDeviceId, existingAlert) {
    if (state.devices.length === 0) return;

    alertDialog ??= buildAlertDialog();
    alertDialogTrigger = triggerElement;
    alertDialogEditingId = existingAlert ? existingAlert.id : null;

    alertDialogDeviceSelect.textContent = "";
    for (const device of state.devices) {
      const option = document.createElement("option");
      option.value = device.id;
      option.textContent = `${device.icon || "❓"} ${device.name}`;
      alertDialogDeviceSelect.append(option);
    }

    if (existingAlert) {
      alertDialogDeviceSelect.value = String(existingAlert.device_id);
      alertDialogDeviceSelect.disabled = true;
      alertDialogTypeSelect.value = existingAlert.alert_type;
      alertDialogThresholdInput.value = String(Math.round(existingAlert.threshold_m));
      alertDialogAnchorSelect.value = existingAlert.anchor_lat != null ? "current" : "home";
      alertDialogSubmitButton.textContent = "Save";
    } else {
      alertDialogDeviceSelect.disabled = false;
      if (preselectDeviceId != null) alertDialogDeviceSelect.value = String(preselectDeviceId);
      alertDialogTypeSelect.value = "movement";
      alertDialogThresholdInput.value = "100";
      alertDialogAnchorSelect.value = "home";
      alertDialogSubmitButton.textContent = "Add";
    }
    updateAlertDialogFieldsForType();

    alertDialog.showModal();
    alertDialogDeviceSelect.focus();
  }

  alertAddOpenButton.addEventListener("click", () => openAlertDialog(alertAddOpenButton));

  document.getElementById("map-style-open").addEventListener("click", (event) => {
    openMapStyleDialog(event.currentTarget);
  });

  async function createAlertRequest(deviceId, alertType, thresholdM, anchor = "home") {
    try {
      const created = await fetchJson("/alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          device_id: deviceId,
          alert_type: alertType,
          threshold_m: thresholdM,
          anchor,
        }),
      });
      // Marks just this row to animate in, so the new alert is findable in a
      // list that's otherwise sorted by device name rather than recency.
      // Optional chained: an endpoint that returns no body simply means no
      // enter animation, not a broken render.
      enteringAlertId = created?.id ?? null;
      await loadAlerts();
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      handleFatalError(error);
    }
  }

  async function updateAlertRequest(alertId, alertType, thresholdM, anchor = "home") {
    try {
      await fetchJson(`/alerts/${alertId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert_type: alertType, threshold_m: thresholdM, anchor }),
      });
      await loadAlerts();
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      handleFatalError(error);
    }
  }

  async function deleteAlertRequest(alertId, row) {
    try {
      // Collapse the row while the DELETE is still in flight so the click
      // feels immediate, but hold the re-render until both have finished --
      // renderDeviceList() replaces every row, which would otherwise cut the
      // animation off on its first frame.
      const collapsed = row ? collapseRow(row) : Promise.resolve();
      await fetchJson(`/alerts/${alertId}`, { method: "DELETE" });
      await loadAlerts();
      await collapsed;
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      // Rebuild from state.alerts so a failed delete doesn't strand a
      // half-collapsed row with inline styles on it.
      renderDeviceList();
      handleFatalError(error);
    }
  }

  // No local try/catch: a failed fetch here should surface through the same
  // error banner as loadDevices()/loadTracks() failures, not get swallowed
  // into an empty alerts list that reads as "you have no alerts configured."
  async function loadAlerts() {
    state.alerts = await fetchJson("/alerts");
  }

  // cmd+click (ctrl+click on non-Mac) adds/removes a device from the current
  // selection instead of isolating it -- the only way to view more than one
  // device's track at once now that there's no per-row checkbox.
  function toggleSelected(deviceId) {
    if (state.selected.has(deviceId)) {
      state.selected.delete(deviceId);
    } else {
      state.selected.add(deviceId);
    }
    state.alertFocusDeviceId = null;
    renderDeviceList();
    reloadTracks();
  }

  // Detected by hand rather than via a native "dblclick" listener: the row's
  // own click handler below calls renderDeviceList(), which tears down and
  // rebuilds every <li> on the first click, so the second click always lands
  // on a fresh element. Browsers key native double-click detection off the
  // clicked element staying the same node, so a real "dblclick" never fires
  // here -- track the alert id + timestamp across clicks instead.
  let lastAlertRowClick = null;

  // On touch, double-click can't work at all: the first tap runs isolateDevice()
  // below, which closes the mobile drawer (see isolateDevice's MOBILE_QUERY
  // check) before a second tap could ever land on the row. Press-and-hold is
  // the touch-native substitute -- it fires from the first and only touch, so
  // there's no dependency on the row still being open for a second tap.
  const ALERT_LONG_PRESS_MS = 500;
  const ALERT_LONG_PRESS_MOVE_TOLERANCE = 10;
  let alertLongPressTimer = null;
  let alertLongPressStart = null;
  let alertLongPressRow = null;
  let alertLongPressTriggered = false;

  function cancelAlertLongPress() {
    clearTimeout(alertLongPressTimer);
    alertLongPressTimer = null;
    alertLongPressStart = null;
    if (alertLongPressRow) alertLongPressRow.classList.remove("is-pressing");
    alertLongPressRow = null;
  }

  deviceListEl.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "touch") return;
    const alertRow = event.target.closest(".alert-list-row");
    if (!alertRow) return;
    alertLongPressTriggered = false;
    alertLongPressStart = { x: event.clientX, y: event.clientY };
    alertLongPressRow = alertRow;
    alertRow.classList.add("is-pressing");
    clearTimeout(alertLongPressTimer);
    alertLongPressTimer = setTimeout(() => {
      alertLongPressTriggered = true;
      cancelAlertLongPress();
      const alertId = alertRow.dataset.alertId;
      const alert = state.alerts.find((candidate) => String(candidate.id) === alertId);
      if (alert) openAlertDialog(alertRow, alert.device_id, alert);
    }, ALERT_LONG_PRESS_MS);
  });

  deviceListEl.addEventListener("pointermove", (event) => {
    if (!alertLongPressStart) return;
    const dx = event.clientX - alertLongPressStart.x;
    const dy = event.clientY - alertLongPressStart.y;
    if (Math.hypot(dx, dy) > ALERT_LONG_PRESS_MOVE_TOLERANCE) cancelAlertLongPress();
  });

  deviceListEl.addEventListener("pointerup", cancelAlertLongPress);
  deviceListEl.addEventListener("pointercancel", cancelAlertLongPress);

  // Event delegation on shared ancestors instead of one listener per row/button.
  deviceListEl.addEventListener("click", (event) => {
    const isolateButton = event.target.closest('button[data-action="isolate"]');
    if (isolateButton) {
      if (event.metaKey || event.ctrlKey) {
        toggleSelected(isolateButton.dataset.deviceId);
      } else {
        isolateDevice(isolateButton.dataset.deviceId);
      }
      return;
    }
    const iconButton = event.target.closest('button[data-action="edit-icon"]');
    if (iconButton) {
      openIconDialog(iconButton.dataset.deviceId, iconButton);
      return;
    }
    const deleteAlertButton = event.target.closest('button[data-action="delete-alert"]');
    if (deleteAlertButton) {
      deleteAlertRequest(
        Number(deleteAlertButton.dataset.alertId),
        deleteAlertButton.closest(".alert-list-row"),
      );
      return;
    }
    const alertRow = event.target.closest(".alert-list-row");
    if (alertRow) {
      if (alertLongPressTriggered) {
        alertLongPressTriggered = false;
        return;
      }
      const alertId = alertRow.dataset.alertId;
      const now = Date.now();
      if (lastAlertRowClick && lastAlertRowClick.alertId === alertId && now - lastAlertRowClick.time < 400) {
        lastAlertRowClick = null;
        const alert = state.alerts.find((candidate) => String(candidate.id) === alertId);
        if (alert) openAlertDialog(alertRow, alert.device_id, alert);
        return;
      }
      lastAlertRowClick = { alertId, time: now };
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

  // The pill's position is measured in px, so it has to be re-measured
  // whenever the switcher's width changes -- without animating, since this
  // isn't a tab change the user is watching.
  window.addEventListener("resize", () => {
    moveTabPill(tabSwitcherEl.querySelector(".tab-button.is-active"), { animate: false });
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


  // --- Mobile sidebar drawer ----------------------------------------------
  //
  // Below MOBILE_QUERY the sidebar is an off-canvas drawer (translated out of
  // view, see dashboard.css) opened via the hamburger button, rather than the
  // always-visible panel desktop gets -- there isn't room for both the list
  // and a usable map at once.
  const MOBILE_QUERY = window.matchMedia("(max-width: 640px)");

  function setSidebarOpen(open) {
    sidebarEl.classList.toggle("is-open", open);
    sidebarBackdropEl.classList.toggle("is-open", open);
    sidebarToggleEl.setAttribute("aria-expanded", String(open));
    // The open drawer covers the toggle's own corner, so it'd otherwise float
    // on top of the sidebar's tab-switcher. Hide it while open and rely on
    // the backdrop tap to close instead.
    sidebarToggleEl.classList.toggle("is-hidden", open);
  }

  sidebarToggleEl.addEventListener("click", () => {
    setSidebarOpen(!sidebarEl.classList.contains("is-open"));
  });

  sidebarBackdropEl.addEventListener("click", () => setSidebarOpen(false));

  // A resize past the breakpoint (e.g. rotating to landscape) shouldn't leave
  // a desktop-width sidebar stuck in the "open" drawer state.
  MOBILE_QUERY.addEventListener("change", () => setSidebarOpen(false));

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

    if (!didApplyDefaultSelection) {
      didApplyDefaultSelection = true;
      for (const device of devices) {
        if (device.name === DEFAULT_SELECTED_NAME_BY_SOURCE[device.source]) {
          state.selected.add(device.id);
        }
      }
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
      setActiveTab(state.activeTab, { animatePill: false });
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
