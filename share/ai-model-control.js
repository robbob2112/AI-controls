"use strict";
const el = (id) => document.getElementById(id);
const selected = new Map(),
  removals = new Set();
let inventory = [],
  removing = false;
let installed = new Set(),
  matches = [],
  requestNumber = 0,
  pullBusy = true,
  submitting = false;
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
function message(text, error = false) {
  el("message").textContent = text;
  el("message").className = error ? "error" : "";
}
function row(text) {
  const li = document.createElement("li");
  li.textContent = text;
  return li;
}
function modelRow(model, review = false) {
  const li = row("");
  if (!model.family) {
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = selected.has(model.name);
    toggle.disabled = installed.has(model.name);
    toggle.setAttribute("aria-label", "Select " + model.name);
    toggle.onchange = () => {
      if (toggle.checked && selected.size >= 12) {
        toggle.checked = false;
        message("Select up to 12 models per batch.", true);
        return;
      }
      if (toggle.checked) selected.set(model.name, model);
      else selected.delete(model.name);
      renderChoices();
    };
    li.append(toggle, " ");
  }
  if (model.family) {
    const expand = document.createElement("button");
    expand.type = "button";
    expand.textContent = model.name + " · versions";
    expand.onclick = () => lookup(model.name, model.specs, model.purpose);
    li.append(expand);
    const summary = document.createElement("span");
    summary.className = "specs";
    summary.textContent = [model.purpose, model.specs].filter(Boolean).join(" · ");
    li.append(summary);
    return li;
  }
  const link = document.createElement("a");
  link.href = model.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = model.family ? "Model card" : review ? model.name : model.label || model.name;
  li.append(link);
  const specs = document.createElement("span");
  specs.className = "specs";
  specs.textContent = [model.purpose, model.specs, installed.has(model.name) ? "Installed" : ""]
    .filter(Boolean)
    .join(" · ");
  li.append(specs);
  if (model.description) {
    const description = document.createElement("span");
    description.className = "variant-description";
    description.textContent = model.description;
    li.append(description);
  }
  return li;
}
function renderChoices() {
  const rows = matches
    .filter((model) => !installed.has(model.name))
    .map((model) => modelRow(model));
  const showingCard = !el("modelCard").hidden;
  el("matches").replaceChildren(...(showingCard ? [] : rows));
  el("modelVersions").replaceChildren(...(showingCard ? rows : []));
  el("review").replaceChildren(...Array.from(selected.values(), (model) => modelRow(model, true)));
  el("install").disabled = pullBusy || submitting || !selected.size;
  el("install").textContent = selected.size
    ? "Download and register (" + selected.size + ")"
    : "Download and register";
}
async function lookup(family = "", familySpecs = "", purpose = "") {
  const number = ++requestNumber;
  el("backToSearch").hidden = !family;
  el("modelCard").hidden = !family;
  el("modelCardTitle").textContent = family;
  el("modelCardSpecs").textContent = [purpose, familySpecs].filter(Boolean).join(" · ");
  el("modelCardSpecs").hidden = !purpose && !familySpecs;
  const description = el("modelDescription");
  description.textContent = family ? "Loading publisher description…" : "";
  if (family) {
    api({ op: "model-card", source: el("source").value, family })
      .then((result) => {
        if (number !== requestNumber) return;
        const paragraphs = (result.description || "Publisher description unavailable.").split(
          /\n\n+/,
        );
        description.replaceChildren(
          ...paragraphs.map((text) => {
            const paragraph = document.createElement("p");
            paragraph.textContent = text;
            return paragraph;
          }),
        );
      })
      .catch(() => {
        if (number === requestNumber)
          description.textContent = "Publisher description unavailable.";
      });
  }
  matches = [];
  renderChoices();
  el("matchesStatus").textContent = "Loading " + (family || "catalog") + "…";
  try {
    const result = await api({
      op: "model-search",
      source: el("source").value,
      query: el("query").value.trim(),
      family,
      purpose,
    });
    if (number !== requestNumber) return;
    matches = result.matches;
    el("matchesStatus").textContent = matches.length
      ? family
        ? family + " · available versions"
        : "Open a model to see its versions."
      : family
        ? "No supported local versions found."
        : "No matching models found.";
    renderChoices();
  } catch (error) {
    if (number === requestNumber) el("matchesStatus").textContent = error.message;
  }
}
function updateDeleteButton() {
  const button = el("delete");
  button.disabled = pullBusy || removing || !removals.size;
  button.textContent = removals.size
    ? "Delete selected models (" + removals.size + ")"
    : "Delete selected models";
  el("deleteStatus").textContent = pullBusy
    ? "Removal is paused while a benchmark or download is active."
    : "";
}
function renderInstalled() {
  el("installed").replaceChildren(
    ...inventory
      .filter((model) => model.source !== "llama.cpp")
      .map((model) => {
        const li = row("");
        const label = document.createElement("label");
        label.className = "installed-option";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = removals.has(model.name);
        checkbox.disabled = removing;
        checkbox.onchange = () => {
          if (checkbox.checked && removals.size >= 12) {
            checkbox.checked = false;
            message("Select up to 12 models per deletion.", true);
            return;
          }
          if (checkbox.checked) removals.add(model.name);
          else removals.delete(model.name);
          updateDeleteButton();
        };
        const size = model.size ? " · " + (model.size / 2 ** 30).toFixed(1) + " GiB" : "";
        label.append(
          checkbox,
          " " + (model.label || model.name) + size + (model.purpose ? " · " + model.purpose : ""),
        );
        li.append(label);
        return li;
      }),
  );
  updateDeleteButton();
}
let reading = false;
async function refresh() {
  if (reading) return;
  reading = true;
  try {
    const state = await api();
    const signature = JSON.stringify(state.models.map((model) => model.name));
    if (signature !== JSON.stringify([...installed])) {
      installed = new Set(state.models.map((model) => model.name));
      for (const name of installed) selected.delete(name);
      inventory = state.models;
      for (const name of removals) if (!installed.has(name)) removals.delete(name);
      renderInstalled();
      renderChoices();
    }
    const pull = state.pull;
    pullBusy =
      !state.editable ||
      state.running.length > 0 ||
      !["idle", "failed", "success"].includes(pull.status);
    el("install").disabled = pullBusy || submitting || !selected.size;
    updateDeleteButton();
    const percent = pull.total
      ? Math.min(100, Math.round((100 * (pull.completed || 0)) / pull.total)) + "%"
      : "";
    el("progress").textContent =
      pull.status === "idle"
        ? state.running.length
          ? "Waiting for the active benchmark to finish."
          : ""
        : pull.status === "success"
          ? "Download complete. Installed models are listed below."
          : [
              pull.index && pull.count ? pull.index + "/" + pull.count : "",
              pull.model,
              pull.status,
              percent,
              pull.error,
            ]
              .filter(Boolean)
              .join(" · ");
    el("results").replaceChildren(
      ...(pull.results || [])
        .filter((item) => item.status === "failed")
        .map((item) => row(item.model + " · " + item.error)),
    );
  } catch (error) {
    message(error.message, true);
  } finally {
    reading = false;
  }
}
el("backToSearch").onclick = () => {
  lookup();
  el("query").focus();
};
el("search").onsubmit = (event) => {
  event.preventDefault();
  lookup();
};
el("source").onchange = () => {
  matches = [];
  renderChoices();
  lookup();
};
el("install").onclick = async () => {
  submitting = true;
  renderChoices();
  try {
    const imports = Object.fromEntries(
      [...selected]
        .filter(([, model]) => model.source_file)
        .map(([name, model]) => [name, model.source_file]),
    );
    const result = await api({ op: "pull", models: [...selected.keys()], imports });
    message(result.message);
    pullBusy = true;
    await refresh();
  } catch (error) {
    message(error.message, true);
  } finally {
    submitting = false;
    renderChoices();
  }
};
el("delete").onclick = async () => {
  const models = [...removals];
  if (!models.length || !window.confirm("Delete these models from HAL?\n\n" + models.join("\n")))
    return;
  removing = true;
  renderInstalled();
  try {
    const result = await api({ op: "model-delete", models, confirmed: true });
    for (const item of result.results) if (item.status === "deleted") removals.delete(item.model);
    el("deletions").replaceChildren(
      ...result.results.map((item) => row(item.model + " · " + (item.error || "Deleted"))),
    );
    await refresh();
  } catch (error) {
    message(error.message, true);
  } finally {
    removing = false;
    renderInstalled();
  }
};
lookup();
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 2500);
