// The photo gallery: filters go into the query string, the server does the
// narrowing, and the grid renders whatever comes back.
//
// Thumbnails are <img> requests the browser makes on its own, so the first
// view of a page is paced by the server building them and every later view is
// served from the browser cache. That first view is slow enough to need saying
// so: the grid fills with pending tiles the moment a query goes out, and each
// one holds its place until its own picture arrives.

import { showError, clearError, showFatalError } from "/static/banners.js";
import { showToast, hideToast } from "/static/toast.js";
import { formatBytes, formatDate } from "/static/format.js";
import { fetchJson } from "/static/http.js";

const PAGE_SIZE = 120;
const SEARCH_DEBOUNCE_MS = 250;
// Enough to fill the view at any density, so a query never looks like it
// returned a short page while it is still running.
const SKELETON_ROWS = 4;
// A sync runs for minutes, so the poll is paced for a progress line, not for a
// value anyone is waiting on.
const REFRESH_POLL_MS = 2000;

const form = document.getElementById("filters");
const grid = document.getElementById("grid");
const resultCount = document.getElementById("result-count");
const librarySummary = document.getElementById("library-summary");
const loadMore = document.getElementById("load-more");
const perRow = document.getElementById("per-row");
const gridEmpty = document.getElementById("grid-empty");
const detail = document.getElementById("detail");
const refreshButton = document.getElementById("refresh");
const pruneButton = document.getElementById("prune");
const pruneConfirm = document.getElementById("prune-confirm");
const markButtons = [...document.querySelectorAll(".detail-mark")];

// What the two levels of mark mean, in the order the confirmation lists them.
const MARK_LINES = [
  ["marked_icloud", "from iCloud, keeping the copy on this drive"],
  ["marked_everywhere", "from iCloud and from this drive"],
];

let nextOffset = null;
let inFlight = null;

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
  return params;
}

// Anything the drive holds bytes for, movies included: the server takes a
// frame from those. An asset with no file has nothing to draw at all, and one
// the server cannot decode says so on its own tile.
function canRender(asset) {
  return Boolean(asset.relative_path);
}

// Why there is no picture, said accurately. "Not backed up" is true only of
// something iCloud still holds -- for a photo you deleted it reads as a
// complaint about the backup rather than the answer, which is that you
// deleted it.
function missingLabel(asset) {
  if (asset.source_album === "Recently Deleted") return "In the trash";
  if (!asset.in_icloud) return "Gone";
  return "Not backed up";
}

// The square a picture will land in, tinted and pulsing until it does.
function pendingFrame() {
  const frame = document.createElement("span");
  frame.className = "photo-frame is-pending";
  return frame;
}

function thumbnail(asset) {
  const frame = pendingFrame();
  const image = document.createElement("img");
  image.className = "photo-thumb";
  image.loading = "lazy";
  image.decoding = "async";
  image.alt = asset.filename;
  image.addEventListener("load", () => {
    frame.classList.remove("is-pending");
    image.classList.add("is-loaded");
  }, { once: true });
  // A thumbnail the server could not build is a fact about that one asset, not
  // a failed page, so it is said on the tile rather than in the error banner.
  image.addEventListener("error", () => {
    frame.classList.remove("is-pending");
    frame.replaceChildren(placeholder("Cannot be rendered"));
  }, { once: true });
  image.src = `/api/photos/assets/${encodeURIComponent(asset.id)}/image?size=grid`;
  frame.append(image);
  return frame;
}

function placeholder(text) {
  const span = document.createElement("span");
  span.className = "photo-placeholder";
  span.textContent = text;
  return span;
}

// Holds a tile's space in the grid while the query that will fill it runs.
function skeletonCard() {
  const item = document.createElement("li");
  item.className = "photo-card photo-skeleton";
  item.setAttribute("aria-hidden", "true");
  const caption = document.createElement("span");
  caption.className = "photo-caption is-pending";
  item.append(pendingFrame(), caption);
  return item;
}

