const $ = (id) => document.getElementById(id);

let uploaded = null;
let calPoints = [];
let calibrationExists = false;
let savedCalibration = null;
let calibrating = false;
let pollTimer = null;
let activeJobId = null;
let loadedResultVideoUrl = null;

const uploadBox = $("uploadBox");
const fileInput = $("videoFile");
const previewImage = $("previewImage");
const canvas = $("calCanvas");
const ctx = canvas.getContext("2d");

async function api(url, options = {}) {
  const res = await fetch(url, options);
  let data = null;
  try { data = await res.json(); } catch (_) { }
  if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
  return data;
}

async function checkHealth() {
  try {
    const data = await api("/api/health");
    const badge = $("modelBadge");
    if (data.ok) {
      badge.className = "model-badge ok";
      badge.textContent = `Model ready · ${data.model.device} · t=${Number(data.model.threshold).toFixed(2)}`;
    } else {
      badge.className = "model-badge bad";
      badge.textContent = "Model not ready";
    }
  } catch (_) {
    $("modelBadge").className = "model-badge bad";
    $("modelBadge").textContent = "Model error";
  }
}

function setCalibrationBadge(state, text) {
  const badge = $("calibrationBadge");
  badge.className = `state-pill ${state}`;
  badge.textContent = text;
}

function calibrationPointsFromData(data) {
  if (!uploaded || !data?.exists) return [];
  const zx = data.zero_x ?? uploaded.width * 0.5;
  const kx = data.known_x ?? zx;
  return [
    { x: zx, y: data.zero_y },
    { x: kx, y: data.known_y },
  ];
}

async function checkCalibrationProfile({ loadPoints = true, silent = false } = {}) {
  const profile = $("profile").value.trim() || "default";
  try {
    const data = await api(`/api/calibration/${encodeURIComponent(profile)}`);
    calibrationExists = Boolean(data.exists);
    savedCalibration = data.exists ? data : null;
    if (data.exists) {
      setCalibrationBadge("ok", "Auto level ready");
      $("knownCm").value = data.known_cm;
      if (loadPoints && uploaded) calPoints = calibrationPointsFromData(data);
      if (!silent) $("calStatus").textContent = `Saved scale: ${Number(data.cm_per_pixel).toFixed(4)} cm/pixel`;
    } else {
      setCalibrationBadge("warning", "Area fallback");
      savedCalibration = null;
      if (!calibrating) calPoints = [];
      if (!silent) $("calStatus").textContent = "No calibration saved for this fixed camera. Water-area alerts will still work.";
    }
    drawPoints();
    updatePointStatus();
    return data;
  } catch (e) {
    calibrationExists = false;
    savedCalibration = null;
    setCalibrationBadge("bad", "Calibration error");
    if (!silent) $("calStatus").textContent = e.message;
    return null;
  }
}

function setCalibrating(on) {
  calibrating = on;
  $("manualPanel").classList.toggle("hidden", !on);
  $("toggleCalibration").textContent = on ? "Cancel calibration" : "Set / update calibration";
  canvas.style.pointerEvents = on ? "auto" : "none";
  if (on && calibrationExists && savedCalibration) calPoints = calibrationPointsFromData(savedCalibration);
  if (on && !calibrationExists) calPoints = [];
  drawPoints();
  updatePointStatus();
}

$("toggleCalibration").addEventListener("click", () => setCalibrating(!calibrating));

uploadBox.addEventListener("dragover", (e) => { e.preventDefault(); uploadBox.classList.add("drag"); });
uploadBox.addEventListener("dragleave", () => uploadBox.classList.remove("drag"));
uploadBox.addEventListener("drop", (e) => {
  e.preventDefault();
  uploadBox.classList.remove("drag");
  if (e.dataTransfer.files?.[0]) uploadVideo(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files?.[0]) uploadVideo(fileInput.files[0]);
});

