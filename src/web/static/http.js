// The one place a JSON route is called from the browser, so every page reports
// a failed request the same way.

export async function fetchJson(url, options) {
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
  if (response.status === 204) return null; // a successful DELETE has no body to parse
  return response.json();
}
