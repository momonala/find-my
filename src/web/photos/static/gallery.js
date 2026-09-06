// The photo gallery: filters go into the query string, the server does the
// narrowing, and the grid renders whatever comes back.
//
// Thumbnails are <img> requests the browser makes on its own, so the first
// view of a page is paced by the server building them and every later view is
// served from the browser cache.

import { showError, clearError, showFatalError } from "/static/banners.js";
import { formatBytes, formatDate } from "/static/format.js";
import { fetchJson } from "/static/http.js";

const PAGE_SIZE = 120;
const SEARCH_DEBOUNCE_MS = 250;

const form = document.getElementById("filters");
const grid = document.getElementById("grid");
const resultCount = document.getElementById("result-count");
const librarySummary = document.getElementById("library-summary");
const loadMore = document.getElementById("load-more");
const perRow = document.getElementById("per-row");
const gridEmpty = document.getElementById("grid-empty");
const detail = document.getElementById("detail");

let nextOffset = null;

function formatDuration(seconds) {
  if (!seconds) return null;
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  return minutes ? `${minutes}:${String(whole % 60).padStart(2, "0")}` : `${whole}s`;
}

// Flags are buttons rather than form fields because each is tri-state: unset
// means "not filtered", which is not the same as filtering on false. Their
// `aria-pressed` is the only record of that, read back here.
function currentQuery() {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (value) params.set(key, value);
  }
  for (const button of form.querySelectorAll('[data-flag][aria-pressed="true"]')) {
    params.set(button.dataset.flag, button.dataset.value ?? "true");
  }
  return params;
}

// A movie has no still on the drive, and an asset with no file has nothing to
// render at all. Both get a placeholder rather than a broken image.
function canRender(asset) {
  return Boolean(asset.relative_path) && asset.item_type !== "movie";
}

function card(asset) {
  const item = document.createElement("li");
  item.className = "photo-card";
  item.dataset.id = asset.id;
  if (asset.mark === "delete") item.dataset.mark = "delete";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "photo-button";

  if (canRender(asset)) {
    const image = document.createElement("img");
    image.loading = "lazy";
    image.decoding = "async";
    image.src = `/api/photos/assets/${encodeURIComponent(asset.id)}/image?size=grid`;
    image.alt = asset.filename;
    button.append(image);
  } else {
    const placeholder = document.createElement("span");
    placeholder.className = "photo-placeholder";
    placeholder.textContent = asset.item_type === "movie" ? "Video" : "Not backed up";
    button.append(placeholder);
  }

  const caption = document.createElement("span");
  caption.className = "photo-caption";
  const duration = formatDuration(asset.duration_seconds);
  caption.textContent = [formatDate(asset.asset_date), formatBytes(asset.original_bytes), duration]
    .filter(Boolean)
    .join(" · ");
  button.append(caption);

  button.addEventListener("click", () => openDetail(asset.id));
  item.append(button);
  return item;
}

async function loadPage({ append = false } = {}) {
  const params = currentQuery();
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(append ? nextOffset ?? 0 : 0));

  try {
    const page = await fetchJson(`/api/photos/assets?${params}`);
    clearError();
    if (!append) grid.replaceChildren();
    grid.append(...page.assets.map(card));

    nextOffset = page.next_offset;
    loadMore.hidden = nextOffset === null;
    gridEmpty.hidden = page.total > 0;
    resultCount.textContent = `${page.total.toLocaleString()} matching`;
  } catch (error) {
    showError(error.message);
  }
}

// The order the panel reads in. Every column the API returns appears in one of
// these; anything added later that is not listed falls into "Other" rather than
// disappearing, which is what makes "all the metadata" true rather than a claim.
const FACT_GROUPS = [
  ["Capture", ["asset_date", "original_creation_date", "added_date", "import_date",
               "time_zone_name", "time_zone_offset_seconds"]],
  ["Camera", ["camera_make", "camera_model", "lens_model", "f_number", "iso", "focal_length",
              "orientation", "original_orientation"]],
  ["Media", ["item_type", "is_live_photo", "width", "height", "duration_seconds",
             "video_frame_rate", "hdr_type", "asset_subtype_v2", "burst_flags"]],
  ["Storage", ["original_bytes", "icloud_bytes", "relative_path"]],
  ["Location", ["latitude", "longitude", "altitude", "gps_speed", "gps_timestamp"]],
  ["Library", ["source_album", "in_icloud", "icloud_gone_at", "first_seen_at", "mark",
               "is_favorite", "is_hidden", "rating", "title", "description", "keywords", "people"]],
  ["Usage", ["view_count", "play_count", "share_count",
             "imported_by_display_name", "imported_by_bundle_id"]],
  ["Identifiers", ["id", "master_id", "data_class_type", "asset_modified", "master_modified"]],
];

