let storageActionBusy = false;

function storageBytes(value) {
  return value == null ? "--" : `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

async function refreshOperations() {
  try {
    const [ops, history] = await Promise.all([api("/api/operations"), api("/api/operations/history?limit=60")]);
    $("operationsStatus").textContent = ops.status.toUpperCase();
    $("operationsStatus").className = `badge ${ops.status === "critical" ? "bad" : ops.status === "healthy" ? "ok" : "neutral"}`;
    $("operationsReadiness").textContent = `${ops.ready_for_live_monitoring ? "Live monitoring ready" : "Live monitoring not ready"} | Field validation still required`;
    const active = ops.cameras.filter((camera) => !["idle", "stopped", "error"].includes(camera.lifecycle));
    $("operationsMetrics").innerHTML = [["Server uptime", `${Math.floor(ops.uptime_sec / 60)} min`],
      ["Running cameras", active.length], ["Database", ops.database.ok ? "OK" : "Error"],
      ["Notifications pending", (ops.deliveries.pending || 0) + (ops.deliveries.retry || 0)]].map(([label, value]) =>
        `<div class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
    $("operationsIssues").textContent = ops.issues.length ? ops.issues.join(" | ") : "No current operational errors.";
    $("operationsCameras").replaceChildren();
    ops.cameras.forEach((camera) => {
      const row = document.createElement("p");
      row.textContent = `${camera.camera_name}: ${camera.connection} / ${camera.severity} | AI p95 ${camera.inference_ms?.p95 == null ? "--" : `${camera.inference_ms.p95.toFixed(0)} ms`} | Busy skips ${camera.skipped_busy || 0} | Stale results ${camera.stale_results || 0}`;
      $("operationsCameras").appendChild(row);
    });
    $("storageCapacity").innerHTML = ops.storage.disks.map((disk) => `<div><b>${escapeHtml(disk.area)}: ${escapeHtml(disk.status)}</b><span>${storageBytes(disk.free_bytes)} free${disk.free_percent == null ? "" : ` (${disk.free_percent.toFixed(1)}%)`}</span></div>`).join("");
    const points = history.items || [];
    const counts = points.map((sample) => sample.cameras.filter((camera) => camera.connection === "connected" && camera.severity !== "UNKNOWN").length);
    const highest = Math.max(1, ...counts);
    const start = points[0]?.created_at_unix || 0;
    const span = Math.max(1, (points.at(-1)?.created_at_unix || 0) - start);
    const coords = counts.map((count, index) => `${10 + 580 * (points[index].created_at_unix - start) / span},${65 - 50 * count / highest}`).join(" ");
    $("operationsTrend").innerHTML = `<path d="M10 10V65H590" fill="none" stroke="#30455a"/><polyline points="${coords}" fill="none" stroke="#6cc4ff" stroke-width="2"/>`;
    $("operationsTrendNote").textContent = points.length ? `Fresh confirmed cameras over ${Math.round(span)}s | ${points.length} persisted samples | Maximum ${Math.max(0, ...counts)}` : "Waiting for persisted operational samples.";
  } catch (_) {
    $("operationsStatus").textContent = "UNAVAILABLE";
    $("operationsStatus").className = "badge bad";
    $("operationsReadiness").textContent = "Operational status unavailable; readiness cannot be confirmed.";
  }
}

async function refreshStorage() {
  try {
    const data = await api("/api/storage");
    const policy = data.retention;
    $("retentionSummary").textContent = `Archive ${policy.enabled ? "enabled" : "disabled"}; permanent purge ${policy.purge_enabled ? "enabled" : "disabled"}. Expiry: uploads ${policy.days.uploads}d, results ${policy.days.outputs}d, evidence ${policy.days.evidence}d, cache ${policy.days.cache}d. Eligible ${policy.candidate_count} file(s), ${storageBytes(policy.candidate_bytes)}; archived ${policy.archived_count}, ${storageBytes(policy.archived_bytes)}.` + (policy.last_error ? ` ${policy.last_error}` : "");
    $("maintainStorage").disabled = !policy.enabled || storageActionBusy;
    $("retentionHistory").innerHTML = data.history.map((item) => `<div class="delivery-row"><div><b>${escapeHtml(item.area + "/" + item.relative_path)}</b><small>${escapeHtml(item.status)} | ${storageBytes(item.bytes)}${item.error ? ` | ${escapeHtml(item.error)}` : ""}</small></div>${item.status === "archived" ? `<button class="action-button secondary" data-restore-media="${escapeHtml(item.id)}" ${storageActionBusy ? "disabled" : ""}>Restore</button>` : ""}</div>`).join("");
  } catch (_) { $("retentionSummary").textContent = "Retention preview is unavailable."; }
}

function initializeOperationsControls() {
  $("refreshStorage").addEventListener("click", refreshStorage);
  async function action(url) {
    if (storageActionBusy) return;
    storageActionBusy = true;
    $("maintainStorage").disabled = true;
    try { await api(url, { method: "POST" }); }
    catch (error) { showError(error.message); }
    finally { storageActionBusy = false; await refreshStorage(); refreshOperations(); }
  }
  $("maintainStorage").addEventListener("click", () => action("/api/storage/maintain"));
  $("retentionHistory").addEventListener("click", (event) => {
    const button = event.target.closest("[data-restore-media]");
    if (button) action(`/api/storage/${encodeURIComponent(button.dataset.restoreMedia)}/restore`);
  });
  async function poll() { await refreshOperations(); setTimeout(poll, 5000); }
  poll(); refreshStorage();
}