function card(asset) {
  const item = document.createElement("li");
  item.className = "photo-card";
  item.dataset.id = asset.id;
  if (asset.mark) item.dataset.mark = asset.mark;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "photo-button";

  button.append(canRender(asset) ? thumbnail(asset) : placeholder(missingLabel(asset)));

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

// A fresh query replaces the grid with pending tiles; an appended page leaves
// what is on screen alone and says so on the button that asked for it.
function startLoading(append) {
  grid.setAttribute("aria-busy", "true");
  loadMore.hidden = true;
  gridEmpty.hidden = true;
  if (append) {
    loadMore.disabled = true;
    return;
  }
  resultCount.textContent = "Loading…";
  const tiles = Math.min(Number(perRow.value) * SKELETON_ROWS, PAGE_SIZE);
  grid.replaceChildren(...Array.from({ length: tiles }, skeletonCard));
}

function stopLoading() {
  grid.setAttribute("aria-busy", "false");
  loadMore.disabled = false;
  for (const tile of grid.querySelectorAll(".photo-skeleton")) tile.remove();
}

async function loadPage({ append = false } = {}) {
  const params = currentQuery();
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(append ? nextOffset ?? 0 : 0));

  // Changing a filter while a page is still coming back abandons it: the older
  // answer is not wanted, and letting it land would overwrite the newer one.
  inFlight?.abort();
  const request = new AbortController();
  inFlight = request;
  startLoading(append);

  try {
    const page = await fetchJson(`/api/photos/assets?${params}`, { signal: request.signal });
    clearError();
    if (!append) grid.replaceChildren();
    grid.append(...page.assets.map(card));

    nextOffset = page.next_offset;
    loadMore.hidden = nextOffset === null;
    gridEmpty.hidden = page.total > 0;
    resultCount.textContent = `${page.total.toLocaleString()} matching`;
  } catch (error) {
    if (error.name === "AbortError") return; // a newer query owns the grid now
    resultCount.textContent = "";
    showError(error.message);
  } finally {
    if (inFlight === request) {
      inFlight = null;
      stopLoading();
    }
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

// The full-size render costs another conversion, so the figure waits the same
// way a grid tile does rather than holding the last asset's picture.
function showDetailImage(asset) {
  const image = document.getElementById("detail-image");
  const figure = image.closest(".detail-figure");
  image.classList.remove("is-loaded");
  image.hidden = !canRender(asset);
  image.alt = asset.filename;
  figure.classList.toggle("is-pending", canRender(asset));
  image.addEventListener("load", () => {
    figure.classList.remove("is-pending");
    image.classList.add("is-loaded");
  }, { once: true });
  image.addEventListener("error", () => figure.classList.remove("is-pending"), { once: true });
  image.src = canRender(asset) ? `/api/photos/assets/${encodeURIComponent(asset.id)}/image?size=detail` : "";
}

async function openDetail(assetId) {
  try {
    const asset = await fetchJson(`/api/photos/assets/${encodeURIComponent(assetId)}`);
    showDetailImage(asset);
    document.getElementById("detail-title").textContent = asset.filename;
    renderMeta(asset);

    showMarks(asset.id, asset.mark);
    detail.showModal();
    document.getElementById("detail-meta").scrollTop = 0;
  } catch (error) {
    showError(error.message);
  }
}

// Which of the two levels this asset carries, if any. The id rides on the
// group so a button only has to know its own level.
function showMarks(assetId, mark) {
  markButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mark === mark)));
  document.querySelector(".detail-marks").dataset.id = assetId;
}

// Pressing the level an asset already carries clears it: the third state has
// no button of its own because "not marked" is not a decision to record.
async function setMark(button) {
  const assetId = document.querySelector(".detail-marks").dataset.id;
  const was = markButtons.find((item) => item.getAttribute("aria-pressed") === "true")?.dataset.mark ?? null;
  const mark = was === button.dataset.mark ? null : button.dataset.mark;
  markButtons.forEach((item) => { item.disabled = true; });
  try {
    await fetchJson(`/api/photos/assets/${encodeURIComponent(assetId)}/mark`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mark }),
    });
    clearError();
    showMarks(assetId, mark);
    const card = grid.querySelector(`[data-id="${CSS.escape(assetId)}"]`);
    // Set the value rather than toggling the attribute: an empty attribute
    // would not match the `[data-mark]` levels the outline hangs off.
    if (mark) card?.setAttribute("data-mark", mark);
    else card?.removeAttribute("data-mark");
    countMark(was, mark);
  } catch (error) {
    showError(error.message);
  } finally {
    markButtons.forEach((item) => { item.disabled = false; });
  }
}

// Move the header's counts by the one mark that changed. The server could
// recount, but that is a scan of the whole library off a USB disk on a button
// somebody clicks over and over; this is the same arithmetic without the wait.
function countMark(was, now) {
  if (was === now) return;
  const totals = { ...lastTotals };
  if (was) totals[`marked_${was}`] -= 1;
  if (now) totals[`marked_${now}`] += 1;
  totals.marked += (now ? 1 : 0) - (was ? 1 : 0);
  showTotals(totals);
}

// The last totals the server gave, kept up to date here as marks change: the
// count is the only part a click moves, and recounting it costs a scan of the
// whole library off a USB disk.
let lastTotals = { marked: 0 };

