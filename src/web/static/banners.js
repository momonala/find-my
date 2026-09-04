// The shell's three banners (see templates/base.html).
//
// Three elements, not one: `fatal` holds a standing problem (map init failed)
// that stays up across refreshes, `warning` a standing notice about how the
// server is configured, and `error` a per-refresh problem that the next
// successful refresh clears. One shared element can't hold all three -- a
// refresh clearing its own error would drop a standing notice with it.

const fatalEl = document.getElementById("fatal-banner");
const warningEl = document.getElementById("warning-banner");
const errorEl = document.getElementById("error-banner");

function show(target, message) {
  target.textContent = message;
  target.hidden = false;
}

export function showError(message) {
  show(errorEl, message);
}

export function clearError() {
  errorEl.hidden = true;
}

export function showFatalError(message) {
  show(fatalEl, message);
}

export function showWarning(message) {
  show(warningEl, message);
}
