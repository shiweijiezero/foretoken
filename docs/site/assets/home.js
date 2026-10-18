// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Keyboard interaction for the homepage's runnable examples; Material owns copying.

/** Bind the homepage example tabs for one loaded page. */
function initializeHomepage() {
  const home = document.querySelector("[data-ft-home]");
  if (!home) return;

  const tabs = [...home.querySelectorAll("[data-ft-tab]")];
  const panels = [...home.querySelectorAll("[data-ft-panel]")];

  // Keep focus order, selection, and panel visibility together for both input modes.
  function selectTab(selected) {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    for (const panel of panels) {
      panel.hidden = panel.id !== selected.getAttribute("aria-controls");
    }
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", (event) => {
      let next;
      const index = tabs.indexOf(tab);
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft")
        next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (!next) return;
      event.preventDefault();
      selectTab(next);
      next.focus();
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initializeHomepage, {
    once: true,
  });
} else {
  initializeHomepage();
}