async function uploadVideo(file) {
  stopPolling();
  resetResultVideo();
  $("uploadStatus").textContent = "Uploading...";
  $("analyzeBtn").disabled = true;
  const fd = new FormData();
  fd.append("file", file);
  try {
    uploaded = await api("/api/upload", { method: "POST", body: fd });
    $("uploadStatus").textContent = file.name;
    renderMeta(uploaded);
    previewImage.onload = async () => {
      fitCanvas();
      await checkCalibrationProfile({ loadPoints: true, silent: true });
      drawPoints();
    };
    previewImage.src = `${uploaded.preview_url}?t=${Date.now()}`;
    previewImage.classList.remove("hidden");
    canvas.classList.remove("hidden");
    $("previewPlaceholder").classList.add("hidden");
    $("analyzeBtn").disabled = false;
    updatePointStatus();
  } catch (e) {
    $("uploadStatus").textContent = `Error: ${e.message}`;
  }
}

function renderMeta(meta) {
  const duration = meta.duration_sec == null ? "-" : `${meta.duration_sec.toFixed(1)}s`;
  $("videoMeta").innerHTML = `
    <div><span>Size</span><strong>${meta.width}×${meta.height}</strong></div>
    <div><span>FPS</span><strong>${Number(meta.fps).toFixed(1)}</strong></div>
    <div><span>Frames</span><strong>${meta.frames}</strong></div>
    <div><span>Length</span><strong>${duration}</strong></div>`;
  $("videoMeta").classList.remove("hidden");
}

function fitCanvas() {
  const wrapRect = $("previewWrap").getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(wrapRect.width));
  canvas.height = Math.max(1, Math.round(wrapRect.height));
  drawPoints();
}
window.addEventListener("resize", fitCanvas);

canvas.addEventListener("click", (e) => {
  if (!calibrating || !uploaded) return;
  const rect = canvas.getBoundingClientRect();
  const xCanvas = e.clientX - rect.left;
  const yCanvas = e.clientY - rect.top;
  const imgRect = previewImage.getBoundingClientRect();
  const wrapRect = $("previewWrap").getBoundingClientRect();
  const imgLeft = imgRect.left - wrapRect.left;
  const imgTop = imgRect.top - wrapRect.top;
  const xIn = xCanvas - imgLeft;
  const yIn = yCanvas - imgTop;
  if (xIn < 0 || yIn < 0 || xIn > imgRect.width || yIn > imgRect.height) return;

  const naturalX = xIn * uploaded.width / imgRect.width;
  const naturalY = yIn * uploaded.height / imgRect.height;
  if (calPoints.length >= 2) calPoints = [];
  calPoints.push({ x: naturalX, y: naturalY });
  drawPoints();
  updatePointStatus();
});

