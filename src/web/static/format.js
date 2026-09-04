// Formatting shared by every page. Pure functions, no DOM.

export function formatRelativeTime(isoString) {
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

// An absolute timestamp, with the year only when it isn't the current one.
export function formatSeenAt(isoString) {
  const date = new Date(isoString);
  const day = date.getDate();
  const month = date.toLocaleString(undefined, { month: "short" });
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const yearSuffix =
    date.getFullYear() === new Date().getFullYear() ? "" : ` '${String(date.getFullYear()).slice(-2)}`;
  return `${day} ${month}${yearSuffix} · ${time}`;
}

export function formatDistance(meters) {
  if (meters < 1000) return `${Math.round(meters)} meters`;
  return `${(meters / 1000).toFixed(1)} km`;
}

export function isFiniteCoordinate(value) {
  return typeof value === "number" && Number.isFinite(value);
}
