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

async function openDetail(assetId) {
  try {
    const asset = await fetchJson(`/api/photos/assets/${encodeURIComponent(assetId)}`);
    const image = document.getElementById("detail-image");
    image.src = canRender(asset) ? `/api/photos/assets/${encodeURIComponent(asset.id)}/image?size=detail` : "";
    image.hidden = !canRender(asset);
    image.alt = asset.filename;
    document.getElementById("detail-title").textContent = asset.filename;

    const facts = [
      ["Taken", formatDate(asset.asset_date)],
      ["Camera", [asset.camera_make, asset.camera_model].filter(Boolean).join(" ") || null],
      ["Lens", asset.lens_model],
      ["Exposure", asset.f_number ? `ƒ/${asset.f_number} · ISO ${asset.iso ?? "?"}` : null],
      ["Length", formatDuration(asset.duration_seconds)],
      ["Size", formatBytes(asset.original_bytes)],
      ["Dimensions", asset.width ? `${asset.width} × ${asset.height}` : null],
      ["Album", asset.source_album],
      ["In iCloud", asset.in_icloud ? "Yes" : "No — this drive is the only copy"],
      ["On the drive", asset.relative_path ?? "Not backed up"],
      ["Location", asset.latitude ? `${asset.latitude.toFixed(4)}, ${asset.longitude.toFixed(4)}` : null],
    ];
    document.getElementById("detail-facts").replaceChildren(
      ...facts.flatMap(([label, value]) => {
        if (!value) return [];
        const term = document.createElement("dt");
        term.textContent = label;
        const description = document.createElement("dd");
        description.textContent = value;
        return [term, description];
      }),
    );

    const mark = document.getElementById("detail-mark");
    mark.dataset.id = asset.id;
    setMarkButton(mark, asset.mark === "delete");
    detail.showModal();
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

function showTotals(totals) {
  const parts = [
    `${totals.assets.toLocaleString()} assets`,
    `${formatBytes(totals.bytes)} of originals`,
    `${(totals.assets - totals.in_icloud).toLocaleString()} no longer in iCloud`,
  ];
  if (totals.marked) {
    parts.push(`${totals.marked.toLocaleString()} marked to delete (${formatBytes(totals.marked_bytes)})`);
  }
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
  await loadPage();
}

// Typing fires an input event per character, and each page costs a filtered
// count plus 120 image requests. Selects stay immediate: they change once.
let typingTimer = null;
form.addEventListener("input", (event) => {
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

document.getElementById("clear-filters").addEventListener("click", () => {
  form.reset();
  for (const button of form.querySelectorAll("[data-flag]")) button.setAttribute("aria-pressed", "false");
  loadPage();
});

loadMore.addEventListener("click", () => loadPage({ append: true }));
document.getElementById("detail-close").addEventListener("click", () => detail.close());
document.getElementById("detail-mark").addEventListener("click", (event) => toggleMark(event.currentTarget));

start();