function drawPoints() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!uploaded || !calPoints.length || previewImage.classList.contains("hidden")) return;

  const imgRect = previewImage.getBoundingClientRect();
  const wrapRect = $("previewWrap").getBoundingClientRect();
  const left = imgRect.left - wrapRect.left;
  const top = imgRect.top - wrapRect.top;
  const mapped = calPoints.map((p) => ({
    x: left + p.x * imgRect.width / uploaded.width,
    y: top + p.y * imgRect.height / uploaded.height,
  }));

  const centerX = mapped.reduce((s, p) => s + p.x, 0) / mapped.length;
  const zoneWidth = imgRect.width * 0.16;
  ctx.fillStyle = "rgba(69, 167, 255, .08)";
  ctx.fillRect(centerX - zoneWidth / 2, top, zoneWidth, imgRect.height);
  ctx.strokeStyle = "rgba(69, 167, 255, .45)";
  ctx.setLineDash([5, 5]);
  ctx.strokeRect(centerX - zoneWidth / 2, top, zoneWidth, imgRect.height);
  ctx.setLineDash([]);

  ctx.lineWidth = 2;
  ctx.font = "bold 12px system-ui";
  mapped.forEach((p, i) => {
    ctx.beginPath();
    ctx.fillStyle = i === 0 ? "#45a7ff" : "#f0b84b";
    ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillText(i === 0 ? "0 cm baseline" : "known reference", p.x + 9, p.y - 7);
    ctx.strokeStyle = i === 0 ? "rgba(69,167,255,.7)" : "rgba(240,184,75,.7)";
    ctx.beginPath();
    ctx.moveTo(centerX - zoneWidth / 2, p.y);
    ctx.lineTo(centerX + zoneWidth / 2, p.y);
    ctx.stroke();
  });
  if (mapped.length === 2) {
    ctx.strokeStyle = "#ffffff";
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(mapped[0].x, mapped[0].y);
    ctx.lineTo(mapped[1].x, mapped[1].y);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

function updatePointStatus() {
  const pill = $("pointStatus");
  if (!uploaded) { pill.textContent = "Upload a video"; return; }
  if (!calibrating) {
    pill.textContent = calibrationExists ? "Automatic level ready" : "Area fallback";
    return;
  }
  if (calPoints.length === 0) pill.textContent = "Click 0 cm baseline";
  if (calPoints.length === 1) pill.textContent = "Click known reference";
  if (calPoints.length === 2) pill.textContent = "Ready to save";
}

$("resetPoints").addEventListener("click", () => {
  calPoints = [];
  drawPoints();
  updatePointStatus();
  $("calStatus").textContent = "";
});

$("saveCalibration").addEventListener("click", async () => {
  if (!uploaded) { $("calStatus").textContent = "Upload a video first."; return; }
  if (calPoints.length !== 2) { $("calStatus").textContent = "Select both calibration points first."; return; }
  const knownCm = Number($("knownCm").value);
  if (!Number.isFinite(knownCm) || knownCm <= 0) { $("calStatus").textContent = "Reference distance must be greater than 0."; return; }

  const payload = {
    profile: $("profile").value.trim() || "default",
    zero_y: calPoints[0].y,
    known_y: calPoints[1].y,
    known_cm: knownCm,
    zero_x: calPoints[0].x,
    known_x: calPoints[1].x,
    frame_width: uploaded.width,
  };

  try {
    const data = await api("/api/calibration", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    calibrationExists = true;
    setCalibrationBadge("ok", "Auto level ready");
    $("calStatus").textContent = `Saved · ${data.cm_per_pixel.toFixed(4)} cm/pixel · automatic measurement zone enabled`;
    await checkCalibrationProfile({ loadPoints: true, silent: true });
    setCalibrating(false);
  } catch (e) {
    $("calStatus").textContent = `Error: ${e.message}`;
  }
});

$("loadProfile").addEventListener("click", async () => {
  await checkCalibrationProfile({ loadPoints: true, silent: false });
});

$("profile").addEventListener("change", async () => {
  calPoints = [];
  await checkCalibrationProfile({ loadPoints: true, silent: true });
});

$("analyzeBtn").addEventListener("click", async () => {
  if (!uploaded) return;
  $("analysisError").classList.add("hidden");
  $("resultSection").classList.add("hidden");
  $("progressArea").classList.remove("hidden");
  $("analyzeBtn").disabled = true;

  const payload = {
    video_id: uploaded.video_id,
    calibration_mode: "auto",
    profile: $("profile").value.trim() || "default",
    warning_cm: Number($("warningCm").value),
    danger_cm: Number($("dangerCm").value),
    warning_ratio: Number($("warningRatioPct").value) / 100,
    danger_ratio: Number($("dangerRatioPct").value) / 100,
    sample_every_n_frames: Number($("sampleN").value),
  };

  try {
    const data = await api("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    pollJob(data.job_id);
  } catch (e) {
    showAnalysisError(e.message);
  }
});

function resetResultVideo() {
  const video = $("resultVideo");
  if (!video) return;
  video.pause();
  video.removeAttribute("src");
  video.load();
  loadedResultVideoUrl = null;
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  activeJobId = null;
}

function pollJob(jobId) {
  stopPolling();
  activeJobId = jobId;
  const tick = async () => {
    if (activeJobId !== jobId) return;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (activeJobId !== jobId) return;
      const p = Number(job.progress || 0);
      $("progressBar").style.width = `${p}%`;
      $("progressPercent").textContent = `${p.toFixed(0)}%`;
      if (job.status === "running") $("progressText").textContent = `Processing ${job.processed_frames || 0} frames...`;
      else if (job.status === "queued") $("progressText").textContent = "Queued...";
      else $("progressText").textContent = job.status;

      if (job.status === "done") {
        stopPolling();
        $("analyzeBtn").disabled = false;
        renderResult(job.result);
        return;
      }
      if (job.status === "error") {
        stopPolling();
        showAnalysisError(job.error || "Analysis failed");
        return;
      }
      pollTimer = setTimeout(tick, 900);
    } catch (e) {
      stopPolling();
      showAnalysisError(e.message);
    }
  };
  tick();
}

function showAnalysisError(message) {
  $("analysisError").textContent = message;
  $("analysisError").classList.remove("hidden");
  $("analyzeBtn").disabled = false;
  $("progressArea").classList.add("hidden");
}

function formatWaterLevel(cm) {
  if (cm == null || Number.isNaN(Number(cm))) return "-";
  const value = Number(cm);
  return value >= 100 ? `${(value / 100).toFixed(2)} m` : `${value.toFixed(1)} cm`;
}

function renderResult(r) {
  $("progressArea").classList.add("hidden");
  $("resultSection").classList.remove("hidden");

  const calibrated = Boolean(r.calibration_used);
  $("calibratedMetrics").classList.toggle("hidden", !calibrated);
  $("areaMetrics").classList.toggle("hidden", calibrated);

  $("maxLevel").textContent = formatWaterLevel(r.max_water_level_cm);
  $("meanLevel").textContent = formatWaterLevel(r.mean_water_level_cm);
  $("maxRatio").textContent = `${(Number(r.max_water_ratio) * 100).toFixed(1)}%`;
  $("meanRatio").textContent = `${(Number(r.mean_water_ratio) * 100).toFixed(1)}%`;

  if (calibrated) {
    $("resultMeasurementHint").textContent = `Automatic waterline + saved calibration profile “${r.calibration_profile}”. Real-world level drives the alert.`;
    $("measurementModeChip").textContent = "Mode: automatic level";
    $("secondaryAreaChip").textContent = `Peak water area: ${(Number(r.max_water_ratio) * 100).toFixed(1)}%`;
    const conf = r.mean_waterline_confidence == null ? null : Number(r.mean_waterline_confidence) * 100;
    $("confidenceChip").textContent = conf == null ? "Waterline confidence: -" : `Waterline confidence: ${conf.toFixed(0)}%`;
  } else {
    $("resultMeasurementHint").textContent = "No saved calibration was found, so the alert used water-area percentage. Calibrate this fixed camera once to enable automatic cm/m levels.";
    $("measurementModeChip").textContent = "Mode: area fallback";
    $("secondaryAreaChip").textContent = "Physical level: calibration required";
    $("confidenceChip").textContent = "Waterline confidence: not used";
  }
  $("modelThresholdChip").textContent = `Model threshold: ${Number(r.model_threshold).toFixed(2)}`;

  const badge = $("statusBadge");
  badge.textContent = r.highest_status;
  badge.className = "status-badge";
  if (r.highest_status === "WARNING") badge.classList.add("warning");
  if (r.highest_status === "DANGER") badge.classList.add("danger");

  const video = $("resultVideo");
  if (loadedResultVideoUrl !== r.output_url) {
    resetResultVideo();
    video.src = r.output_url;
    video.load();
    loadedResultVideoUrl = r.output_url;
  }

  $("timeline").innerHTML = (r.timeline || []).map((x) => {
    const primary = calibrated ? `Level ${formatWaterLevel(x.water_level_cm)}` : `Area ${(x.water_ratio * 100).toFixed(1)}%`;
    const conf = calibrated && x.waterline_confidence != null ? `Confidence ${(x.waterline_confidence * 100).toFixed(0)}%` : "Area-based alert";
    return `<div class="timeline-row">
      <span>${x.time_sec.toFixed(1)}s</span>
      <span>${primary}</span>
      <span>${conf}</span>
      <strong class="${x.status.toLowerCase()}">${x.status}</strong>
    </div>`;
  }).join("");

  $("resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
}

setCalibrating(false);
checkHealth();
checkCalibrationProfile({ loadPoints: true, silent: true });