// Size first, then only the counts that are actually saying something: a zero
// here is the normal state and does not need a column of its own.
function showTotals(totals) {
  const parts = [`${totals.assets.toLocaleString()} assets`, formatBytes(totals.bytes)];
  const freed = totals.assets - totals.in_icloud;
  if (freed) parts.push(`${freed.toLocaleString()} only on this drive`);
  if (totals.marked) parts.push(`${totals.marked.toLocaleString()} marked to delete`);
  librarySummary.textContent = parts.join(" · ");
  // Ships hidden so an empty line does not sit above the grid while the
  // first totals are still being counted off the drive.
  librarySummary.hidden = false;
  lastTotals = totals;
  pruneButton.hidden = !totals.marked;
  pruneButton.textContent = `Delete ${totals.marked.toLocaleString()}`;
}


function askToPrune() {
  document.getElementById("prune-confirm-lines").replaceChildren(
    ...MARK_LINES.filter(([key]) => lastTotals[key]).map(([key, wording]) => {
      const line = document.createElement("li");
      line.textContent = `${lastTotals[key].toLocaleString()} ${wording}`;
      return line;
    }),
  );
  pruneConfirm.showModal();
}

// The first option is the "any" one the markup ships with, and it is what a
// cleared filter selects, so a reload keeps it and replaces only what follows.
function fillOptions(name, values, label = (item) => item.value) {
  const select = form.elements[name];
  const chosen = select.value;
  while (select.options.length > 1) select.remove(1);
  select.append(
    ...values.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = `${label(item)} (${item.count.toLocaleString()})`;
      return option;
    }),
  );
  select.value = chosen;
}

async function loadFacets() {
  const { facets, totals } = await fetchJson("/api/photos/facets");
  fillOptions("camera", facets.cameras);
  fillOptions("year", facets.years);
  fillOptions("album", facets.albums);
  fillOptions("type", facets.types);
  showTotals(totals);
}

// The two long jobs -- pulling from iCloud and deleting from it -- are the same
// shape: minutes of work on the server, watched here rather than waited on.
// Their state lives on the server, so a reloaded page picks up a run already
// going, and both end by reloading a library they have changed.
function job(url, button) {
  // Only a run this page watched end is worth reloading for: the last run's
  // result is still sitting there on every fresh load, and re-querying for it
  // would be a second full page of thumbnails for nothing.
  let watched = false;

  // The phase goes in a toast while it runs, and stays there until the job
  // says something else: a sync is minutes long and this is the only account
  // of where it has got to. The button it was started from spins meanwhile.
  function show(state) {
    const running = state.status === "running";
    button.dataset.busy = String(running);
    button.disabled = running;
    if (running) showToast(state.phase ? `${state.phase}…` : "Working…", { sticky: true });
  }

  async function poll() {
    let state;
    try {
      state = await fetchJson(url);
    } catch (error) {
      button.dataset.busy = "false";
      button.disabled = false;
      showError(error.message);
      return;
    }
    show(state);
    if (state.status === "running") {
      watched = true;
      setTimeout(poll, REFRESH_POLL_MS);
      return;
    }
    // A failure is the page's to report whether or not this page watched the
    // run: it is why the library did not change, and it is still true on a
    // reload. What a finished run managed is news, so it is only said to the
    // page that was watching, and only for as long as a toast lasts.
    if (state.status === "error") {
      hideToast();
      showError(state.message);
    }
    if (watched) {
      watched = false;
      if (state.status === "done") {
        showToast(state.message);
        await loadFacets();
        await loadPage();
      }
    }
  }

  async function start() {
    button.disabled = true;
    try {
      show(await fetchJson(url, { method: "POST" }));
      clearError();
      watched = true;
      setTimeout(poll, REFRESH_POLL_MS);
    } catch (error) {
      button.disabled = false;
      showError(error.message);
    }
  }

  return { start, poll };
}

const refreshJob = job("/api/photos/refresh", refreshButton);
const pruneJob = job("/api/photos/prune", pruneButton);

async function start() {
  applyDensity();
  startLoading(false);
  try {
    await loadFacets();
  } catch (error) {
    stopLoading();
    resultCount.textContent = "";
    showFatalError(`${error.message} Is the backup drive mounted?`);
    return;
  }
  await loadPage();
  refreshJob.poll();
  pruneJob.poll();
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

// Density is a view setting, not a filter: the select carries no `name`, so it
// never reaches the query string and never re-queries the library.
function applyDensity() {
  grid.style.gridTemplateColumns = `repeat(${perRow.value}, minmax(0, 1fr))`;
}

perRow.addEventListener("change", applyDensity);

refreshButton.addEventListener("click", refreshJob.start);
pruneButton.addEventListener("click", askToPrune);
document.getElementById("prune-cancel").addEventListener("click", () => pruneConfirm.close());
document.getElementById("prune-go").addEventListener("click", () => {
  pruneConfirm.close();
  pruneJob.start();
});
loadMore.addEventListener("click", () => loadPage({ append: true }));
document.getElementById("detail-close").addEventListener("click", () => detail.close());
for (const button of markButtons) {
  button.addEventListener("click", (event) => setMark(event.currentTarget));
}

start();
