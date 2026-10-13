"use strict";
// Presentation preferences only: no machine configuration or API writes.
(function (root) {
  const defaults = Object.freeze({appearance: "automatic", sort: "name-asc"});
  const storageKey = "haas-monitor.ui.v1";
  const appearances = new Set(["automatic", "light", "dark"]);
  const sorts = new Set(["name-asc", "name-desc", "added-asc", "added-desc"]);
  function normalize(value) {
    return {appearance: appearances.has(value?.appearance) ? value.appearance : defaults.appearance,
      sort: sorts.has(value?.sort) ? value.sort : defaults.sort};
  }
  function load(storage) {
    try { return normalize(JSON.parse(storage.getItem(storageKey))); }
    catch { return {...defaults}; }
  }
  function save(storage, value) {
    try { storage.setItem(storageKey, JSON.stringify(normalize(value))); return true; }
    catch { return false; }
  }
  function sortedMachines(machines, order) {
    const copy = [...machines];
    if (order === "added-desc") return copy.reverse();
    if (order === "added-asc") return copy;
    const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
    return copy.sort((a, b) => collator.compare(a.name, b.name) * (order === "name-desc" ? -1 : 1));
  }
  function homeAssistantAppearance(win) {
    // Ingress normally shares HA's origin. Cross-origin/sandboxed embeddings
    // may deny access; do not use tokens, websocket connections or guessed messages.
    try {
      let parent = win.parent;
      for (let depth = 0; parent && parent !== win && depth < 5; depth++) {
        const host = parent.document.querySelector("home-assistant");
        const dark = host?.hass?.themes?.darkMode;
        if (typeof dark === "boolean") return dark ? "dark" : "light";
        const scheme = parent.getComputedStyle(host || parent.document.documentElement).colorScheme;
        if (scheme === "dark" || scheme === "light") return scheme;
        if (parent.parent === parent) break;
        parent = parent.parent;
      }
    } catch { /* Browser security boundary: use the device preference below. */ }
    return null;
  }
  function resolvedAppearance(preference, win) {
    if (preference === "light" || preference === "dark") return {theme: preference, source: "override"};
    const ha = homeAssistantAppearance(win);
    if (ha) return {theme: ha, source: "home-assistant"};
    return {theme: win.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light", source: "device"};
  }
  function start(win, onSortChange) {
    let storage;
    try { storage = win.localStorage; } catch { storage = null; }
    let preferences = load(storage);
    const doc = win.document, appearance = doc.getElementById("appearance"), sort = doc.getElementById("machine-sort");
    const hint = doc.getElementById("appearance-hint"), warning = doc.getElementById("preference-warning");
    function apply() {
      const resolved = resolvedAppearance(preferences.appearance, win);
      doc.documentElement.dataset.theme = resolved.theme;
      if (hint) hint.textContent = resolved.source === "home-assistant" ? "Following Home Assistant appearance"
        : resolved.source === "device" ? "Following device appearance" : "";
      appearance.value = preferences.appearance; sort.value = preferences.sort;
    }
    function update(field, value) {
      preferences = normalize({...preferences, [field]: value});
      const persisted = save(storage, preferences);
      if (warning) warning.hidden = persisted;
      apply();
      if (field === "sort") onSortChange();
    }
    appearance.addEventListener("change", () => update("appearance", appearance.value));
    sort.addEventListener("change", () => update("sort", sort.value));
    win.addEventListener("storage", event => {
      if (event.key === storageKey || event.key === null) { preferences = load(storage); apply(); onSortChange(); }
    });
    win.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", apply);
    // HA replaces hass objects without a DOM event contract for Ingress children.
    // Reading the parent's selected appearance periodically also catches those changes.
    win.setInterval(apply, 2000);
    apply();
    return {get sort() { return preferences.sort; }, apply};
  }
  const api = {defaults, normalize, load, save, sortedMachines, resolvedAppearance, start};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MonitorUI = api;
})(typeof window !== "undefined" ? window : globalThis);
