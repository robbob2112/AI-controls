"use strict";
(() => {
  const section = document.getElementById("psuTelemetry");
  if (!section) return;
  const status = document.getElementById("psuStatus");
  async function refresh() {
    try {
      const response = await fetch("/api/psu-current", { cache: "no-store" }),
        data = await response.json();
      if (!response.ok || !data.ok) throw Error(data.error || "PSU readings unavailable");
      for (const el of section.querySelectorAll("[data-psu]")) {
        const value = data.values[el.dataset.psu];
        el.textContent =
          value === null || value === undefined
            ? "Unavailable"
            : value.toLocaleString(undefined, {
                maximumFractionDigits: el.dataset.psu.includes("voltage") ? 2 : 1,
              }) +
              " " +
              el.dataset.unit;
      }
      status.textContent =
        (data.stale ? "STALE — " : "Live · ") +
        data.source +
        " · Updated " +
        new Date(data.time * 1000).toLocaleTimeString() +
        (data.error ? " · " + data.error : "");
      section.dataset.stale = String(data.stale);
    } catch (error) {
      status.textContent = error.message + " — retrying";
      section.dataset.stale = "true";
      for (const el of section.querySelectorAll("[data-psu]")) el.textContent = "Unavailable";
    }
  }
  refresh();
  setInterval(refresh, 5000);
})();
