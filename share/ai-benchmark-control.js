"use strict";
const el = (id) => document.getElementById(id);
const labels = {
  context: "Context tokens",
  threads: "CPU threads",
  batch: "Batch tokens",
  microbatch: "SYCL microbatch",
  flash: "Flash attention",
  kv: "KV cache",
  sycl_graph: "SYCL graphs",
  sycl_xmx: "SYCL XMX",
  radeon_compute: "Radeon Vulkan compute queues",
  temperature: "Temperature",
  seed: "Seed",
  cpu_threads: "CPU threads",
  cpu_quota: "CPU quota (cores)",
  cpu_affinity: "CPU affinity",
};
const helpText = {
  cpu_threads:
    "Sets how many PyTorch worker threads can do model math in parallel. Higher counts may improve speed and increase CPU heat.",
  cpu_quota:
    "Caps CPU time in units equivalent to one fully busy logical CPU each. A quota of four lets threads together use up to four CPUs’ worth of compute.",
  cpu_affinity:
    "Restricts the benchmark to specific logical CPUs. With none selected, Linux can schedule it on any available CPU. Use Ctrl-click or Cmd-click to select or clear individual CPUs.",
  context: "Sets the maximum number of tokens kept in context. Longer context uses more memory.",
  threads:
    "Sets the number of CPU worker threads used by the backend. It can affect prompt processing and CPU overhead.",
  batch:
    "Sets how many prompt tokens the backend can process in a batch. Larger batches can improve throughput and use more memory.",
  microbatch:
    "Sets how many tokens SYCL processes in each smaller chunk of a batch. It cannot exceed the batch size.",
  flash:
    "Selects whether to use optimized attention kernels. Support and performance depend on the model and backend.",
  kv: "Selects the format for the cached attention state. Lower precision can save memory and change generation quality.",
  sycl_graph:
    "Lets SYCL capture and reuse a sequence of GPU operations. This can reduce dispatch overhead on supported models.",
  sycl_xmx:
    "Controls use of Intel matrix accelerators in SYCL. Changing it can affect speed and kernel compatibility.",
  radeon_compute:
    "Selects the Vulkan compute queue configuration for Radeon. Safe uses HAL’s compatibility configuration, while Default lets the driver choose.",
  temperature: "Controls randomness in token sampling. Lower values favor more predictable output.",
  seed: "Sets the initial random seed used for sampling. Reusing it helps repeat a test under the same backend and settings.",
  duration:
    "Sets how long a timed workload measures performance. For fixed work, this is the maximum time allowed to complete the cases.",
  schedule:
    "Sets the earliest start in Mountain time. Other eligible tests can run while a scheduled test waits.",
  placement:
    "NOW makes the test eligible immediately at the front of the queue. START and END set its position and honor the earliest start.",
  cooldown:
    "Adds a separate queue entry that cools the GPU toward its starting temperature. The selected time limits how long that entry waits.",
  rearPercent:
    "Sets the rear fan duty during a timed Radeon Vulkan test. Leave it blank for automatic control.",
  gpu: "Compare queues a separate timed or fixed-work Vulkan run on each GPU. Split runs one timed Vulkan workload with model weights on both GPUs.",
  model:
    "The size shown estimates memory for the model files. Actual RAM or VRAM use can be higher because of context, caches and runtime buffers.",
  suite:
    "Timed workloads measure performance for the chosen duration. Fixed work runs a set of cases until they finish or the duration limit expires.",
};
function appendHelp(header, key) {
  const text = helpText[key];
  if (!text) return;
  const badge = document.createElement("button");
  badge.type = "button";
  badge.className = "helpBadge";
  badge.textContent = "?";
  badge.setAttribute("aria-label", "About " + (labels[key] || key));
  const tip = document.createElement("span");
  tip.className = "helpTip";
  tip.id = "help-" + key;
  tip.textContent = text;
  tip.setAttribute("role", "tooltip");
  badge.setAttribute("aria-describedby", tip.id);
  header.append(badge, tip);
}
for (const key of [
  "model",
  "gpu",
  "duration",
  "schedule",
  "placement",
  "cooldown",
  "rearPercent",
  "suite",
]) {
  const control = el(key),
    label = control.parentElement;
  const field = document.createElement("div"),
    header = document.createElement("div");
  field.className = "settingField";
  header.className = "settingLabel";
  if (label.id) {
    field.id = label.id;
    field.hidden = label.hidden;
    label.removeAttribute("id");
  }
  label.replaceWith(field);
  label.htmlFor = key;
  header.append(label);
  appendHelp(header, key);
  field.append(header, control);
}
const zone = "America/Denver";
const display = new Intl.DateTimeFormat(undefined, {
  timeZone: zone,
  dateStyle: "short",
  timeStyle: "short",
});
const parts = new Intl.DateTimeFormat("en-CA", {
  timeZone: zone,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
let state = null,
  reading = false,
  writing = false,
  lastModels = "";
function wallValue(date) {
  const p = Object.fromEntries(parts.formatToParts(date).map((x) => [x.type, x.value]));
  return p.year + "-" + p.month + "-" + p.day + "T" + p.hour + ":" + p.minute;
}
function scheduled(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw Error("Choose a Mountain time.");
  const wall = Date.parse(value + "Z");
  let guess = wall;
  for (let i = 0; i < 3; i++) guess += wall - Date.parse(wallValue(new Date(guess)) + "Z");
  if (wallValue(new Date(guess)) !== value) throw Error("This Mountain time does not exist.");
  return new Date(guess).toISOString();
}
function cell(row, value) {
  const td = row.insertCell();
  td.textContent = value;
  return td;
}
function action(label, click, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.onclick = click;
  button.disabled = disabled;
  return button;
}
function message(value, error = false) {
  el("announcement").className = error ? "warning" : "";
  el("announcement").textContent = value;
}
function when(seconds) {
  return Number.isFinite(seconds) ? display.format(new Date(seconds * 1000)) : "—";
}
function elapsed(start) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - start));
  return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
}
async function api(data) {
  const response = await fetch(
    "/llama/benchmark-control/api",
    data
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }
      : { cache: "no-store" },
  );
  const result = await response.json();
  if (!response.ok || result.ok === false) throw Error(result.error || "Request failed");
  return result;
}
async function mutate(data) {
  if (writing) return;
  writing = true;
  try {
    message((await api(data)).message);
    await refresh();
  } catch (error) {
    message(error.message, true);
  } finally {
    writing = false;
  }
}
let durationSuite;
function configureDuration(fixed) {
  const select = el("duration"),
    minutes = el("minutes");
  if (durationSuite !== fixed) {
    durationSuite = fixed;
    const choices = fixed
      ? [
          [600, "10 minutes"],
          [1800, "30 minutes"],
          [3600, "1 hour"],
          [7200, "2 hours"],
          [14400, "4 hours"],
          [28800, "8 hours"],
        ]
      : [
          [10, "10 seconds"],
          [60, "1 minute"],
          [300, "5 minutes"],
          [600, "10 minutes"],
          [900, "15 minutes"],
          [1800, "30 minutes"],
          [2700, "45 minutes"],
          [3600, "1 hour"],
        ];
    select.replaceChildren(
      ...choices.map(([seconds, label]) => new Option(label, seconds)),
      new Option("Custom…", "custom"),
    );
    select.value = fixed ? "14400" : "3600";
    minutes.min = fixed ? 10 : 10 / 60;
    minutes.max = fixed ? 480 : 60;
    minutes.value = fixed ? 240 : 60;
  }
  const custom = select.value === "custom";
  el("timeLabel").textContent = fixed ? "Duration limit" : "Duration";
  el("customDuration").hidden = !custom;
  minutes.disabled = !custom;
}
el("duration").onchange = () => configureDuration(durationSuite);
function readTuning() {
  const tuning = {};
  for (const item of el("advanced").querySelectorAll("select")) {
    if (item.multiple) {
      const cpus = Array.from(item.selectedOptions, (option) => Number(option.value));
      if (cpus.length) tuning[item.name] = cpus;
    } else if (item.value) tuning[item.name] = JSON.parse(item.value);
  }
  return tuning;
}
function setTuning(select, value) {
  if (select.multiple) {
    for (const option of select.options)
      option.selected = (value || []).includes(Number(option.value));
  } else select.value = value === undefined ? "" : JSON.stringify(value);
}
function configure() {
  const model = el("model").value;
  const littlebit = model === "littlebit-qwen3:8b-0.55bpw";
  const native = model.startsWith("llama.cpp/");
  const gpu = el("gpu"),
    backend = el("backend"),
    suite = el("suite");
  gpu.options[2].disabled = !littlebit;
  backend.options[2].disabled = !littlebit;
  gpu.disabled = littlebit || native;
  backend.disabled = littlebit || native;
  suite.disabled = littlebit || native;
  el("cooldown").disabled = littlebit;
  if (littlebit) {
    gpu.value = "cpu";
    backend.value = "littlebit";
    suite.value = "fixed";
    el("cooldown").value = "0";
  } else if (native) {
    gpu.value = "b65";
    backend.value = "sycl";
    suite.value = "hourly";
  } else if (gpu.value === "cpu") {
    gpu.value = "r9700";
    backend.value = "vulkan";
  }
  const intel = gpu.value === "b65";
  const compare = gpu.value === "compare";
  const split = gpu.value === "both";
  if (split) {
    suite.value = "hourly";
    suite.disabled = true;
  }
  el("parallel").disabled = littlebit || split || compare;
  const fixed = el("suite").value === "fixed";
  el("backend").options[1].disabled = !intel || fixed;
  if (el("backend").selectedOptions[0].disabled) el("backend").value = "vulkan";
  configureDuration(fixed);
  el("schedule").disabled = el("placement").value === "now";
  el("rearSetting").hidden = intel || fixed || littlebit || compare || split;
  el("rearPercent").disabled = el("rearSetting").hidden;
  const sycl = el("backend").value === "sycl";
  const old = readTuning();
  el("advanced").replaceChildren();
  const options = littlebit ? state?.cpu_options : state?.options;
  for (const [key, values] of Object.entries(options || {})) {
    if (
      (["microbatch", "sycl_graph", "sycl_xmx"].includes(key) && !sycl) ||
      (key === "radeon_compute" && (intel || compare))
    )
      continue;
    const field = document.createElement("div");
    field.className = "settingField";
    const header = document.createElement("div");
    header.className = "settingLabel";
    const label = document.createElement("label");
    const select = document.createElement("select");
    select.name = key;
    select.id = "tuning-" + key;
    label.htmlFor = select.id;
    label.textContent = labels[key] || key;
    if (key === "cpu_affinity") {
      select.multiple = true;
      select.size = 6;
    } else select.add(new Option(littlebit ? "Default (8)" : "Backend default", ""));
    for (const value of values) select.add(new Option(String(value), JSON.stringify(value)));
    setTuning(select, old[key]);
    header.append(label);
    appendHelp(header, key);
    field.append(header, select);
    el("advanced").append(field);
  }
}
function modelOptionLabel(model) {
  const label = model.label || model.name;
  if (!model.size) return label;
  const size = (model.size / 2 ** 30).toFixed(1);
  return label + " · ~" + size + " GiB " + (model.source === "LittleBit" ? "checkpoint" : "model");
}
function render(data) {
  const first = state === null;
  state = data;
  el("availability").textContent = data.editable
    ? "Queue controls ready."
    : "Viewing the queue while the running worker finishes. Queue changes unlock after its upgrade.";
  el("pullButton").disabled =
    !data.editable || !["idle", "failed", "success"].includes(data.pull.status);
  const runnable = data.models.filter((model) => model.benchmark);
  const models = runnable.map((model) => model.name);
  el("createButton").disabled = !data.editable || !runnable.length;
  const signature = JSON.stringify(data.models);
  const signatureChanged = signature !== lastModels;
  if (signatureChanged) {
    const previous = el("model").value;
    el("model").replaceChildren(
      ...runnable.map((model) => new Option(modelOptionLabel(model), model.name)),
    );
    if (models.includes(previous)) el("model").value = previous;
    el("installed").replaceChildren(
      ...data.models.map((model) => {
        const li = document.createElement("li");
        li.textContent =
          (model.label || model.name) +
          (model.size ? " · " + (model.size / 1e9).toFixed(1) + " GB" : "") +
          (model.benchmark ? "" : " · chat only");
        return li;
      }),
    );
    lastModels = signature;
  }
  if (first || signatureChanged) configure();
  el("loaded").textContent = data.loaded.length
    ? "Loaded in memory: " +
      data.loaded
        .map(
          (model) =>
            model.name +
            " · " +
            model.backend +
            (model.vram ? " · " + (model.vram / 1073741824).toFixed(1) + " GiB VRAM" : ""),
        )
        .join("; ")
    : "No models reported loaded in memory.";
  const pull = data.pull;
  el("pullStatus").textContent = pull.model
    ? pull.model +
      " · " +
      (pull.error || pull.status) +
      (pull.total && pull.completed
        ? " · " + Math.round((100 * pull.completed) / pull.total) + "% of current layer"
        : "")
    : "";
  const running = el("running");
  running.replaceChildren();
  if (!data.running.length) cell(running.insertRow(), "No tests running.");
  for (const item of data.running) {
    const row = running.insertRow();
    cell(
      row,
      [item.job.model || item.job.kind, item.job.gpu, item.job.parallel ? "Overlap enabled" : ""]
        .filter(Boolean)
        .join(" · "),
    );
    cell(row, item.status);
    cell(row, elapsed(item.started));
    cell(row, item.eta ? when(item.eta) + " · " + item.eta_mode : "Waiting for enough data");
    const stop = action(
      item.terminating ? "Stopping…" : "Terminate",
      () => {
        if (
          window.confirm(
            "Terminate " + (item.job.model || item.job.kind) + " and archive partial results?",
          )
        ) {
          mutate({ op: "terminate", id: item.id, confirmed: true });
        }
      },
      item.terminating || (!data.editable && !item.id.startsWith("legacy-")),
    );
    stop.className = "danger";
    cell(row, "").append(stop);
  }
  const pending = el("pending");
  pending.replaceChildren();
  if (!data.pending.length) cell(pending.insertRow(), "No pending tests.");
  for (const [index, item] of data.pending.entries()) {
    const row = pending.insertRow();
    cell(row, item.file);
    cell(
      row,
      [item.model || item.kind, item.profile, item.gpu, item.parallel ? "Overlap enabled" : ""]
        .filter(Boolean)
        .join(" · "),
    );
    cell(row, item.not_before ? when(Date.parse(item.not_before) / 1000) : "Eligible now");
    const controls = cell(row, "");
    controls.className = "actions";
    controls.append(
      action(
        "Up",
        () => mutate({ op: "reorder", file: item.file, direction: "up" }),
        !data.editable || index === 0,
      ),
      action(
        "Down",
        () => mutate({ op: "reorder", file: item.file, direction: "down" }),
        !data.editable || index === data.pending.length - 1,
      ),
    );
  }
}
async function refresh() {
  if (reading) return;
  reading = true;
  try {
    render(await api());
  } catch (error) {
    message(error.message, true);
  } finally {
    reading = false;
  }
}
el("schedule").value = wallValue(new Date());
for (const id of ["model", "gpu", "backend", "suite", "placement"]) el(id).onchange = configure;
el("create").onsubmit = (event) => {
  event.preventDefault();
  try {
    const tuning = readTuning();
    const placement = el("placement").value;
    const cooldown = Number(el("cooldown").value);
    mutate({
      op: "create",
      model: el("model").value,
      gpu: el("gpu").value,
      backend: el("backend").value,
      suite: el("suite").value,
      seconds:
        el("duration").value === "custom"
          ? Math.round(Number(el("minutes").value) * 60)
          : Number(el("duration").value),
      placement,
      not_before: placement === "now" ? null : scheduled(el("schedule").value),
      cooldown: cooldown > 0,
      cooldown_seconds: cooldown * 60,
      tuning,
      parallel: !el("parallel").disabled && el("parallel").checked,
      rear_percent:
        el("rearPercent").disabled || !el("rearPercent").value
          ? null
          : Number(el("rearPercent").value),
    });
  } catch (error) {
    message(error.message, true);
  }
};
el("pull").onsubmit = (event) => {
  event.preventDefault();
  mutate({ op: "pull", model: el("pullName").value.trim() });
};
function fillBenchmark(job, label) {
  const profiles = {
    "radeon-vulkan": ["r9700", "vulkan", "hourly"],
    "b65-vulkan-hours": ["b65", "vulkan", "hourly"],
    "b65-sycl-hours": ["b65", "sycl", "hourly"],
    "dual-vulkan-hours": ["both", "vulkan", "hourly"],
    "fixed-work-v1": [job.gpu, "vulkan", "fixed"],
    "littlebit-cpu": ["cpu", "littlebit", "fixed"],
  };
  const profile = profiles[job.profile];
  if (!profile) throw Error("This archived benchmark profile cannot be edited on this form.");
  const [gpu, backend, suite] = profile;
  const installed = Array.from(el("model").options).some((item) => item.value === job.model);
  el("model").value = installed ? job.model : "";
  el("gpu").value = gpu;
  el("suite").value = suite;
  el("backend").value = backend;
  el("placement").value = "now";
  el("cooldown").value = "0";
  configure();
  const seconds = suite === "fixed" ? job.timeout_seconds || 14400 : job.duration_seconds || 3600;
  el("duration").value = Array.from(el("duration").options).some(
    (option) => option.value === String(seconds),
  )
    ? String(seconds)
    : "custom";
  el("minutes").value = seconds / 60;
  configureDuration(suite === "fixed");
  el("parallel").checked = job.parallel === true;
  el("rearPercent").value = job.rear_percent ?? "";
  for (const item of el("advanced").querySelectorAll("select")) {
    setTuning(item, job.tuning?.[item.name]);
  }
  message(
    installed
      ? label + " settings loaded. Review and choose Queue test."
      : "The archived model " +
          job.model +
          " is unavailable. Choose an installed model before queueing.",
    !installed,
  );
  document.querySelector("#create").scrollIntoView({ block: "start" });
}
async function showReplay() {
  const id = new URLSearchParams(location.search).get("replay");
  if (!id) return;
  try {
    const result = await api({ op: "replay-preview", id });
    const job = result.job;
    if (job.kind === "benchmark") {
      fillBenchmark(job, result.label);
      return;
    }
    const details = [
      job.model || "Cooldown",
      job.profile,
      job.gpu,
      job.duration_seconds ? job.duration_seconds / 60 + " min" : null,
      Object.keys(job.tuning || {}).length ? JSON.stringify(job.tuning) : null,
    ].filter(Boolean);
    el("replayTitle").textContent = result.label + " archived test";
    el("replayDetails").textContent = details.join(" · ");
    el("replayPanel").hidden = false;
    el("replayConfirm").disabled = !result.editable;
    el("replayConfirm").onclick = () =>
      mutate({ op: "replay", id, placement: el("replayPlacement").value });
  } catch (error) {
    message(error.message, true);
  }
}
refresh().then(showReplay);
setInterval(refresh, 7000);
