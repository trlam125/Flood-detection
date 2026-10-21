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
      const lvl = job.live.level;
      if (lvl) {
        $("liveLevel").classList.remove("hidden");
        const confStr = job.live.level_confidence ? ` (${(job.live.level_confidence * 100).toFixed(0)}%)` : "";
        $("liveLevel").textContent = lvl + confStr;
        $("liveLevel").className = `level-badge-tag ${lvl.toLowerCase()}`;
      }
      const lvlPart = lvl && lvl !== "NO_CAR" ? ` | Mức ngập: ${lvl}` : "";
      $("liveMetric").textContent = `${job.live.severity} | Flooded area ${pct(job.live.water_ratio)}${lvlPart}`;
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
  $("mediaSubtitle").textContent = "Annotated result: segmentation mask, severity, flooded-area ratio and flood level metrics.";
  setSeverityBadge(result.highest_severity);

  $("highestSeverity").textContent = result.highest_severity;
  $("highestSeverity").className = severityClass(result.highest_severity);
  $("maxRatio").textContent = pct(result.max_water_ratio);

  // Highest and Dominant flood levels from best_flood_level_model
  const hl = result.highest_level || "NO_CAR";
  const hlInfo = (result.level_info && result.level_info[hl]) || {};
  $("highestLevel").textContent = hlInfo.short_title || hl;
  $("highestLevel").className = `level-val ${(hl || "").toLowerCase()}`;

  const dl = result.dominant_level || "NO_CAR";
  const dlInfo = (result.level_info && result.level_info[dl]) || {};
  $("dominantLevel").textContent = dlInfo.short_title || dl;
  $("dominantLevel").className = `level-val ${(dl || "").toLowerCase()}`;

  $("alertCount").textContent = String(result.alert_count || 0);
  $("analysisMeta").textContent = `${result.analyzed_frames} inference frames | ${result.model.backend.toUpperCase()} | best_flood_level_model`;

  // Badges
  if ($("levelBadgeSummary")) {
    $("levelBadgeSummary").textContent = `Highest: ${hlInfo.short_title || hl}`;
    $("levelBadgeSummary").className = `level-badge-tag ${(hl || "neutral").toLowerCase()}`;
  }
  if ($("dominantLevelBadge")) {
    $("dominantLevelBadge").textContent = `Dominant: ${dlInfo.short_title || dl}`;
    $("dominantLevelBadge").className = `level-badge-tag ${(dl || "neutral").toLowerCase()}`;
  }

  // Draw both side-by-side timelines
  drawTimeline(result.timeline || [], result.thresholds || thresholds);
  drawLevelTimeline(result.timeline || []);

  // Populate level breakdown table
  const tbody = $("levelTableBody");
  if (tbody && result.level_breakdown && result.level_breakdown.length) {
    tbody.innerHTML = result.level_breakdown.map((item) => {
      const tagClass = (item.level || "level0").toLowerCase();
      return `
        <tr>
          <td><span class="level-tag ${tagClass}">${escapeHtml(item.short_title || item.level)}</span></td>
          <td><b>${escapeHtml(item.title)}</b><span class="sub-text">${escapeHtml(item.desc || "")}</span></td>
          <td><b>${escapeHtml(item.depth || "--")}</b></td>
          <td class="bar-cell">
            <div class="bar-track">
              <div class="bar-fill" style="width: ${item.percent}%; background: ${item.color};"></div>
            </div>
          </td>
          <td class="pct-cell" style="color: ${item.color};">${item.percent}% <span class="sub-text">(${item.count}f)</span></td>
        </tr>`;
    }).join("");
  }

  // Populate level advisory
  const advEl = $("levelAdvisory");
  if (advEl) {
    const severity = (hlInfo.severity || "SAFE").toLowerCase();
    advEl.className = `level-advisory ${severity}`;
    advEl.innerHTML = `
      <div style="font-weight: 800; margin-bottom: 5px;">
        📢 ĐÁNH GIÁ MỨC ĐỘ NGẬP XE (best_flood_level_model):
        <span class="level-tag ${(hl || '').toLowerCase()}" style="margin-left: 6px;">${escapeHtml(hlInfo.title || hl)}</span>
      </div>
      <div style="margin-bottom: 4px;"><b>Mức ngập cao nhất:</b> ${escapeHtml(hlInfo.title || hl)} [Độ sâu: ${escapeHtml(hlInfo.depth || '--')}]. ${escapeHtml(hlInfo.advisory || '')}</div>
      <div><b>Mức ngập phổ biến:</b> ${escapeHtml(dlInfo.title || dl)} [Độ sâu: ${escapeHtml(dlInfo.depth || '--')}]. ${escapeHtml(dlInfo.desc || '')}</div>
    `;
  }
}