// Rendered separately, not as rows: two get their own section, and the
// filename is the dialog's own heading.
const HANDLED_ELSEWHERE = new Set(["exif", "resources", "filename"]);

const LABELS = {
  id: "Asset id",
  master_id: "Master id",
  iso: "ISO",
  f_number: "Aperture",
  focal_length: "Focal length",
  in_icloud: "Still in iCloud",
  icloud_gone_at: "Left iCloud",
  first_seen_at: "First seen here",
  relative_path: "Path on the drive",
  original_bytes: "Original size",
  icloud_bytes: "iCloud storage used",
  asset_date: "Taken",
  mark: "Marked",
  hdr_type: "HDR type",
  time_zone_offset_seconds: "Time zone offset",
};

const BYTE_KEYS = new Set(["original_bytes", "icloud_bytes"]);
const BOOLEAN_KEYS = new Set(["is_live_photo", "is_favorite", "is_hidden", "in_icloud"]);
const MOMENT_KEYS = new Set([
  "asset_date", "added_date", "original_creation_date", "import_date", "master_modified",
  "asset_modified", "icloud_gone_at", "first_seen_at", "gps_timestamp",
]);

function labelFor(key) {
  if (LABELS[key]) return LABELS[key];
  const words = key.replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}

function formatMoment(isoString) {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  return `${formatDate(isoString)}, ${date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}

// Apple reports the capture zone as an offset in seconds, which reads as a
// four-digit number unless it is put back into the form a clock shows.
function formatOffset(seconds) {
  const sign = seconds < 0 ? "-" : "+";
  const minutes = Math.abs(seconds) / 60;
  return `${sign}${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

// Returns null for anything with nothing to say, so empty fields cost no row.
function formatValue(key, value) {
  if (value === null || value === undefined || value === "") return null;
  if (Array.isArray(value)) return value.length ? value.map(String).join(", ") : null;
  if (BYTE_KEYS.has(key)) return formatBytes(value);
  if (BOOLEAN_KEYS.has(key)) return value ? "Yes" : "No";
  if (MOMENT_KEYS.has(key)) return formatMoment(value);
  if (key === "duration_seconds") return formatDuration(value);
  if (key === "time_zone_offset_seconds") return formatOffset(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function factRows(asset, keys) {
  return keys.flatMap((key) => {
    const value = formatValue(key, asset[key]);
    if (value === null) return [];
    const term = document.createElement("dt");
    term.textContent = labelFor(key);
    const description = document.createElement("dd");
    description.textContent = value;
    return [term, description];
  });
}

function factSection(heading, rows) {
  const section = document.createElement("section");
  section.className = "detail-section";
  const title = document.createElement("h3");
  title.textContent = heading;
  const list = document.createElement("dl");
  list.className = "detail-facts";
  list.append(...rows);
  section.append(title, list);
  return section;
}

function variantSection(resources) {
  if (!resources?.length) return [];
  const rows = resources.flatMap((item) => {
    const term = document.createElement("dt");
    term.textContent = item.kind;
    const description = document.createElement("dd");
    const dimensions = item.width ? `${item.width} × ${item.height}` : "";
    description.textContent = [formatBytes(item.size_bytes), dimensions, item.content_type]
      .filter(Boolean)
      .join(" · ");
    return [term, description];
  });
  return [factSection("Variants in iCloud", rows)];
}

function exifSection(exif) {
  if (!exif) return [];
  const wrapper = document.createElement("details");
  wrapper.className = "detail-section detail-exif";
  const summary = document.createElement("summary");
  summary.textContent = "Raw EXIF";
  const body = document.createElement("pre");
  body.textContent = JSON.stringify(exif, null, 2);
  wrapper.append(summary, body);
  return [wrapper];
}

function renderMeta(asset) {
  const shown = new Set(HANDLED_ELSEWHERE);
  const sections = [];
  for (const [heading, keys] of FACT_GROUPS) {
    keys.forEach((key) => shown.add(key));
    const rows = factRows(asset, keys);
    if (rows.length) sections.push(factSection(heading, rows));
  }
  const leftover = Object.keys(asset).filter((key) => !shown.has(key));
  const leftoverRows = factRows(asset, leftover);
  if (leftoverRows.length) sections.push(factSection("Other", leftoverRows));

  sections.push(...variantSection(asset.resources), ...exifSection(asset.exif));
  document.getElementById("detail-meta").replaceChildren(...sections);
}

async function openDetail(assetId) {
  try {
    const asset = await fetchJson(`/api/photos/assets/${encodeURIComponent(assetId)}`);
    const image = document.getElementById("detail-image");
    image.src = canRender(asset) ? `/api/photos/assets/${encodeURIComponent(asset.id)}/image?size=detail` : "";
    image.hidden = !canRender(asset);
    image.alt = asset.filename;
    document.getElementById("detail-title").textContent = asset.filename;
    renderMeta(asset);

    const mark = document.getElementById("detail-mark");
    mark.dataset.id = asset.id;
    setMarkButton(mark, asset.mark === "delete");
    detail.showModal();
    document.getElementById("detail-meta").scrollTop = 0;
  } catch (error) {
    showError(error.message);
  }
}

function setMarkButton(button, marked) {
  button.setAttribute("aria-pressed", String(marked));
  button.textContent = marked ? "Marked to delete" : "Mark to delete";
}

async function toggleMark(button) {
  const marked = button.getAttribute("aria-pressed") === "true";
  button.disabled = true;
  try {
    const updated = await fetchJson(`/api/photos/assets/${encodeURIComponent(button.dataset.id)}/mark`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mark: marked ? null : "delete" }),
    });
    clearError();
    setMarkButton(button, !marked);
    const card = grid.querySelector(`[data-id="${CSS.escape(button.dataset.id)}"]`);
    // Set the value rather than toggling the attribute: an empty attribute
    // would not match the `[data-mark="delete"]` the outline hangs off.
    if (marked) card?.removeAttribute("data-mark");
    else card?.setAttribute("data-mark", "delete");
    showTotals(updated.totals);
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

// Size first, then only the counts that are actually saying something: a zero
// here is the normal state and does not need a column of its own.
function showTotals(totals) {
  const parts = [`${totals.assets.toLocaleString()} assets`, formatBytes(totals.bytes)];
  const freed = totals.assets - totals.in_icloud;
  if (freed) parts.push(`${freed.toLocaleString()} only on this drive`);
  if (totals.marked) parts.push(`${totals.marked.toLocaleString()} marked to delete`);
  librarySummary.textContent = parts.join(" · ");
}

function fillOptions(name, values, label = (item) => item.value) {
  const select = form.elements[name];
  select.append(
    ...values.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = `${label(item)} (${item.count.toLocaleString()})`;
      return option;
    }),
  );
}

async function start() {
  try {
    const { facets, totals } = await fetchJson("/api/photos/facets");
    fillOptions("camera", facets.cameras);
    fillOptions("year", facets.years);
    fillOptions("album", facets.albums);
    fillOptions("type", facets.types);
    showTotals(totals);
  } catch (error) {
    showFatalError(`${error.message} Is the backup drive mounted?`);
    return;
  }
  applyDensity();
  await loadPage();
}

// Typing fires an input event per character, and each page costs a filtered
// count plus 120 image requests. Selects stay immediate: they change once.
let typingTimer = null;
form.addEventListener("input", (event) => {
  if (event.target === perRow) return; // a view setting, not a filter
  if (event.target.type !== "search") {
    loadPage();
    return;
  }
  clearTimeout(typingTimer);
  typingTimer = setTimeout(() => loadPage(), SEARCH_DEBOUNCE_MS);
});
form.addEventListener("submit", (event) => event.preventDefault());

for (const button of form.querySelectorAll("[data-flag]")) {
  button.addEventListener("click", () => {
    button.setAttribute("aria-pressed", String(button.getAttribute("aria-pressed") !== "true"));
    loadPage();
  });
}

// Density is a view setting, not a filter: the select carries no `name`, so it
// never reaches the query string and never re-queries the library.
function applyDensity() {
  grid.style.gridTemplateColumns = `repeat(${perRow.value}, minmax(0, 1fr))`;
}

perRow.addEventListener("change", applyDensity);

loadMore.addEventListener("click", () => loadPage({ append: true }));
document.getElementById("detail-close").addEventListener("click", () => detail.close());
document.getElementById("detail-mark").addEventListener("click", (event) => toggleMark(event.currentTarget));

start();
