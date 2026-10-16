const $ = (id) => document.getElementById(id);
let activeJob = null;
let thresholds = { low: 0.10, moderate: 0.20, critical: 0.45 };

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function pct(value, digits = 1) {
  return `${(Number(value || 0) * 100).toFixed(digits)}%`;
}

function severityClass(value) {
  return String(value || "SAFE").toLowerCase();
}

function setSeverityBadge(value) {
  const el = $("liveSeverity");
  el.textContent = value || "SAFE";
  el.className = `severity-badge ${severityClass(value)}`;
}

function updateLegend() {
  const low = Number($("lowRatio").value || 10);
  const mod = Number($("moderateRatio").value || 20);
  const crit = Number($("criticalRatio").value || 45);
  $("safeText").textContent = `below ${low}%`;
  $("lowText").textContent = `${low}-${mod}%`;
  $("moderateText").textContent = `${mod}-${crit}%`;
  $("criticalText").textContent = `${crit}%+`;
}
["lowRatio", "moderateRatio", "criticalRatio"].forEach((id) => $(id).addEventListener("input", updateLegend));

async function loadHealth() {
  try {
    const h = await api("/api/health");
    thresholds = h.thresholds || thresholds;
    $("lowRatio").value = Math.round(thresholds.low * 100);
    $("moderateRatio").value = Math.round(thresholds.moderate * 100);
    $("criticalRatio").value = Math.round(thresholds.critical * 100);
    updateLegend();

    const model = $("modelBadge");
    model.textContent = h.model_exists ? `${String(h.model_backend).toUpperCase()} model ready` : "Water model missing";
    model.className = `badge ${h.model_exists ? "ok" : "bad"}`;
    $("deviceBadge").textContent = `Device ${h.device || "auto"}`;

    const channelBox = $("alertChannels");
    channelBox.innerHTML = "";
    (h.alert_channels || ["dashboard"]).forEach((name) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = name.toUpperCase();
      channelBox.appendChild(chip);
    });
  } catch (error) {
    $("modelBadge").textContent = "Backend unavailable";
    $("modelBadge").className = "badge bad";
  }
}

function readAnalysisOptions() {
  const low = Number($("lowRatio").value) / 100;
  const moderate = Number($("moderateRatio").value) / 100;
  const critical = Number($("criticalRatio").value) / 100;
  if (!(0 <= low && low < moderate && moderate < critical && critical <= 1)) {
    throw new Error("Thresholds must satisfy SAFE < LOW < MODERATE < CRITICAL and stay within 0-100%.");
  }
  return {
    low,
    moderate,
    critical,
    sampleEveryN: Number($("sampleN").value || 2),
  };
}

async function uploadFile(file) {
  if (!file) return;
  if (activeJob) {
    showError("An analysis is already running. Please wait for it to finish before uploading another video.");
    return;
  }

  $("analysisError").classList.add("hidden");
  let options;
  try {
    options = readAnalysisOptions();
  } catch (error) {
    showError(error.message);
    return;
  }

  $("uploadInfo").classList.remove("hidden");
  $("uploadInfo").textContent = `Uploading ${file.name}...`;
  $("progressOverlay").classList.remove("hidden");
  $("progressBar").style.width = "0%";
  $("progressPercent").textContent = "0%";
  $("progressText").textContent = "Uploading video";
  $("liveMetric").textContent = "";
  $("liveMetric").classList.add("hidden");
  $("resultSection").classList.add("hidden");
  setSeverityBadge("SAFE");

  const form = new FormData();
  form.append("file", file);
  form.append("sample_every_n_frames", String(options.sampleEveryN));
  form.append("low_ratio", String(options.low));
  form.append("moderate_ratio", String(options.moderate));
  form.append("critical_ratio", String(options.critical));

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
    $("liveMetric").classList.remove("hidden");
    $("liveMetric").textContent = "Waiting for first inference frame...";
    pollJob(upload.job_id);
  } catch (error) {
    activeJob = null;
    finishProgress();
    $("uploadInfo").textContent = error.message;
    showError(error.message);
  }
}

$("videoFile").addEventListener("change", (event) => {
  const file = event.target.files[0];
  uploadFile(file);
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
drop.addEventListener("drop", (event) => uploadFile(event.dataTransfer.files[0]));

async function pollJob(jobId) {
  if (activeJob !== jobId) return;
  try {
    const job = await api(`/api/jobs/${jobId}`);
    const progress = Number(job.progress || 0);
    $("progressBar").style.width = `${progress}%`;
    $("progressPercent").textContent = `${progress.toFixed(0)}%`;
    $("progressText").textContent = job.status === "running" ? `Processing ${job.processed_frames || 0} frames` : job.status;
    if (job.live) {
      setSeverityBadge(job.live.severity);
      $("liveMetric").textContent = `${job.live.severity} | Flooded area ${pct(job.live.water_ratio)}`;
    }
    if (job.status === "done") {
      activeJob = null;
      finishProgress();
      renderResult(job.result);
      await Promise.all([loadAlerts(), loadHistory()]);
      return;
    }
    if (job.status === "error") {
      activeJob = null;
      finishProgress();
      showError(job.error || "Analysis failed");
      return;
    }
    setTimeout(() => pollJob(jobId), 850);
  } catch (error) {
    activeJob = null;
    finishProgress();
    showError(error.message);
  }
}

function finishProgress() {
  $("progressOverlay").classList.add("hidden");
}

function renderResult(result) {
  $("resultSection").classList.remove("hidden");
  $("videoPlayer").src = `${result.output_url}?v=${Date.now()}`;
  $("videoPlayer").load();
  $("mediaSubtitle").textContent = "Annotated result: segmentation mask, severity and flooded-area ratio.";
  setSeverityBadge(result.highest_severity);
  $("highestSeverity").textContent = result.highest_severity;
  $("highestSeverity").className = severityClass(result.highest_severity);
  $("maxRatio").textContent = pct(result.max_water_ratio);
  $("meanRatio").textContent = pct(result.mean_water_ratio);
  $("alertCount").textContent = String(result.alert_count || 0);
  $("analysisMeta").textContent = `${result.analyzed_frames} inference frames | ${result.model.backend.toUpperCase()}`;
  drawTimeline(result.timeline || [], result.thresholds || thresholds);
}

function drawTimeline(points, limits) {
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
        <div><b>${escapeHtml(item.severity)}</b><span>${pct(item.water_ratio)} flooded | ${formatTime(item.created_at)}</span></div>
        <small>${(item.channels || []).map((x) => escapeHtml(x)).join(", ")}</small>
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
    root.innerHTML = items.map((item) => `
      <div class="activity-row">
        <span class="status-dot ${severityClass(item.highest_severity)}"></span>
        <div><b>${escapeHtml(item.highest_severity || item.status)}</b><span>${pct(item.max_water_ratio)} max | ${formatTime(item.created_at)}</span></div>
        <small>${item.analyzed_frames || 0} frames</small>
      </div>`).join("");
  } catch (_) {}
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
loadHealth();
loadAlerts();
loadHistory();
updateLegend();
