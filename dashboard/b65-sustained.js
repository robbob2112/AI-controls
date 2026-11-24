const fmt = (n) => (Number.isFinite(n) ? n.toFixed(2) : "—");
const date = (s) =>
  s
    ? new Date(s).toLocaleString("en-US", { timeZone: "America/Denver", timeZoneName: "short" })
    : "—";
async function refresh() {
  try {
    const r = await fetch("/api/b65-sustained", { cache: "no-store" });
    if (!r.ok) throw Error(r.status);
    const d = await r.json();
    document.getElementById("status").textContent = d.state + (d.message ? " — " + d.message : "");
    const body = document.getElementById("summary");
    body.replaceChildren();
    for (const row of d.runs || []) {
      const tr = document.createElement("tr");
      for (const value of [
        row.configuration,
        row.state,
        date(row.started),
        date(row.finished),
        fmt(row.work_seconds / 60),
        fmt(row.prompt_tps),
        fmt(row.generation_tps),
      ]) {
        const td = document.createElement("td");
        td.textContent = value;
        td.style.padding = "12px";
        tr.append(td);
      }
      body.append(tr);
    }
    const complete = (d.runs || []).filter((x) => x.state === "complete");
    const singles = complete.filter((x) => x.configuration !== "B65 pair");
    const pair = complete.find((x) => x.configuration === "B65 pair");
    document.getElementById("comparison").textContent =
      pair && singles.length === 2
        ? "For this workload, paired generation was " +
          fmt((1 - pair.generation_tps / Math.max(...singles.map((x) => x.generation_tps))) * 100) +
          "% slower than the faster single card. This result does not establish performance for other models or parallel requests."
        : "";
    document.getElementById("results").textContent = JSON.stringify(d, null, 2);
    document.getElementById("updated").textContent =
      "Last checked " + date(new Date().toISOString());
  } catch (e) {
    document.getElementById("status").textContent = "Unable to read progress: " + e.message;
  }
}
refresh();
setInterval(refresh, 15000);

async function codingRefresh() {
  try {
    const r = await fetch("/api/coding-concurrent", { cache: "no-store" });
    if (!r.ok) throw Error(r.status);
    const d = await r.json();
    const body = document.getElementById("coding-results");
    body.replaceChildren();
    for (const run of d.comparisons || [d]) {
      const tr = document.createElement("tr");
      for (const value of [
        run.model,
        run.configuration,
        run.state,
        date(run.started),
        date(run.finished),
        run.completed_requests,
        run.incomplete_requests,
        fmt(run.aggregate_completed_tokens_per_second),
        fmt(run.mean_completed_latency_seconds),
      ]) {
        const td = document.createElement("td");
        td.textContent = value ?? "—";
        td.style.padding = "12px";
        tr.append(td);
      }
      body.append(tr);
    }
    document.getElementById("coding-details").textContent = JSON.stringify(d, null, 2);
  } catch (e) {
    document.getElementById("coding-details").textContent = e.message;
  }
}
codingRefresh();
setInterval(codingRefresh, 15000);
