"use strict";
const hardware = document.getElementById("discoveredHardware");
async function refreshHardware() {
  try {
    const response = await fetch("/api/hardware-inventory", { cache: "no-store" });
    if (!response.ok) throw new Error("Hardware inventory unavailable");
    const data = await response.json();
    const status = document.getElementById("discoveredStatus");
    status.textContent = data.configured
      ? `${data.gpus.length} GPUs · ${data.sensors.length} sensor channels`
      : "First-time hardware setup has not been run.";
    const describe = (id, rows) => {
      document.getElementById(id).replaceChildren(
        ...rows.map((line) => {
          const li = document.createElement("li");
          li.textContent = line;
          return li;
        }),
      );
    };
    describe(
      "discoveredGpus",
      data.gpus.map((gpu) =>
        [
          gpu.model,
          gpu.id,
          gpu.driver,
          gpu.vram_bytes ? `${(gpu.vram_bytes / 2 ** 30).toFixed(1)} GiB VRAM` : "",
          gpu.present ? "" : "Unavailable",
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    describe(
      "discoveredSensors",
      data.sensors.map((sensor) =>
        [
          sensor.label,
          sensor.gpu,
          sensor.value == null
            ? "Unavailable"
            : `${sensor.value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${sensor.unit}`,
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    );
  } catch (error) {
    document.getElementById("discoveredStatus").textContent = error.message;
  }
}
refreshHardware();
hardware.addEventListener("toggle", () => {
  if (hardware.open) refreshHardware();
});
setInterval(() => {
  if (hardware.open && !document.hidden) refreshHardware();
}, 5000);
