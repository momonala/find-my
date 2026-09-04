// Durations are read back out of the stylesheet rather than repeated here, so
// retuning a token in tokens.css can't leave JS waiting the old amount of time
// and cutting an animation off part-way.

export const REDUCED_MOTION_QUERY = window.matchMedia("(prefers-reduced-motion: reduce)");

export function motionDurationMs(token) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  if (raw.endsWith("ms")) return parseFloat(raw);
  if (raw.endsWith("s")) return parseFloat(raw) * 1000;
  return 0;
}

// Without the height collapse the row would fade in place and everything under
// it would snap upwards the moment the re-render dropped it, which is the jank
// this exists to avoid. Resolves once the row is done animating; the page's own
// sheet styles `.is-removing`.
export function collapseRow(row) {
  if (REDUCED_MOTION_QUERY.matches) return Promise.resolve();
  row.style.height = `${row.offsetHeight}px`;
  void row.offsetHeight; // commit the measured height, so collapsing to 0 has something to tween from
  row.classList.add("is-removing");
  row.style.height = "0px";
  return new Promise((resolve) => setTimeout(resolve, motionDurationMs("--duration-quick")));
}
