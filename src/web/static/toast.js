// The shell's toast (see templates/base.html): one line about something that
// just happened, or is still happening, said without taking a permanent seat
// in the layout the way a status line in a header does.
//
// A sticky toast is for work still running -- a sync is minutes of downloading
// and its phase is the only account of where it has got to -- and stays until
// whatever put it there replaces it. Everything else takes itself away.

const TOAST_MS = 5000;

const toastEl = document.getElementById("toast");
let timer = null;

export function showToast(message, { sticky = false } = {}) {
  clearTimeout(timer);
  toastEl.textContent = message;
  toastEl.hidden = false;
  if (!sticky) timer = setTimeout(hideToast, TOAST_MS);
}

export function hideToast() {
  clearTimeout(timer);
  toastEl.hidden = true;
}