function drawTimeline(points, limits) {
  const canvas = $("timelineChart");
  if (!canvas) return;
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

function drawLevelTimeline(points) {
  const canvas = $("levelTimelineChart");
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 800;
  const height = 220;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 78, right: 18, top: 16, bottom: 30 };
  const chartW = width - pad.left - pad.right;
  const chartH = height - pad.top - pad.bottom;

  const levelConfigs = {
    "Level4": { rank: 4, label: "L4 (>50cm)", color: "#ff3b3b" },
    "Level3": { rank: 3, label: "L3 (30-50cm)", color: "#ef5757" },
    "Level2": { rank: 2, label: "L2 (15-30cm)", color: "#ee963f" },
    "Level1": { rank: 1, label: "L1 (5-15cm)", color: "#e2c44f" },
    "Level0": { rank: 0, label: "L0 (<5cm)", color: "#55c978" },
  };

  const yOfRank = (r) => pad.top + chartH * (1 - (r / 4));

  // Background zones & grid lines for each level
  ctx.font = "10.5px system-ui";
  for (let r = 0; r <= 4; r++) {
    const y = yOfRank(r);
    const lvlKey = `Level${r}`;
    const cfg = levelConfigs[lvlKey];

    // Grid line
    ctx.strokeStyle = "#223344";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();

    // Axis label
    ctx.fillStyle = cfg.color;
    ctx.fillText(cfg.label, 6, y + 4);
  }

  const validPoints = (points || []).filter((p) => p.time_sec !== undefined);
  const maxT = Math.max(1, ...validPoints.map((p) => Number(p.time_sec || 0)));
  const xOf = (t) => pad.left + chartW * (Number(t || 0) / maxT);

  if (validPoints.length > 0) {
    // Draw step-wise progression line
    for (let i = 0; i < validPoints.length; i++) {
      const p = validPoints[i];
      const lvl = p.level || "Level0";
      const rank = levelConfigs[lvl] ? levelConfigs[lvl].rank : (lvl === "NO_CAR" ? 0 : 0);
      const x = xOf(p.time_sec);
      const y = yOfRank(rank);

      if (i > 0) {
        const prevP = validPoints[i - 1];
        const prevLvl = prevP.level || "Level0";
        const prevRank = levelConfigs[prevLvl] ? levelConfigs[prevLvl].rank : (prevLvl === "NO_CAR" ? 0 : 0);
        const prevX = xOf(prevP.time_sec);
        const prevY = yOfRank(prevRank);

        // Segment line
        const segColor = (levelConfigs[lvl] && levelConfigs[lvl].color) || "#55c978";
        ctx.strokeStyle = segColor;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.moveTo(prevX, prevY);
        ctx.lineTo(x, prevY);
        ctx.lineTo(x, y);
        ctx.stroke();
      }

      // Point marker
      const ptColor = (levelConfigs[lvl] && levelConfigs[lvl].color) || "#8ea2b6";
      ctx.fillStyle = ptColor;
      ctx.beginPath();
      ctx.arc(x, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  ctx.fillStyle = "#8fa2b5";
  ctx.fillText("0s", pad.left, height - 8);
  ctx.fillText(`${maxT.toFixed(1)}s`, width - pad.right - 34, height - 8);
}

async function loadAlerts() {
  try {
    const data = await api("/api/alerts?limit=25");
    const root = $("alertsList");
    const items = data.items || [];
    if (!items.length) { root.className = "list-empty"; root.textContent = "No alerts yet."; return; }
    root.className = "activity-list";
    root.innerHTML = items.map((item) => {
      const lvl = item.level || "NO_CAR";
      const lvlClass = lvl.toLowerCase();
      const confStr = item.level_confidence ? ` (${(item.level_confidence * 100).toFixed(0)}%)` : "";
      return `
        <div class="activity-row">
          <span class="status-dot ${severityClass(item.severity)}"></span>
          <div>
            <div class="alert-title-row">
              <b>${escapeHtml(item.severity)}</b>
              <span class="level-tag ${lvlClass}">${escapeHtml(lvl)}${confStr}</span>
              <span class="muted">${pct(item.water_ratio)} flooded</span>
              <span class="muted">t = ${Number(item.time_sec || 0).toFixed(1)}s</span>
            </div>
            <div style="font-size: 10.5px; color: #b8c8d8; margin: 3px 0 2px; line-height: 1.35;">${escapeHtml(item.message)}</div>
            <span class="muted">${formatTime(item.created_at)}</span>
          </div>
          <small>${(item.channels || []).map((x) => escapeHtml(x)).join(", ")}</small>
        </div>`;
    }).join("");
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
