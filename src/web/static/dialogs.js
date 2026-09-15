// Builders for the <dialog> elements pages use for editors and pickers. They
// share chrome and field markup, so they share these and the `.app-dialog`
// styles in shell.css that go with them.

export function createDialog(variantClass, accessibleName) {
  const dialog = document.createElement("dialog");
  dialog.className = `app-dialog ${variantClass}`;
  dialog.setAttribute("aria-label", accessibleName);
  document.body.append(dialog);
  return dialog;
}

export function createField(labelText, control) {
  const label = document.createElement("label");
  label.className = "dialog-field";
  const caption = document.createElement("span");
  caption.textContent = labelText;
  label.append(caption, control);
  return label;
}

export function createSelect(options) {
  const select = document.createElement("select");
  select.className = "dialog-select";
  for (const [value, text] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    select.append(option);
  }
  return select;
}

export function createButton(text, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

export function createSubmitButton(text) {
  const button = document.createElement("button");
  button.type = "submit";
  button.className = "btn-floating";
  button.textContent = text;
  return button;
}

export function createActions(...buttons) {
  const actions = document.createElement("div");
  actions.className = "dialog-actions";
  actions.append(...buttons);
  return actions;
}

export function sectionTitle(text) {
  const title = document.createElement("h2");
  title.className = "dialog-section-title";
  title.textContent = text;
  return title;
}
