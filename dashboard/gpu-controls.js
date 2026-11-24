"use strict";
(() => {
  const root = document.getElementById("gpuControls");
  if (!root) return;
  const fmt = (v, suffix) =>
    v === null || v === undefined ? "Unavailable" : v.toLocaleString() + suffix;
  let busy = false;
  async function refresh() {
    if (busy) return;
    try {
      const r = await fetch("/api/gpu-fans", { cache: "no-store" }),
        d = await r.json();
      if (!r.ok || !d.ok) throw Error(d.error || "Unavailable");
      document.getElementById("gpuKernel").textContent = "Kernel: " + d.kernel;
      for (const [i, g] of d.gpus.entries()) {
        let card = root.querySelector('[data-gpu="' + g.pci + '"]');
        if (!card) {
          card = document.createElement("article");
          card.dataset.gpu = g.pci;
          card.className = "gpu-card";
          const title = document.createElement("h3");
          title.textContent =
            g.driver === "amdgpu" && g.pci === "0000:09:00.0"
              ? "Radeon AI PRO R9700"
              : g.driver === "xe"
                ? "B65-" + (g.pci === "0000:04:00.0" ? "0" : i)
                : g.label || g.pci;
          card.append(title);
          for (const name of ["readings", "mode", "support", "message"]) {
            const p = document.createElement("p");
            p.dataset.field = name;
            card.append(p);
          }
          const label = document.createElement("label");
          label.textContent = "Manual fan duty ";
          const select = document.createElement("select");
          select.ariaLabel = "GPU " + i + " manual fan duty";
          for (let v = 10; v <= 100; v += 10) {
            const o = new Option(v + "%", v);
            select.add(o);
          }
          select.value = "50";
          label.append(select);
          card.append(label);
          for (const [action, text] of [
            ["manual", "Apply manual"],
            ["curve", "Temperature automatic"],
          ]) {
            const b = document.createElement("button");
            b.textContent = text;
            b.dataset.action = action;
            b.onclick = async () => {
              busy = true;
              const message = card.querySelector('[data-field="message"]');
              message.setAttribute("role", "status");
              try {
                const response = await fetch("/api/gpu-fans", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    action,
                    pci: g.pci,
                    ...(action === "manual" ? { percent: Number(select.value) } : {}),
                  }),
                });
                const result = await response.json();
                if (!response.ok || !result.ok) throw Error(result.error || "Not applied");
                message.textContent = result.message;
              } catch (e) {
                message.textContent = e.message;
              } finally {
                busy = false;
                refresh();
              }
            };
            card.append(b);
          }
          root.append(card);
        }
        card.querySelector('[data-field="readings"]').textContent =
          fmt(g.rpm, " RPM") +
          (g.rpm === 0 ? " (reported; watchdog active)" : "") +
          " · Duty " +
          fmt(g.pwm === null || g.pwm === undefined ? null : Math.round((g.pwm * 100) / 255), "%") +
          " · Package " +
          fmt(g.pkg_c, "°C") +
          " · VRAM " +
          fmt(g.vram_c, "°C");
        card.querySelector('[data-field="mode"]').textContent =
          g.manual_seconds_left > 0
            ? "Manual override · " + g.manual_seconds_left + " seconds remaining"
            : g.preferred_mode === "curve" && g.mode === 1
              ? "Temperature automatic · " + g.curve_percent + "% duty"
              : g.mode === 2
                ? "Firmware mode — minimum-floor governor restoring control"
                : g.mode === 0
                  ? "Full-speed failsafe"
                  : "Firmware controlled";
        card.querySelector('[data-field="support"]').textContent = g.supported
          ? "Minimum duty: 10% while Linux control is active. Automatic speed ramps with package/VRAM heat. Manual override lasts up to 5 minutes. " +
            (g.rpm_warning || "") +
            " " +
            (g.error || "")
          : "Fan-capable driver required for speed changes. Readings remain available.";
        for (const el of card.querySelectorAll("button,select")) el.disabled = !g.supported;
      }
    } catch (e) {
      document.getElementById("gpuKernel").textContent = e.message;
    }
  }
  refresh();
  setInterval(refresh, 5000);
  // OS-owned chassis settings use the verified per-header BMC interface.
  let caseState = null,
    caseBusy = false,
    caseMessage = "";
  const caseHeaders = ["CHA_FAN1", "CHA_FAN3", "CHA_FAN4", "CHA_FAN5"];
  function caseColumn() {
    const body = document.getElementById("fanResponse");
    if (!body) return;
    const head = body.closest("table").querySelector("thead tr");
    if (!head.querySelector("[data-base]")) {
      const th = document.createElement("th");
      th.dataset.base = "1";
      th.textContent = "Base speed";
      head.append(th);
    }
    for (const row of body.rows) {
      const header = row.dataset.header || row.cells[0].textContent.trim();
      const display = physicalFanNames[header] || header;
      let cell = row.querySelector("[data-base]");
      if (!cell) {
        cell = row.insertCell();
        cell.dataset.base = "1";
      }
      if (header.startsWith("CPU_")) {
        cell.textContent = "Automatic";
        if (caseState?.ok) row.cells[1].textContent = "Temperature automatic";
        continue;
      }
      if (header.includes("PUMP")) {
        cell.textContent = "100%";
        if (caseState?.ok) row.cells[1].textContent = "Full speed";
        continue;
      }
      if (!caseHeaders.includes(header)) {
        cell.textContent = "Unavailable";
        continue;
      }
      let select = cell.querySelector("select");
      if (!select) {
        select = document.createElement("select");
        select.ariaLabel = display + " minimum fan duty";
        select.add(new Option("Automatic baseline", ""));
        select.options[0].disabled = true;
        for (let p = 10; p <= 100; p += 10) select.add(new Option(p + "%", p));
        select.onchange = async () => {
          if (!select.value) return;
          caseBusy = true;
          caseMessage = "Applying " + display + "…";
          caseColumn();
          try {
            const response = await fetch("/api/case-fans", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ header, percent: Number(select.value) }),
            });
            const data = await response.json();
            if (!response.ok || !data.ok) throw Error(data.error || "Not applied");
            caseState = data;
            caseMessage =
              display +
              " minimum " +
              data.fans[header].base_percent +
              "% saved. " +
              (data.message || "Thermal ramp remains active.");
          } catch (error) {
            caseState = null;
            caseMessage = error.message;
          } finally {
            caseBusy = false;
            caseColumn();
          }
        };
        cell.append(select);
      }
      select.disabled = caseBusy || !caseState?.ok;
      if (!caseBusy && document.activeElement !== select)
        select.value = caseState?.fans?.[header]?.base_percent ?? "";
      select.title = "Minimum duty; existing temperature curve may command a higher speed";
      if (caseState?.ok)
        row.cells[1].textContent = caseState.fans[header].command || "OS setting · thermal ramp";
    }
    const status = document.getElementById("fanIdleStatus");
    if (status)
      status.textContent =
        caseMessage ||
        (caseState?.ok
          ? "OS controls available via BMC interface. Select a minimum duty; thermal ramp can run faster."
          : "Reading case-fan controller…");
  }
  async function refreshCase() {
    if (caseBusy) return;
    try {
      const response = await fetch("/api/case-fans", { cache: "no-store" }),
        data = await response.json();
      if (!response.ok || !data.ok) throw Error(data.error || "Case-fan controller unavailable");
      caseState = data;
    } catch (error) {
      caseState = null;
      caseMessage = error.message;
    }
    caseColumn();
    if (caseState?.ok && caseState.policy) {
      const summary = document.getElementById("airflowStatus");
      if (summary)
        summary.textContent =
          "Unified OS cooling · checks every " +
          caseState.sample_interval_seconds +
          " second · " +
          caseState.policy.reason;
    }
  }
  caseColumn();
  new MutationObserver(caseColumn).observe(document.getElementById("fanResponse"), {
    childList: true,
  });
  refreshCase();
  setInterval(refreshCase, 5000);
})();
