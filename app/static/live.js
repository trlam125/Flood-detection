let liveSource = null;
let liveSourceLoading = false;
let liveOperation = false;
let liveSelectionVersion = 0;
let liveItems = [];
let liveStreamKey = null;
let liveRefreshCount = 0;

function sourcePayload() {
  const kind = $("sourceKind").value;
  return { kind, rtsp_env: kind === "rtsp" ? $("rtspEnv").value.trim() : null,
    device_index: kind === "webcam" ? Number($("webcamIndex").value) : null,
    analysis_interval_sec: Number($("sourceInterval").value), stale_after_sec: Number($("sourceStale").value),
    open_timeout_sec: Number($("sourceOpenTimeout").value), read_timeout_sec: Number($("sourceReadTimeout").value),
    auto_start: $("sourceAutoStart").checked, external_alerts_enabled: $("sourceExternal").checked };
}

function sourceHasDraft() {
  if (!liveSource?.configuration) return true;
  const current = sourcePayload();
  return Object.keys(current).some((key) => current[key] !== liveSource.configuration[key]);
}

function updateLiveControls() {
  const state = liveItems.find((item) => item.camera_id === selectedCamera?.id);
  const running = state && !["idle", "stopped", "error"].includes(state.lifecycle);
  const disabled = !selectedCamera || liveSourceLoading || liveOperation;
  $("rtspSourceField").classList.toggle("hidden", $("sourceKind").value !== "rtsp");
  $("webcamSourceField").classList.toggle("hidden", $("sourceKind").value !== "webcam");
  $("sourceForm").querySelectorAll("input, select, button").forEach((el) => { el.disabled = disabled || Boolean(running); });
  $("reloadSource").disabled = disabled;
  $("startLive").disabled = disabled || Boolean(running) || !liveSource?.configuration || Boolean(state?.requires_server_restart);
  $("stopLive").disabled = disabled || !running;
  $("restartLive").disabled = disabled || !running;
  $("liveRoiReference").disabled = disabled || !running || state?.connection !== "connected";
  $("sourceSaveState").textContent = liveSourceLoading ? "Đang đọc cấu hình…" : !selectedCamera ? "Lưu và chọn camera trước."
    : running ? "Camera đang chạy. Dừng giám sát trước khi đổi kết nối."
    : sourceHasDraft() ? "Kết nối chưa được lưu." : "Đã lưu kết nối. Bạn có thể bắt đầu giám sát.";
}

async function selectLiveCamera() {
  const version = ++liveSelectionVersion;
  const cameraId = selectedCamera?.id;
  liveSource = null;
  liveSourceLoading = Boolean(cameraId);
  renderLiveCamera();
  try {
    const saved = cameraId ? await api(`/api/cameras/${cameraId}/source`) : null;
    if (version !== liveSelectionVersion) return;
    liveSource = saved;
    const cfg = saved?.configuration || { kind: "rtsp", rtsp_env: "", device_index: 0,
      analysis_interval_sec: 1, stale_after_sec: 5, open_timeout_sec: 10, read_timeout_sec: 5 };
    $("sourceKind").value = cfg.kind;
    $("rtspEnv").value = cfg.rtsp_env || "";
    $("webcamIndex").value = cfg.device_index ?? 0;
    $("sourceInterval").value = cfg.analysis_interval_sec;
    $("sourceStale").value = cfg.stale_after_sec;
    $("sourceOpenTimeout").value = cfg.open_timeout_sec;
    $("sourceReadTimeout").value = cfg.read_timeout_sec;
    $("sourceAutoStart").checked = Boolean(cfg.auto_start);
    $("sourceExternal").checked = Boolean(cfg.external_alerts_enabled);
  } catch (error) { if (version === liveSelectionVersion) showError(error.message); }
  finally { if (version === liveSelectionVersion) { liveSourceLoading = false; updateLiveControls(); } }
}

