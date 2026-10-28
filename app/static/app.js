const $ = (id) => document.getElementById(id);
let activeJob = null;
let thresholds = { low: 0.10, moderate: 0.20, critical: 0.45 };
let cameraProfiles = [];
let selectedCamera = null;
let draftRoi = null;
let draftReference = null;
let savingCamera = false;
let roiReference = null;
let editorPoints = [];
let referenceUrl = null;
let referenceVideo = null;
let currentChart = null;
let eventDefaults = { confirm_sec: 3, clear_sec: 10, reminder_sec: 300, hysteresis_ratio: .03, max_gap_sec: 5, ema_time_constant_sec: 1, alert_min_severity: "MODERATE" };
let lastEventRefresh = 0;
let playbackResult = null;

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg}`).join("; ")
      : data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}

function pct(value, digits = 1) {
  if (value == null) return "--";
  return `${(Number(value || 0) * 100).toFixed(digits)}%`;
}

function severityClass(value) {
  return String(value || "UNKNOWN").toLowerCase();
}

function severityLabel(value) {
  return ({ SAFE: "An toàn", LOW: "Mức thấp", MODERATE: "Ngập vừa", CRITICAL: "Nguy hiểm", UNKNOWN: "Chưa xác định" })[value] || "Chưa xác định";
}

function renderObservation(kind, state = {}, { available = true, note = "" } = {}) {
  const severity = available ? state.severity || "UNKNOWN" : "UNKNOWN";
  const badge = $(kind === "video" ? "liveSeverity" : "cameraLiveSeverity");
  badge.textContent = severityLabel(severity);
  badge.dataset.severity = severity;
  badge.className = `severity-badge ${severityClass(severity)}`;
  $(kind + "WaterLabel").textContent = `Diện tích nước · ${state.ratio_scope === "roi" ? "vùng chọn" : "toàn ảnh"}`;
  $(kind + "Water").textContent = pct(available ? state.water_ratio : null);
  $(kind + "Confidence").textContent = pct(available ? state.confidence : null, 0);
  const details = [];
  if (available && state.observed_severity && state.observed_severity !== "UNKNOWN") details.push(`Quan sát: ${severityLabel(state.observed_severity)}`);
  if (available && state.pending_severity) {
    details.push(`Đang xác nhận ${severityLabel(state.pending_severity)}: ${Number(state.pending_elapsed_sec || 0).toFixed(1)}/${Number(state.pending_required_sec || 0).toFixed(1)} giây`);
  }
  if (available && Number.isFinite(state.time_sec)) details.push(`Khung ${state.time_sec.toFixed(1)} giây`);
  if (note) details.push(note);
  $(kind === "video" ? "liveMetric" : "cameraObservationNote").textContent = details.join(" · ") || "Đang chờ dữ liệu…";
}

function resetVideoObservations(note) {
  playbackResult = null;
  renderObservation("video", {}, { available: false, note });
}

function updatePlaybackObservation() {
  if (smoothPlayback) { renderSmoothObservation(); return; }
  if (!playbackResult || !$("videoPlayer").classList.contains("visible")) return;
  const at = $("videoPlayer").currentTime;
  const points = playbackResult.timeline || [];
  let left = 0, right = points.length;
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    if (points[mid].time_sec <= at + 1e-6) left = mid + 1;
    else right = mid;
  }
  let sample = points[left - 1];
  const final = playbackResult.final_observation;
  if (final && final.time_sec <= at + 1e-6 && (!sample || final.time_sec >= sample.time_sec)) sample = final;
  renderObservation("video", { ...sample, ratio_scope: playbackResult.ratio_scope },
    { available: Boolean(sample), note: sample ? "" : "Chưa có dữ liệu tại thời điểm này." });
  $("liveSeverity").title = "Confirmed level at the displayed analysis sample";
}

["timeupdate", "seeking", "seeked", "loadedmetadata", "ended"].forEach((event) => {
  $("videoPlayer").addEventListener(event, updatePlaybackObservation);
});

function updateLegend() {
  const low = Number($("lowRatio").value);
  const mod = Number($("moderateRatio").value);
  const crit = Number($("criticalRatio").value);
  $("safeText").textContent = `below ${low}%`;
  $("lowText").textContent = `${low}-${mod}%`;
  $("moderateText").textContent = `${mod}-${crit}%`;
  $("criticalText").textContent = `${crit}%+`;
}
["lowRatio", "moderateRatio", "criticalRatio"].forEach((id) => $(id).addEventListener("input", updateLegend));

async function loadHealth(updateDefaults = true) {
  try {
    const h = await api("/api/health");
    thresholds = h.thresholds || thresholds;
    eventDefaults = h.event_policy || eventDefaults;
    if (updateDefaults && !selectedCamera && !cameraHasDraft()) {
      setThresholdInputs(thresholds, 2);
      setEventPolicyInputs(eventDefaults);
    }
    updateLegend();

    const model = $("modelBadge");
    model.textContent = h.model_exists ? "AI sẵn sàng" : "Chưa có model AI";
    model.className = `badge ${h.model_exists ? "ok" : "bad"}`;
    const acceleration = h.acceleration || {};
    const cudaMissing = acceleration.requested_device === "cuda" && !acceleration.cuda_available;
    $("deviceBadge").className = `badge ${cudaMissing ? "bad" : "neutral"}`;
    $("deviceBadge").textContent = cudaMissing ? "CUDA chưa sẵn sàng" : h.device?.startsWith("cuda")
      ? `GPU: ${h.model?.device_name || acceleration.gpu_name || "NVIDIA"}`
      : h.device === "cpu" || acceleration.requested_device === "cpu" ? "Thiết bị: CPU"
      : acceleration.cuda_available ? `GPU sẵn sàng: ${acceleration.gpu_name}` : "Thiết bị: CPU";

    const channelBox = $("alertChannels");
    channelBox.innerHTML = "";
    (h.alert_channels || ["dashboard"]).forEach((name) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = name.toUpperCase();
      channelBox.appendChild(chip);
    });
    $("replayModeNote").textContent = h.replay_external_alerts_enabled
      ? "Replay notifications also use the configured external channels."
      : "Replay notifications stay on the dashboard. External replay delivery is disabled in server settings.";
  } catch (error) {
    $("modelBadge").textContent = "Backend unavailable";
    $("modelBadge").className = "badge bad";
  }
}

function readAnalysisOptions() {
  if (["lowRatio", "moderateRatio", "criticalRatio"].some((id) => !$(id).value.trim())) {
    throw new Error("Fill in all three severity thresholds.");
  }
  const low = Number($("lowRatio").value) / 100;
  const moderate = Number($("moderateRatio").value) / 100;
  const critical = Number($("criticalRatio").value) / 100;
  if (!(0 <= low && low < moderate && moderate < critical && critical <= 1)) {
    throw new Error("Thresholds must satisfy SAFE < LOW < MODERATE < CRITICAL and stay within 0-100%.");
  }
  const sampleEveryN = Number($("sampleN").value);
  if (!Number.isInteger(sampleEveryN) || sampleEveryN < 1 || sampleEveryN > 30) {
    throw new Error("Sample every N frames must be an integer between 1 and 30.");
  }
  return {
    low,
    moderate,
    critical,
    sampleEveryN,
    eventPolicy: readEventPolicy(),
  };
}

async function uploadFile(file, frozen = null) {
  if (!file) return;
  if (activeJob) {
    showError("An analysis is already running. Please wait for it to finish before uploading another video.");
    return;
  }

  $("analysisError").classList.add("hidden");
  let options;
  try {
    options = frozen ? frozen.options : readAnalysisOptions();
    if (!frozen && (savingCamera || cameraHasDraft())) throw new Error("Save or reload the camera profile before uploading.");
  } catch (error) {
    showError(error.message);
    return;
  }
  activeJob = "uploading";
  $("downloadResult").classList.add("hidden");
  $("previewImage").classList.remove("visible");
  $("originalProgress").classList.add("hidden");
  $("cancelAnalysis").classList.add("hidden");

  $("uploadInfo").classList.remove("hidden");
  $("uploadInfo").textContent = `Uploading ${file.name}...`;
  $("analysisProgress").classList.remove("hidden");
  $("progressBar").style.width = "0%";
  $("progressPercent").textContent = "0%";
  $("progressText").textContent = "Uploading video";
  $("resultSection").classList.add("hidden");
  resetVideoObservations("Waiting for analysis.");

  const form = new FormData();
  form.append("file", file);
  const camera = frozen ? frozen.camera : selectedCamera;
  if (camera) {
    form.append("camera_id", camera.id);
    form.append("camera_revision", String(camera.revision));
  } else {
    form.append("sample_every_n_frames", String(options.sampleEveryN));
    form.append("low_ratio", String(options.low));
    form.append("moderate_ratio", String(options.moderate));
    form.append("critical_ratio", String(options.critical));
    form.append("event_policy_json", JSON.stringify(options.eventPolicy));
  }

  try {
    const upload = await api("/api/upload", { method: "POST", body: form });
    $("uploadInfo").innerHTML = `<b>${escapeHtml(upload.filename || file.name)}</b><span>${upload.width}x${upload.height} | ${upload.fps.toFixed(1)} FPS | ${upload.duration_sec.toFixed(1)} s</span>`;
    $("videoPlayer").src = upload.video_url;
    $("videoPlayer").classList.add("visible");
    $("emptyState").classList.add("hidden");
    $("mediaSubtitle").textContent = "Processing video...";

    if (!upload.job_id) throw new Error("Upload succeeded but the analysis job was not created.");
    activeJob = upload.job_id;
    $("progressText").textContent = "Processing video";
    $("liveMetric").textContent = "Waiting for first inference frame...";
    pollJob(upload.job_id);
  } catch (error) {
    activeJob = null;
    finishProgress();
    resetVideoObservations("Analysis unavailable.");
    $("uploadInfo").textContent = error.message;
    showError(error.message);
  }
}

$("videoFile").addEventListener("change", (event) => {
  const file = event.target.files[0];
  selectVideoFile(file);
  event.target.value = "";
});
const drop = $("dropZone");
["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
  event.preventDefault();
  drop.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
  event.preventDefault();
  drop.classList.remove("dragging");
}));
drop.addEventListener("drop", (event) => selectVideoFile(event.dataTransfer.files[0]));

async function pollJob(jobId) {
  if (activeJob !== jobId) return;
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (activeJob !== jobId) return;
    const progress = Number(job.progress || 0);
    $("progressBar").style.width = `${progress}%`;
    $("progressPercent").textContent = `${progress.toFixed(0)}%`;
    const labels = { previewing: `Đã đọc ${Number(job.analyzed_until_sec || 0).toFixed(1)} / ${Number(job.duration_sec || 0).toFixed(1)} giây`, waiting_upload: "Đã có kết quả. Đang lưu bản gốc ở nền…", rendering: "Đã có kết quả. Đang xuất video ở nền…" };
    $("progressText").textContent = labels[job.status] || (job.status === "running" ? `Processing ${job.processed_frames || 0} frames` : job.status);
    if (job.live && (!activePreview || !activePreview.reading || (job.analyzed_frames || 0) >= activePreview.frameCount)) {
      if (smoothPlayback) renderSmoothObservation();
      else renderObservation("video", job.live);
      if (Date.now() - lastEventRefresh > 2000) {
        lastEventRefresh = Date.now();
        loadEvents();
        loadAlerts();
      }
    }
    if (job.status === "done") {
      finishPreviewUi(false);
      activeJob = null;
      finishProgress();
      renderResult(job.result);
      await Promise.all([loadAlerts(), loadHistory(), loadEvents()]);
      return;
    }
    if (job.status === "error") {
      finishPreviewUi(true);
      activeJob = null;
      finishProgress();
      if (job.partial_result?.model?.backend) renderResult(job.partial_result);
      resetVideoObservations("Analysis interrupted.");
      showError(job.error || "Analysis failed");
      loadEvents();
      return;
    }
    setTimeout(() => pollJob(jobId), 850);
  } catch (error) {
    if (activePreview) {
      await stopPreview(activePreview, error.message);
      return;
    }
    activeJob = null;
    finishProgress();
    resetVideoObservations("Analysis unavailable.");
    showError(error.message);
  }
}

function finishProgress() {
  $("analysisProgress").classList.add("hidden");
}

function renderResult(result) {
  $("resultSection").classList.remove("hidden");
  if (result.output_url && !smoothPlayback) {
    playbackResult = result;
    $("previewImage").classList.remove("visible");
    $("videoPlayer").src = `${result.output_url}?v=${Date.now()}`;
    $("videoPlayer").classList.add("visible");
    $("videoPlayer").load();
  }
  $("downloadResult").classList.toggle("hidden", !result.output_url);
  if (result.output_url) $("downloadResult").href = result.output_url;
  const camera = result.configuration?.camera;
  const scope = result.ratio_scope === "roi" ? "monitored ROI" : "full frame";
  $("mediaSubtitle").textContent = `${result.pending_export ? "Đã phân tích xong · Đang lưu video ở nền" : "Đã phân tích xong"}${camera ? ` · ${camera.name}` : ""}`;
  if (smoothPlayback || result.output_url) updatePlaybackObservation();
  else renderObservation("video", { ...(result.final_observation || result.timeline?.at(-1)),
    ...result.final_state, ratio_scope: result.ratio_scope });
  $("highestSeverity").textContent = severityLabel(result.highest_severity);
  $("highestSeverity").className = severityClass(result.highest_severity);
  $("maxRatio").textContent = pct(result.max_water_ratio);
  $("meanRatio").textContent = pct(result.mean_water_ratio);
  $("alertCount").textContent = String(result.alert_count || 0);
  $("analysisMeta").textContent = `${result.analyzed_frames} inference frames | ${result.model.backend.toUpperCase()} | ${scope}`;
  const open = (result.events || []).filter((event) => event.status === "open_at_end").length;
  const final = result.final_state?.severity || "UNKNOWN";
  $("stateSummary").textContent = `${result.partial ? "Kết quả chưa đầy đủ. " : ""}Mức cuối: ${severityLabel(final)}. ${result.event_count || 0} sự kiện cảnh báo.${open ? " Cảnh báo vẫn còn khi video kết thúc; chưa xác nhận hết ngập." : ""}${result.highest_severity === "UNKNOWN" ? " Video chưa đủ dài để xác nhận mức ngập." : ""}`;
  drawTimeline(result.timeline || [], result.thresholds || thresholds);
}

function drawTimeline(points, limits) {
  currentChart = { points, limits };
  const canvas = $("timelineChart");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 800;
  const height = 220;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 46, right: 18, top: 16, bottom: 30 };
  const chartW = width - pad.left - pad.right;
  const chartH = height - pad.top - pad.bottom;
  ctx.font = "11px system-ui";
  ctx.strokeStyle = "#2a3949";
  ctx.fillStyle = "#8fa2b5";
  ctx.lineWidth = 1;
  [0, 0.25, 0.5, 0.75, 1].forEach((v) => {
    const y = pad.top + chartH * (1 - v);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.fillText(`${Math.round(v * 100)}%`, 8, y + 4);
  });

  const maxT = Math.max(1, ...points.map((p) => Number(p.time_sec || 0)));
  const xOf = (t) => pad.left + chartW * (Number(t || 0) / maxT);
  const yOf = (v) => pad.top + chartH * (1 - Math.max(0, Math.min(1, Number(v || 0))));

  [[limits.low, "LOW"], [limits.moderate, "MOD"], [limits.critical, "CRIT"]].forEach(([value, label]) => {
    const y = yOf(value);
    ctx.setLineDash([5, 5]);
    ctx.strokeStyle = "#56697d";
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#9fb0c1";
    ctx.fillText(label, width - pad.right - 30, y - 4);
  });

  if (points.length > 0) {
    ctx.strokeStyle = "#54aaf5";
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    points.forEach((p, index) => {
      const x = xOf(p.time_sec);
      const y = yOf(p.water_ratio);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.fillStyle = "#8fa2b5";
  ctx.fillText("0s", pad.left, height - 8);
  ctx.fillText(`${maxT.toFixed(1)}s`, width - pad.right - 34, height - 8);
}

async function loadAlerts() {
  try {
    const data = await api("/api/alerts?limit=12");
    const root = $("alertsList");
    const items = data.items || [];
    if (!items.length) { root.className = "list-empty"; root.textContent = "No alerts yet."; return; }
    root.className = "activity-list";
    root.innerHTML = items.map((item) => `
      <div class="activity-row">
        <span class="status-dot ${severityClass(item.severity)}"></span>
        <div title="${escapeHtml(item.message)}"><b>${escapeHtml(transitionLabel(item.event_type))} ${escapeHtml(item.severity)}${item.camera_name ? ` | ${escapeHtml(item.camera_name)} v${item.camera_revision}` : ""}</b><span>${pct(item.water_ratio)} water in ${item.ratio_scope === "roi" ? "ROI" : "frame"} | ${formatTime(item.created_at)}</span></div>
        <small>${(item.channels || []).map((name) => escapeHtml(name === "dashboard" ? name : `${name}: ${deliveryStatus(item.dispatch?.[name]?.status || (item.dispatch?.[name]?.ok ? "sent" : "unknown"))}`)).join(" | ")}${evidenceLink(item.snapshot_url)}</small>
      </div>`).join("");
  } catch (_) {}
}

async function loadHistory() {
  try {
    const data = await api("/api/history?limit=12");
    const root = $("historyList");
    const items = data.items || [];
    if (!items.length) { root.className = "list-empty"; root.textContent = "No completed analyses yet."; return; }
    root.className = "activity-list";
    root.innerHTML = items.map((item) => {
      const camera = item.configuration?.camera;
      const runningLive = item.configuration?.analysis_mode === "live_camera" && item.status === "running";
      return `
      <div class="activity-row">
        <span class="status-dot ${severityClass(item.highest_severity)}"></span>
        <div><b>${item.configuration?.analysis_mode === "live_camera" ? "Live camera | " : "Video | "}${escapeHtml(item.highest_severity || item.status)}${camera ? ` | ${escapeHtml(camera.name)} v${camera.revision}` : ""}</b><span>${runningLive ? "Observations in live camera view" : `${pct(item.max_water_ratio)} max`} | ${formatTime(item.created_at)}</span></div>
        <small>${runningLive ? "running" : `${item.analyzed_frames || 0} frames`}</small>
      </div>`;
    }).join("");
  } catch (_) {}
}

function eventStatusLabel(status) {
  return ({ active: "Active", unknown: "Observation unavailable", resolved: "Below alert threshold",
    open_at_end: "Open when video ended", interrupted: "Analysis interrupted" })[status] || status;
}

function deliveryStatus(status) {
  return ({ pending: "Queued", sending: "Sending", retry: "Retry scheduled", sent: "Accepted", failed: "Failed",
    blocked: "Blocked", cancelled: "Skipped", unknown: "Outcome unavailable" })[status] || status;
}

let deliveryActionInFlight = false;
async function loadDeliveries() {
  try {
    const data = await api("/api/deliveries?limit=12");
    const count = data.counts || {};
    $("deliverySummary").textContent = `${data.worker_running ? "Worker running" : "Worker stopped"} | Queued ${(count.pending || 0) + (count.retry || 0)} | Sending ${count.sending || 0} | Accepted ${count.sent || 0} | Needs attention ${(count.failed || 0) + (count.blocked || 0)}` + (data.worker_error ? ` | ${data.worker_error}` : "");
    const root = $("deliveriesList");
    root.className = data.items.length ? "delivery-list" : "list-empty";
    root.innerHTML = data.items.length ? data.items.map((job) => `<div class="delivery-row">
      <div title="${escapeHtml(job.message)}"><b>${escapeHtml(job.channel.toUpperCase())} | ${escapeHtml(deliveryStatus(job.status))} | ${escapeHtml(transitionLabel(job.event_type))} ${escapeHtml(job.severity)}</b>
      <small>${job.attempts} attempt(s) | Queued ${formatTime(job.created_at)}${job.status === "retry" ? ` | Retry at ${formatTime(job.next_attempt_at)}` : ""}</small>
      <small>${escapeHtml(job.last_error || "")}</small></div><div class="button-row">
      ${["failed", "blocked"].includes(job.status) ? `<button class="action-button secondary" data-delivery-action="retry" data-delivery-id="${job.id}" ${deliveryActionInFlight ? "disabled" : ""}>Retry</button>` : ""}
      ${["pending", "retry", "failed", "blocked"].includes(job.status) ? `<button class="action-button secondary" data-delivery-action="skip" data-delivery-id="${job.id}" ${deliveryActionInFlight ? "disabled" : ""}>Skip message</button>` : ""}
      </div></div>`).join("") : "No external notifications queued.";
  } catch (_) { $("deliverySummary").textContent = "Notification delivery status is unavailable."; }
}

function initializeDeliveryControls() {
  $("refreshDeliveries").addEventListener("click", loadDeliveries);
  $("deliveriesList").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delivery-action]");
    if (!button || deliveryActionInFlight) return;
    deliveryActionInFlight = true;
    $("deliveriesList").querySelectorAll("button").forEach((item) => { item.disabled = true; });
    try {
      await api(`/api/deliveries/${button.dataset.deliveryId}/${button.dataset.deliveryAction}`, { method: "POST" });
    } catch (error) { showError(error.message); }
    finally { deliveryActionInFlight = false; await loadDeliveries(); loadAlerts(); }
  });
  async function poll() { await loadDeliveries(); setTimeout(poll, 3000); }
  poll();
}

function transitionLabel(kind) {
  return ({ started: "Started", escalated: "Increased", deescalated: "Decreased", reminder: "Reminder",
    resolved: "Cleared", data_lost: "Observation lost", data_resumed: "Observation resumed",
    observation_ended: "Observation ended" })[kind] || "Alert";
}

async function loadEvents() {
  const root = $("eventsList");
  try {
    const items = (await api("/api/events?limit=8")).items || [];
    if (!items.length) { root.className = "list-empty"; root.textContent = "No confirmed alert events yet."; return; }
    root.className = "activity-list";
    root.innerHTML = items.map((event) => `<button type="button" class="event-row" data-event-id="${escapeHtml(event.id)}">
      <b>${escapeHtml(eventStatusLabel(event.status))} | <span class="${severityClass(event.severity)}">${escapeHtml(event.severity)}</span></b>
      <small>${escapeHtml(event.camera_name || "Uploaded video")}${event.camera_revision ? ` v${event.camera_revision}` : ""} | Peak ${escapeHtml(event.peak_severity)} | Started at ${escapeHtml(eventTime(event, event.started_sec))}</small>
    </button>`).join("");
  } catch (_) { root.textContent = "Unable to load alert events."; }
}

async function showEvent(eventId) {
  try {
    const event = await api(`/api/events/${encodeURIComponent(eventId)}`);
    $("eventTimeNote").textContent = event.time_source === "monotonic"
      ? "Live camera: local observation time and elapsed session seconds. Connection loss does not confirm recovery."
      : "Times refer to the uploaded video. An event left open at video end does not confirm recovery.";
    $("eventDetail").innerHTML = `<p>${escapeHtml(event.camera_name || "Uploaded video")} | ${escapeHtml(eventStatusLabel(event.status))} | Peak ${escapeHtml(event.peak_severity)}</p>` +
      event.transitions.map((transition) => `<div class="transition-row"><b>${escapeHtml(eventTime(event, transition.time_sec))} — ${escapeHtml(transitionLabel(transition.type))}: ${escapeHtml(transition.severity)}</b><small>${["data_lost", "observation_ended"].includes(transition.type) ? "Last observed water coverage" : "Water coverage"} ${pct(transition.water_ratio)}${transition.reason ? ` | ${escapeHtml(transition.reason)}` : ""}${evidenceLink(transition.snapshot_url)}</small></div>`).join("");
    $("eventDialog").showModal();
  } catch (error) { showError(error.message); }
}

function eventTime(event, seconds) {
  const elapsed = `${Number(seconds).toFixed(1)}s`;
  return event.time_source === "monotonic" && event.started_at_unix
    ? `${formatTime(event.started_at_unix + Number(seconds))} (+${elapsed})` : elapsed;
}

function evidenceLink(url) {
  return /^\/media\/outputs\/live_evidence\/[a-zA-Z0-9_]+\.jpg$/.test(url || "")
    ? ` | <a href="${escapeHtml(url)}" target="_blank" rel="noopener">Snapshot</a>` : "";
}

function readEventPolicy() {
  const ranges = { confirmSec: [0, 600], clearSec: [0, 3600], reminderSec: [0, 86400],
    hysteresisRatio: [0, 100], maxGapSec: [0.000001, 3600], emaTauSec: [0.000001, 60] };
  const values = {};
  for (const [id, [min, max]] of Object.entries(ranges)) {
    const input = $(id).value;
    const value = Number(input);
    if (!input.trim() || !Number.isFinite(value) || value < min || value > max) throw new Error(`Invalid event timing: ${id}.`);
    values[id] = value;
  }
  return { confirm_sec: values.confirmSec, clear_sec: values.clearSec, reminder_sec: values.reminderSec,
    hysteresis_ratio: values.hysteresisRatio / 100, max_gap_sec: values.maxGapSec,
    ema_time_constant_sec: values.emaTauSec, alert_min_severity: $("alertMinimum").value };
}

function setEventPolicyInputs(policy) {
  $("confirmSec").value = policy.confirm_sec;
  $("clearSec").value = policy.clear_sec;
  $("reminderSec").value = policy.reminder_sec;
  $("hysteresisRatio").value = policy.hysteresis_ratio * 100;
  $("maxGapSec").value = policy.max_gap_sec;
  $("emaTauSec").value = policy.ema_time_constant_sec;
  $("alertMinimum").value = policy.alert_min_severity;
}

function setThresholdInputs(limits, sampleN) {
  $("lowRatio").value = limits.low * 100;
  $("moderateRatio").value = limits.moderate * 100;
  $("criticalRatio").value = limits.critical * 100;
  $("sampleN").value = sampleN;
  updateLegend();
}

function cameraPayload() {
  const options = readAnalysisOptions();
  return {
    name: $("cameraName").value.trim(),
    location: $("cameraLocation").value.trim(),
    thresholds: { low: options.low, moderate: options.moderate, critical: options.critical },
    sample_every_n_frames: options.sampleEveryN,
    roi: draftRoi,
    reference_width: draftReference?.width ?? null,
    reference_height: draftReference?.height ?? null,
    event_policy: options.eventPolicy,
  };
}

function cameraHasDraft() {
  if (!selectedCamera) return Boolean($("cameraName").value.trim() || $("cameraLocation").value.trim() || draftRoi);
  try {
    const draft = cameraPayload();
    const saved = Object.fromEntries(Object.keys(draft).map((key) => [key, key === "event_policy" ? selectedCamera.event_policy || eventDefaults : selectedCamera[key]]));
    const canonical = (value) => JSON.stringify(value, (_, item) => typeof item === "number" ? Number(item.toPrecision(12)) : item);
    return canonical(draft) !== canonical(saved);
  } catch (_) { return true; }
}

function updateCameraStatus() {
  $("roiSummary").textContent = draftRoi
    ? `${draftRoi.length}-corner ROI | reference ${draftReference.width} x ${draftReference.height}. Only water inside this region is measured.`
    : "Water coverage is measured across the full frame.";
  $("cameraSaveState").textContent = savingCamera ? "Saving profile..."
    : cameraHasDraft() ? "Unsaved changes. Save the profile before uploading."
    : selectedCamera ? `Saved configuration v${selectedCamera.revision}.` : "No saved camera selected.";
}

function applyCamera(camera) {
  selectedCamera = camera || null;
  $("cameraSelect").value = camera?.id || "";
  $("cameraName").value = camera?.name || "";
  $("cameraLocation").value = camera?.location || "";
  draftRoi = camera?.roi ? camera.roi.map((point) => [...point]) : null;
  draftReference = draftRoi ? { width: camera.reference_width, height: camera.reference_height } : null;
  setThresholdInputs(camera?.thresholds || thresholds, camera?.sample_every_n_frames || 2);
  setEventPolicyInputs(camera?.event_policy || eventDefaults);
  updateCameraStatus();
  syncCameraPickers();
  selectLiveCamera();
}

async function loadCameras(selectId = null) {
  const data = await api("/api/cameras");
  cameraProfiles = data.items || [];
  const previous = $("cameraSelect").value;
  $("cameraSelect").replaceChildren(new Option("Whole frame (no saved camera)", ""));
  cameraProfiles.forEach((camera) => $("cameraSelect").add(new Option(`${camera.name} (v${camera.revision})`, camera.id)));
  $("cameraSelect").value = selectId ?? previous;
  if (selectId !== null) applyCamera(cameraProfiles.find((camera) => camera.id === selectId));
  syncCameraPickers();
}

async function saveCamera(event) {
  event.preventDefault();
  if (savingCamera) return;
  let payload;
  try {
    payload = cameraPayload();
    if (!payload.name) throw new Error("Enter a camera name before saving.");
  } catch (error) { showError(error.message); return; }
  const camera = selectedCamera;
  if (camera) payload.expected_revision = camera.revision;
  savingCamera = true;
  const controls = ["cameraSelect", "cameraName", "cameraLocation", "saveCamera", "newCamera", "reloadCamera", "useFullFrame", "roiReferenceFile", "lowRatio", "moderateRatio", "criticalRatio", "sampleN", "confirmSec", "clearSec", "reminderSec", "hysteresisRatio", "maxGapSec", "emaTauSec", "alertMinimum"];
  controls.forEach((id) => { $(id).disabled = true; });
  updateCameraStatus();
  $("analysisError").classList.add("hidden");
  try {
    const saved = await api(camera ? `/api/cameras/${camera.id}` : "/api/cameras", {
      method: camera ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    // Keep the saved response even if refreshing the list fails afterwards.
    if (![...$("cameraSelect").options].some((option) => option.value === saved.id)) {
      $("cameraSelect").add(new Option(`${saved.name} (v${saved.revision})`, saved.id));
    }
    applyCamera(saved);
    await loadCameras(saved.id);
  } catch (error) { showError(error.message); }
  finally {
    savingCamera = false;
    controls.forEach((id) => { $(id).disabled = false; });
    updateCameraStatus();
    syncCameraPickers();
  }
}

function releaseReferenceMedia() {
  if (referenceVideo) {
    referenceVideo.pause();
    referenceVideo.removeAttribute("src");
    referenceVideo.load();
    referenceVideo = null;
  }
  if (referenceUrl) URL.revokeObjectURL(referenceUrl);
  referenceUrl = null;
}

async function openRoiReference(file) {
  if (!file) return;
  const cameraId = selectedCamera?.id;
  $("roiReferenceFile").disabled = true;
  releaseReferenceMedia();
  referenceUrl = URL.createObjectURL(file);
  try {
    const isImage = file.type.startsWith("image/") || /\.(png|jpe?g|webp|bmp)$/i.test(file.name);
    const media = isImage ? new Image() : document.createElement("video");
    if (!isImage) {
      referenceVideo = media;
      media.preload = "auto";
      media.muted = true;
      media.playsInline = true;
    }
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error("Reference could not be decoded within 20 seconds.")), 20000);
      function finish(error) {
        clearTimeout(timer);
        media.onload = media.onloadeddata = media.onerror = null;
        error ? reject(error) : resolve();
      }
      media[isImage ? "onload" : "onloadeddata"] = () => finish();
      media.onerror = () => finish(new Error("Cannot decode this reference. Try an image or an MP4 supported by your browser."));
      media.src = referenceUrl;
      if (!isImage) media.load();
    });
    if (cameraId !== selectedCamera?.id) throw new Error("The selected camera changed. Choose the reference again.");
    const width = isImage ? media.naturalWidth : media.videoWidth;
    const height = isImage ? media.naturalHeight : media.videoHeight;
    if (!width || !height) throw new Error("Reference has no image dimensions.");
    const scale = Math.min(1, 960 / width, 540 / height);
    const image = document.createElement("canvas");
    image.width = Math.max(1, Math.round(width * scale));
    image.height = Math.max(1, Math.round(height * scale));
    image.getContext("2d").drawImage(media, 0, 0, image.width, image.height);
    roiReference = { image, width, height };
    const compatible = draftReference && Math.abs((width / height) / (draftReference.width / draftReference.height) - 1) <= 0.01;
    editorPoints = compatible && draftRoi ? draftRoi.map((point) => [...point]) : [];
    $("roiCanvas").width = image.width;
    $("roiCanvas").height = image.height;
    drawRoiEditor();
    $("roiDialog").showModal();
  } catch (error) { showError(error.message); }
  finally {
    releaseReferenceMedia();
    $("roiReferenceFile").disabled = false;
  }
}

function drawRoiEditor() {
  if (!roiReference) return;
  const canvas = $("roiCanvas");
  const ctx = canvas.getContext("2d");
  ctx.drawImage(roiReference.image, 0, 0);
  if (editorPoints.length) {
    ctx.beginPath();
    editorPoints.forEach(([x, y], index) => {
      if (!index) ctx.moveTo(x * (canvas.width - 1), y * (canvas.height - 1));
      else ctx.lineTo(x * (canvas.width - 1), y * (canvas.height - 1));
    });
    if (editorPoints.length >= 3) {
      ctx.closePath();
      ctx.fillStyle = "rgba(75, 180, 255, .28)";
      ctx.fill();
    }
    ctx.strokeStyle = "#ffe34f";
    ctx.lineWidth = 2;
    ctx.stroke();
    editorPoints.forEach(([x, y], index) => {
      ctx.beginPath(); ctx.arc(x * (canvas.width - 1), y * (canvas.height - 1), 4, 0, Math.PI * 2);
      ctx.fillStyle = "#ffe34f"; ctx.fill();
      ctx.font = "13px system-ui";
      ctx.fillText(String(index + 1), x * (canvas.width - 1) + 7, y * (canvas.height - 1) - 7);
    });
  }
  $("applyRoi").disabled = editorPoints.length < 3;
  $("undoRoiPoint").disabled = !editorPoints.length;
  $("roiEditorStatus").textContent = `${editorPoints.length}/32 corners | ${roiReference.width} x ${roiReference.height}.`;
}

function initializeCameraControls() {
  $("cameraForm").addEventListener("submit", saveCamera);
  $("cameraSelect").addEventListener("change", () => applyCamera(cameraProfiles.find((camera) => camera.id === $("cameraSelect").value)));
  $("newCamera").addEventListener("click", () => applyCamera(null));
  $("reloadCamera").addEventListener("click", () => loadCameras(selectedCamera?.id || "").catch((error) => showError(error.message)));
  ["cameraName", "cameraLocation", "lowRatio", "moderateRatio", "criticalRatio", "sampleN", "confirmSec", "clearSec", "reminderSec", "hysteresisRatio", "maxGapSec", "emaTauSec", "alertMinimum"].forEach((id) => $(id).addEventListener("input", updateCameraStatus));
  $("useFullFrame").addEventListener("click", () => { draftRoi = draftReference = null; updateCameraStatus(); });
  $("roiReferenceFile").addEventListener("change", (event) => {
    openRoiReference(event.target.files[0]);
    event.target.value = "";
  });
  $("roiCanvas").addEventListener("click", (event) => {
    if (!roiReference || editorPoints.length >= 32) return;
    const rect = $("roiCanvas").getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    if (editorPoints.some(([px, py]) => Math.hypot(px - x, py - y) < 0.003)) return;
    editorPoints.push([x, y]);
    drawRoiEditor();
  });
  $("undoRoiPoint").addEventListener("click", () => { editorPoints.pop(); drawRoiEditor(); });
  $("clearRoiPoints").addEventListener("click", () => { editorPoints = []; drawRoiEditor(); });
  $("cancelRoi").addEventListener("click", () => $("roiDialog").close());
  $("applyRoi").addEventListener("click", () => {
    if (!roiReference || editorPoints.length < 3) return;
    draftRoi = editorPoints.map((point) => [...point]);
    draftReference = { width: roiReference.width, height: roiReference.height };
    updateCameraStatus();
    $("roiDialog").close();
  });
  $("roiDialog").addEventListener("close", () => { releaseReferenceMedia(); roiReference = null; editorPoints = []; });
}

function formatTime(epoch) {
  if (!epoch) return "--";
  return new Date(Number(epoch) * 1000).toLocaleString();
}

function showError(message) {
  $("analysisError").textContent = message;
  $("analysisError").classList.remove("hidden");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
}

$("refreshAlerts").addEventListener("click", loadAlerts);
$("refreshHistory").addEventListener("click", loadHistory);
$("refreshEvents").addEventListener("click", loadEvents);
$("closeEvent").addEventListener("click", () => $("eventDialog").close());
$("eventsList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-event-id]");
  if (button) showEvent(button.dataset.eventId);
});
window.addEventListener("resize", () => {
  if (currentChart) requestAnimationFrame(() => drawTimeline(currentChart.points, currentChart.limits));
});
initializeCameraControls();
initializeWorkflow();
initializePreviewControls();
initializeLiveControls();
initializeDeliveryControls();
initializeOperationsControls();
loadHealth().then(() => loadCameras()).catch((error) => showError(error.message));
loadAlerts();
loadHistory();
loadEvents();
updateLegend();
