// The Find My page. A thin client: it renders from the JSON routes under
// /api/findmy and holds no state the server doesn't already have.
//
// Shared chrome -- banners, dialog builders, formatters, the fetch wrapper --
// comes from /static/*.js, which every page uses. Everything below is this
// page alone: the map, the device sidebar and the alert list.

import { clearError, showError, showFatalError, showWarning } from "/static/banners.js";
import {
  createActions,
  createButton,
  createDialog,
  createField,
  createSelect,
  createSubmitButton,
  sectionTitle,
} from "/static/dialogs.js";
import { formatDistance, formatRelativeTime, formatSeenAt, isFiniteCoordinate } from "/static/format.js";
import { fetchJson } from "/static/http.js";
import { REDUCED_MOTION_QUERY, collapseRow, motionDurationMs } from "/static/motion.js";

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
  // Every JSON route this page uses is namespaced under the feature, so a
  // second page's routes can never collide with these.
  const API = "/api/findmy";
  const HISTORY_LIMIT = 2000;
  const DEFAULT_ZOOM = 16;
  const FALLBACK_CENTER = [0, 0];
  // Web Mercator uses the sphere at the equator, not a mean radius.
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
  // Mirrors MAX_ICON_LENGTH in src/web/findmy/schemas.py, which rejects anything longer.
  const ICON_MAX_LENGTH = 16;
  // How close two clicks on the same alert row must be to count as a double-click.
  const ALERT_DOUBLE_CLICK_MS = 400;
  const DEFAULT_ALERT_THRESHOLD_M = 100;

  // Below this width the sidebar is an off-canvas drawer rather than an
  // always-visible panel -- there isn't room for both the list and a usable map.
  const MOBILE_QUERY = window.matchMedia("(max-width: 640px)");

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
  const selectAllButton = document.getElementById("select-all");
  const selectNoneButton = document.getElementById("select-none");
  const trackEmptyEl = document.getElementById("track-empty");
  const deviceToolbarEl = document.getElementById("device-toolbar");
  const sidebarEl = document.querySelector(".sidebar");
  const sidebarToggleEl = document.getElementById("sidebar-toggle");
  const sidebarBackdropEl = document.getElementById("sidebar-backdrop");
  const alertEmptyEl = document.getElementById("alert-empty");
  const alertAddOpenButton = document.getElementById("alert-add-open");
  const mapStyleOpenButton = document.getElementById("map-style-open");

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
    node.textContent = `${deviceNameFor(deviceId)} · ${formatSeenAt(seenAt)} (${formatRelativeTime(seenAt)})`;
    return node;
  }

  // --- Header: last full poll cycle ---------------------------------------

  function formatLastUpdatedText(isoString) {
    return isoString ? formatRelativeTime(isoString) : "—";
  }

  async function loadStatus() {
    try {
      const { last_updated: lastUpdated } = await fetchJson(`${API}/status`);
      lastUpdatedEl.textContent = formatLastUpdatedText(lastUpdated);
    } catch (error) {
      console.error(`Failed to load ${API}/status`, error);
      lastUpdatedEl.textContent = "—";
    }
  }

  // --- Map -----------------------------------------------------------------

  async function initMap() {
    let center = FALLBACK_CENTER;
    try {
      // Two routes, one for the shell's map keys and one for this page's own
      // settings -- fetched together so the map still opens in one round trip.
      const [shellConfig, pageConfig] = await Promise.all([
        fetchJson("/api/config"),
        fetchJson(`${API}/config`),
      ]);
      const config = { ...shellConfig, ...pageConfig };
      if (!isFiniteCoordinate(config.home_latitude) || !isFiniteCoordinate(config.home_longitude)) {
        throw new Error("Server returned non-numeric home coordinates.");
      }
      state.home = { latitude: config.home_latitude, longitude: config.home_longitude };
      center = [config.home_latitude, config.home_longitude];
      if (!config.telegram_configured) {
        showWarning("Telegram alerts aren't configured on the server -- triggered alerts will only show here.");
      }
      addMapTilerStyles(config.maptiler_key);
      addGoogleStyles(config.google_map_types);
    } catch (error) {
      console.error("Failed to load the map configuration", error);
      showFatalError("Could not load home coordinates; centering the map on (0, 0).");
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

  const OSM_ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
  const CARTO_ATTRIBUTION = `&copy; <a href="https://carto.com/attributions">CARTO</a> ${OSM_ATTRIBUTION}`;
  const MAPTILER_ATTRIBUTION =
    '&copy; <a href="https://www.maptiler.com/copyright/" target="_blank" rel="noopener noreferrer">MapTiler</a> ' +
    OSM_ATTRIBUTION;
  const GOOGLE_ATTRIBUTION = "Map data &copy; Google";

  const MAP_STYLES = [
    {
      key: "voyager",
      label: "Voyager",
      url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
      attribution: CARTO_ATTRIBUTION,
    },
    {
      key: "dark",
      label: "Dark Matter",
      url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
      attribution: CARTO_ATTRIBUTION,
      boostContrast: true,
    },
    {
      key: "positron",
      label: "Positron",
      url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      attribution: CARTO_ATTRIBUTION,
    },
    {
      key: "osm",
      label: "OpenStreetMap",
      url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      attribution: OSM_ATTRIBUTION,
    },
  ];

  // MapTiler tiles are far more reliable than raw OSM (their own CDN, generous
  // free tier), but need an API key -- only offer them once the server says
  // one is configured (see MAPTILER_API_KEY in src/env.py).
  const MAPTILER_STYLES = [
    { key: "maptiler-streets", label: "MapTiler Streets", path: "maps/streets-v2/{z}/{x}/{y}.png" },
    { key: "maptiler-outdoor", label: "MapTiler Outdoor", path: "maps/outdoor-v2/{z}/{x}/{y}.png" },
    { key: "maptiler-satellite", label: "MapTiler Satellite", path: "maps/satellite/{z}/{x}/{y}.jpg" },
    { key: "maptiler-dataviz", label: "MapTiler Dataviz", path: "maps/dataviz/{z}/{x}/{y}.png" },
  ];

  function addMapTilerStyles(key) {
    if (!key) return;
    appendStyles(
      "maptiler-",
      MAPTILER_STYLES.map(({ key: styleKey, label, path }) => ({
        key: styleKey,
        label,
        url: `https://api.maptiler.com/${path}?key=${key}`,
        attribution: MAPTILER_ATTRIBUTION,
      })),
    );
  }

  // Google tiles come through the server rather than a direct CDN URL, and the
  // server owns which types exist -- /api/config reports them
  // (src/maps/google_tiles.py).
  function addGoogleStyles(mapTypes) {
    if (!mapTypes || !mapTypes.length) return;
    appendStyles(
      "google-",
      mapTypes.map(({ type, label }) => ({
        key: `google-${type}`,
        label,
        url: `/api/tiles/google/${type}/{z}/{x}/{y}`,
        attribution: GOOGLE_ATTRIBUTION,
      })),
    );
  }

  // /config can resolve after a re-render, so adding is idempotent on the prefix.
  function appendStyles(keyPrefix, styles) {
    if (MAP_STYLES.some((style) => style.key.startsWith(keyPrefix))) return;
    MAP_STYLES.push(...styles);
    updateMapStyleDialog();
  }

  const MAP_STYLE_KEY = "mapStyle";
  let activeTileLayer = null;

  function loadMapStyleKey() {
    return localStorage.getItem(MAP_STYLE_KEY) || "voyager";
  }

  function applyMapStyle(key) {
    const style = MAP_STYLES.find((candidate) => candidate.key === key) || MAP_STYLES[0];
    if (activeTileLayer) map.removeLayer(activeTileLayer);
    map.getContainer().classList.toggle("map-boost-contrast", Boolean(style.boostContrast));
    activeTileLayer = L.tileLayer(style.url, {
      attribution: style.attribution,
      subdomains: "abcd",
      maxZoom: 20,
    }).addTo(map);
    localStorage.setItem(MAP_STYLE_KEY, style.key);
  }

  let mapStyleDialog = null;
  let mapStyleDialogTrigger = null;

  function buildHistoryToggleRow() {
    const row = document.createElement("label");
    row.className = "dialog-toggle-row";

    const caption = document.createElement("span");
    caption.textContent = "Show history trail";

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "toggle-switch";
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", String(state.showHistory));
    toggle.addEventListener("click", () => {
      state.showHistory = !state.showHistory;
      toggle.setAttribute("aria-checked", String(state.showHistory));
      reloadTracks();
    });

    row.append(caption, toggle);
    return row;
  }

  function buildMapStyleDialog() {
    const dialog = createDialog("map-style-dialog", "Map settings");

    const styleSelect = createSelect(MAP_STYLES.map((style) => [style.key, style.label]));
    styleSelect.addEventListener("change", () => {
      applyMapStyle(styleSelect.value);
      updateMapStyleDialog();
    });

    dialog.append(
      sectionTitle("Map style"),
      styleSelect,
      sectionTitle("Options"),
      buildHistoryToggleRow(),
      createActions(createButton("Close", "btn-ghost", () => dialog.close())),
    );

    dialog.addEventListener("close", () => mapStyleDialogTrigger?.focus());
    return dialog;
  }

  function updateMapStyleDialog() {
    const select = mapStyleDialog?.querySelector(".dialog-select");
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
        // Marching dashes (see .alert-radius in findmy.css) -- the crawl
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

    return [...visibleDevices()].sort((a, b) => {
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

  // Computed server-side (see serialize_location in src/web/findmy/schemas.py) so the CLI's
  // --json output and this view share one distance calculation.
  function distanceMeters(device) {
    return isFiniteCoordinate(device.distance_m) ? device.distance_m : null;
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

  // --- Icon editor dialog ----------------------------------------------------
  //
  // Created once and reused. Focus is moved in on open and restored to the
  // control that opened it on close.

  let iconDialog = null;
  let iconDialogInput = null;
  let iconDialogCaption = null;
  let iconDialogDeviceId = null;
  let iconDialogTrigger = null;

  function buildIconDialog() {
    const dialog = createDialog("icon-dialog", "Set marker emoji");

    const form = document.createElement("form");
    form.className = "dialog-form";
    form.method = "dialog";

    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = ICON_MAX_LENGTH;
    input.autocomplete = "off";
    input.className = "dialog-input icon-dialog-input";

    const field = createField("", input);
    iconDialogCaption = field.querySelector("span");

    form.append(
      field,
      createActions(
        createButton("Clear", "btn-ghost", () => {
          input.value = "";
          form.requestSubmit();
        }),
        createButton("Cancel", "btn-ghost", () => dialog.close()),
        createSubmitButton("Save"),
      ),
    );
    dialog.append(form);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const emoji = input.value.trim() || null;
      dialog.close();
      submitIcon(iconDialogDeviceId, emoji);
    });

    dialog.addEventListener("close", () => iconDialogTrigger?.focus());

    iconDialogInput = input;
    return dialog;
  }

  function openIconDialog(deviceId, triggerElement) {
    const device = deviceFor(deviceId);
    if (!device) return;

    iconDialog ??= buildIconDialog();
    iconDialogDeviceId = deviceId;
    iconDialogTrigger = triggerElement;
    iconDialogCaption.textContent = `Marker emoji for ${device.name}`;
    iconDialogInput.value = device.icon || "";
    iconDialog.showModal();
    iconDialogInput.focus();
  }

  async function submitIcon(deviceId, emoji) {
    try {
      await fetchJson(`${API}/locations/${encodeURIComponent(deviceId)}/icon`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emoji }),
      });
      await loadDevices();
      reloadTracks();
    } catch (error) {
      reportError(error);
    }
  }

  // --- Alerts: movement / enter-radius / exit-radius, configured per device --
  //
  // Configured alerts live in their own sidebar tab (rendered into the same
  // <ul> as the device/item tabs, styled the same way) with a delete button
  // per row. Adding one goes through a single "Add alert" button that opens
  // a focus-managed <dialog>, built once and reused like the icon-editor
  // dialog. Evaluation itself happens server-side
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

  function isAlertFlagged(alert) {
    if (RADIUS_ALERT_TYPES.has(alert.alert_type)) return isRadiusAlertAlarmed(alert);
    if (!alert.triggered_at) return false;
    return Date.now() - new Date(alert.triggered_at).getTime() < ALERT_RECENT_MS;
  }

  function deviceHasActiveAlert(deviceId) {
    return state.alerts.some((alert) => alert.device_id === deviceId && isAlertFlagged(alert));
  }

  // Kept separate from the "last triggered" time (below): the subtitle is a
  // single ellipsis-truncated line, so a combined string loses whichever half
  // runs past the end.
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

  // Built on the same device-row/device-text/device-name/device-subtitle
  // classes as buildDeviceRow(), minus the avatar button -- an alert row
  // isn't selectable or clickable.
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
    avatar.classList.toggle("has-active-alert", isAlertFlagged(alert));
    avatar.textContent = alert.device_icon || "•";
    avatar.setAttribute("aria-hidden", "true");

    const text = document.createElement("span");
    text.className = "device-text";
    const name = document.createElement("span");
    name.className = "device-name";
    name.textContent = alert.device_name;
    const subtitle = document.createElement("span");
    subtitle.className = "device-subtitle";
    subtitle.classList.toggle("is-alert-active", isAlertFlagged(alert));
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

  // Built once and reused, same pattern as the icon-editor dialog. One form
  // serves both adding and editing: `editingId` is the alert being edited, or
  // null while adding (mirrors the create/update branch in the submit handler).
  const alertDialog = {
    element: null,
    deviceSelect: null,
    typeSelect: null,
    anchorField: null,
    anchorSelect: null,
    thresholdInput: null,
    thresholdUnit: null,
    submitButton: null,
    trigger: null,
    editingId: null,
  };

  // Only enter/exit alerts have an anchor point -- movement alerts measure
  // between consecutive fixes, not from a fixed point, so the field is
  // hidden rather than shown-but-irrelevant for that type.
  function updateAlertDialogFieldsForType() {
    const isRadiusAlert = RADIUS_ALERT_TYPES.has(alertDialog.typeSelect.value);
    alertDialog.thresholdUnit.textContent = isRadiusAlert ? "m from anchor" : "m between fixes";
    alertDialog.anchorField.hidden = !isRadiusAlert;
  }

  function buildThresholdField() {
    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.value = String(DEFAULT_ALERT_THRESHOLD_M);
    input.className = "dialog-input alert-threshold-input";

    const unit = document.createElement("span");
    unit.className = "alert-threshold-unit";

    const wrap = document.createElement("span");
    wrap.className = "alert-threshold-wrap";
    wrap.append(input, unit);

    alertDialog.thresholdInput = input;
    alertDialog.thresholdUnit = unit;
    return createField("Threshold", wrap);
  }

  function buildAlertDialog() {
    const dialog = createDialog("alert-dialog", "Add or edit alert");

    const form = document.createElement("form");
    form.className = "dialog-form";
    form.method = "dialog";

    alertDialog.deviceSelect = createSelect([]);
    alertDialog.typeSelect = createSelect(Object.entries(ALERT_TYPE_LABELS));
    alertDialog.anchorSelect = createSelect([
      ["home", "Home"],
      ["current", "Current location"],
    ]);
    alertDialog.anchorField = createField("Measured from", alertDialog.anchorSelect);
    alertDialog.submitButton = createSubmitButton("Add");

    form.append(
      createField("Device", alertDialog.deviceSelect),
      createField("Alert type", alertDialog.typeSelect),
      alertDialog.anchorField,
      buildThresholdField(),
      createActions(
        createButton("Cancel", "btn-ghost", () => dialog.close()),
        alertDialog.submitButton,
      ),
    );
    dialog.append(form);

    alertDialog.typeSelect.addEventListener("change", updateAlertDialogFieldsForType);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const deviceId = alertDialog.deviceSelect.value;
      const thresholdM = Number(alertDialog.thresholdInput.value);
      if (!deviceId || !Number.isFinite(thresholdM) || thresholdM <= 0) return;

      const alertType = alertDialog.typeSelect.value;
      const anchor = RADIUS_ALERT_TYPES.has(alertType) ? alertDialog.anchorSelect.value : "home";
      dialog.close();
      if (alertDialog.editingId != null) {
        updateAlertRequest(alertDialog.editingId, alertType, thresholdM, anchor);
      } else {
        createAlertRequest(deviceId, alertType, thresholdM, anchor);
      }
    });

    dialog.addEventListener("close", () => alertDialog.trigger?.focus());
    return dialog;
  }

  // `existingAlert` is omitted when adding a new alert, and passed when
  // editing one -- the device is fixed for an edit (the API only lets you
  // change type/threshold/anchor), so its dropdown is preselected and
  // disabled rather than left editable.
  function openAlertDialog(triggerElement, preselectDeviceId, existingAlert) {
    if (state.devices.length === 0) return;

    alertDialog.element ??= buildAlertDialog();
    alertDialog.trigger = triggerElement;
    alertDialog.editingId = existingAlert ? existingAlert.id : null;

    alertDialog.deviceSelect.textContent = "";
    for (const device of state.devices) {
      const option = document.createElement("option");
      option.value = device.id;
      option.textContent = device.icon ? `${device.icon} ${device.name}` : device.name;
      alertDialog.deviceSelect.append(option);
    }

    if (existingAlert) {
      alertDialog.deviceSelect.value = String(existingAlert.device_id);
      alertDialog.deviceSelect.disabled = true;
      alertDialog.typeSelect.value = existingAlert.alert_type;
      alertDialog.thresholdInput.value = String(Math.round(existingAlert.threshold_m));
      alertDialog.anchorSelect.value = existingAlert.anchor_lat != null ? "current" : "home";
      alertDialog.submitButton.textContent = "Save";
    } else {
      alertDialog.deviceSelect.disabled = false;
      if (preselectDeviceId != null) alertDialog.deviceSelect.value = String(preselectDeviceId);
      alertDialog.typeSelect.value = "movement";
      alertDialog.thresholdInput.value = String(DEFAULT_ALERT_THRESHOLD_M);
      alertDialog.anchorSelect.value = "home";
      alertDialog.submitButton.textContent = "Add";
    }
    updateAlertDialogFieldsForType();

    alertDialog.element.showModal();
    alertDialog.deviceSelect.focus();
  }

  async function createAlertRequest(deviceId, alertType, thresholdM, anchor = "home") {
    try {
      const created = await fetchJson(`${API}/alerts`, {
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
      reportError(error);
    }
  }

  async function updateAlertRequest(alertId, alertType, thresholdM, anchor = "home") {
    try {
      await fetchJson(`${API}/alerts/${alertId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert_type: alertType, threshold_m: thresholdM, anchor }),
      });
      await loadAlerts();
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      reportError(error);
    }
  }

  async function deleteAlertRequest(alertId, row) {
    try {
      // Collapse the row while the DELETE is still in flight so the click
      // feels immediate, but hold the re-render until both have finished --
      // renderDeviceList() replaces every row, which would otherwise cut the
      // animation off on its first frame.
      const collapsed = row ? collapseRow(row) : Promise.resolve();
      await fetchJson(`${API}/alerts/${alertId}`, { method: "DELETE" });
      await loadAlerts();
      await collapsed;
      renderDeviceList();
      reloadTracks();
    } catch (error) {
      // Rebuild from state.alerts so a failed delete doesn't strand a
      // half-collapsed row with inline styles on it.
      renderDeviceList();
      reportError(error);
    }
  }

  // No local try/catch: a failed fetch here should surface through the same
  // error banner as loadDevices()/loadTracks() failures, not get swallowed
  // into an empty alerts list that reads as "you have no alerts configured."
  async function loadAlerts() {
    state.alerts = await fetchJson(`${API}/alerts`);
  }

  // cmd+click (ctrl+click on non-Mac) adds/removes a device from the current
  // selection instead of isolating it -- the only way to view more than one
  // device's track at once.
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
      const isDoubleClick =
        lastAlertRowClick?.alertId === alertId && now - lastAlertRowClick.time < ALERT_DOUBLE_CLICK_MS;
      if (isDoubleClick) {
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

  alertAddOpenButton.addEventListener("click", () => openAlertDialog(alertAddOpenButton));

  mapStyleOpenButton.addEventListener("click", () => openMapStyleDialog(mapStyleOpenButton));

  // --- Mobile sidebar drawer ----------------------------------------------
  //
  // Below MOBILE_QUERY the sidebar is an off-canvas drawer (translated out of
  // view, see findmy.css) opened via the hamburger button, rather than the
  // always-visible panel desktop gets -- there isn't room for both the list
  // and a usable map at once.
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

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebarEl.classList.contains("is-open")) {
      setSidebarOpen(false);
      sidebarToggleEl.focus();
    }
  });

  // A resize past the breakpoint (e.g. rotating to landscape) shouldn't leave
  // a desktop-width sidebar stuck in the "open" drawer state.
  MOBILE_QUERY.addEventListener("change", () => setSidebarOpen(false));

  // loadAlerts() runs alongside loadDevices()/loadStatus() rather than after --
  // renderDeviceList() runs again once all three land so the sidebar's alert
  // highlight isn't one refresh cycle stale, since loadDevices() alone renders
  // before loadAlerts() may have resolved.
  async function refreshAll() {
    await Promise.all([loadDevices(), loadStatus(), loadAlerts()]);
    renderDeviceList();
    await loadTracks();
  }

  // --- Data loading ---------------------------------------------------------

  async function loadDevices() {
    const devices = await fetchJson(`${API}/locations`);
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
    const points = await fetchJson(`${API}/locations/${encodeURIComponent(deviceId)}/history?${params}`, { signal });
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
    loadTracks().catch(reportError);
  }

  function reportError(error) {
    if (error?.name === "AbortError") return;
    console.error(error);
    showError(error instanceof Error ? error.message : String(error));
  }

  initMap()
    .then(() => {
      setActiveTab(state.activeTab, { animatePill: false });
      updateSortIndicators();
      return refreshAll();
    })
    .catch(reportError);

  // The poller writes independently of anyone viewing the dashboard, so keep
  // the view honest for a page left open across several fetch cycles -- but
  // only while the tab is actually visible, so a backgrounded tab doesn't
  // keep polling forever.
  setInterval(() => {
    if (document.visibilityState !== "visible") return;
    refreshAll().catch(reportError);
  }, STATUS_POLL_MS);
})();