function renderLiveCamera() {
  const state = liveItems.find((item) => item.camera_id === selectedCamera?.id);
  const running = state && !["idle", "stopped", "error"].includes(state.lifecycle);
  const fresh = Boolean(running && state.connection === "connected" && state.analysis_age_sec != null && state.frame_age_sec != null &&
    state.analysis_age_sec <= Math.min(state.fresh_frame_limit_sec, state.fresh_analysis_limit_sec) && state.frame_age_sec <= state.fresh_frame_limit_sec);
  renderObservation("camera", state || {}, { available: fresh,
    note: fresh ? "" : running ? "Đang chờ dữ liệu mới…" : "Chưa có dữ liệu camera." });
  $("liveCameraName").textContent = selectedCamera ? selectedCamera.name : "Chọn camera đã lưu hoặc thêm camera mới.";
  const connections = { connected: "Đã kết nối", connecting: "Đang kết nối…", reconnecting: "Đang kết nối lại…", stopped: "Đã dừng" };
  $("cameraConnection").textContent = state?.lifecycle === "error" ? "Có lỗi kết nối" : connections[state?.connection] || "Chưa kết nối";
  const age = (value) => value == null ? "--" : `${value.toFixed(1)}s`;
  $("liveCameraMetrics").textContent = state?.run_id
    ? `Tuổi ảnh: ${age(state.frame_age_sec)} · Tuổi kết quả: ${age(state.analysis_age_sec)} · ${state.analyzed_frames} mẫu · ${state.reconnect_attempts} lần kết nối lại`
    : "Chưa có dữ liệu camera.";
  $("liveCameraConfig").textContent = state?.run_id ? `Session profile v${state.camera_revision}; saved profile v${state.saved_camera_revision}. ` +
    (state.configuration_changed && running ? "Restart monitoring to apply the saved changes." : "Monitoring runs independently of this page.") +
    (state.recovery_count ? ` Recovered ${state.recovery_count} time(s); confirmation restarts from fresh observations.` : "") : "";
  $("liveCameraError").textContent = state?.last_error || "";
  const key = running ? `${state.camera_id}:${state.run_id}` : null;
  if (key !== liveStreamKey) {
    liveStreamKey = key;
    if (key) $("cameraLiveImage").src = `/api/cameras/${state.camera_id}/live/stream`;
    else $("cameraLiveImage").removeAttribute("src");
  }
  $("cameraLiveImage").hidden = !key;
  $("liveCameraEmpty").hidden = Boolean(key);
  updateLiveControls();
}

async function refreshLiveCameras() {
  try {
    const data = await api("/api/live");
    liveItems = data.items || [];
    const list = $("liveCameraList");
    list.hidden = liveItems.length < 2;
    list.replaceChildren();
    liveItems.forEach((state) => {
      const button = document.createElement("button");
      button.className = `action-button secondary ${state.camera_id === selectedCamera?.id ? "selected" : ""}`;
      button.textContent = `${state.camera_name} · ${severityLabel(state.severity)}`;
      button.addEventListener("click", async () => {
        try { const camera = await api(`/api/cameras/${state.camera_id}`); applyCamera(camera); }
        catch (error) { showError(error.message); }
      });
      list.appendChild(button);
    });
    renderLiveCamera();
    const resumeError = (data.resume_errors || []).find((item) => item.camera_id === selectedCamera?.id);
    if (resumeError && !liveItems.some((item) => item.camera_id === selectedCamera?.id && item.lifecycle === "running")) $("liveCameraError").textContent = resumeError.detail;
    if (++liveRefreshCount % 5 === 0 && liveItems.some((item) => item.lifecycle === "running")) { loadEvents(); loadAlerts(); loadHealth(false); }
  } catch (_) {
    renderObservation("camera", {}, { available: false, note: "Chưa đọc được trạng thái camera." });
    $("cameraConnection").textContent = "Chưa kết nối";
    $("liveCameraError").textContent = "Cannot reach monitoring status. Check the server connection.";
    $("cameraLiveImage").hidden = true;
    $("cameraLiveImage").removeAttribute("src");
    liveStreamKey = null;
  }
}

async function liveAction(action) {
  const cameraId = selectedCamera?.id;
  if (!cameraId || liveOperation) return;
  try {
    if (action !== "stop" && (cameraHasDraft() || sourceHasDraft())) throw new Error("Save or reload profile and source changes before starting monitoring.");
    liveOperation = true;
    updateLiveControls();
    await api(`/api/cameras/${cameraId}/live/${action}`, { method: "POST" });
    await refreshLiveCameras();
    loadEvents(); loadHistory();
  } catch (error) { showError(error.message); }
  finally { liveOperation = false; updateLiveControls(); }
}

function initializeLiveControls() {
  $("sourceForm").addEventListener("input", updateLiveControls);
  $("sourceKind").addEventListener("change", updateLiveControls);
  $("reloadSource").addEventListener("click", selectLiveCamera);
  $("sourceForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const cameraId = selectedCamera?.id;
    if (!cameraId || liveOperation) return;
    liveOperation = true;
    updateLiveControls();
    try {
      const result = await api(`/api/cameras/${cameraId}/source`, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...sourcePayload(), expected_revision: liveSource?.revision || 0 }) });
      if (cameraId === selectedCamera?.id) liveSource = result;
      await refreshLiveCameras();
    } catch (error) { showError(error.message); }
    finally { liveOperation = false; updateLiveControls(); }
  });
  ["start", "stop", "restart"].forEach((action) => $(action + "Live").addEventListener("click", () => liveAction(action)));
  $("liveRoiReference").addEventListener("click", async () => {
    const cameraId = selectedCamera?.id;
    try {
      const response = await fetch(`/api/cameras/${cameraId}/live/reference.jpg`, { cache: "no-store" });
      if (!response.ok) throw new Error("A fresh camera image is required to draw the ROI.");
      const blob = await response.blob();
      if (cameraId === selectedCamera?.id) await openRoiReference(new File([blob], "camera-reference.jpg", { type: "image/jpeg" }));
    } catch (error) { showError(error.message); }
  });
  selectLiveCamera();
  async function poll() { await refreshLiveCameras(); setTimeout(poll, 1000); }
  poll();
}
