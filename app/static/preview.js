// Local file decoding. Only one sampled frame is in flight at a time.
let activePreview = null;

class BrowserDecodeError extends Error {}

function checkPreview(run) {
  if (run.stopped || activePreview !== run) throw new DOMException("Analysis cancelled", "AbortError");
}

function releaseDecoder(run) {
  if (run.decoder) {
    run.decoder.pause();
    run.decoder.removeAttribute("src");
    run.decoder.load();
    run.decoder = null;
  }
  if (run.url) URL.revokeObjectURL(run.url);
  run.url = null;
}

function decoderEvent(run, eventName, action, timeoutMs = 12000) {
  return new Promise((resolve, reject) => {
    const decoder = run.decoder;
    let timer;
    const finish = (error) => {
      clearTimeout(timer);
      decoder.removeEventListener(eventName, ready);
      decoder.removeEventListener("error", failed);
      run.controller.signal.removeEventListener("abort", cancelled);
      error ? reject(error) : resolve();
    };
    const ready = () => finish();
    const failed = () => finish(new BrowserDecodeError("This browser cannot decode the selected video."));
    const cancelled = () => finish(new DOMException("Analysis cancelled", "AbortError"));
    decoder.addEventListener(eventName, ready, { once: true });
    decoder.addEventListener("error", failed, { once: true });
    run.controller.signal.addEventListener("abort", cancelled, { once: true });
    timer = setTimeout(() => finish(new BrowserDecodeError("The browser timed out while reading this video.")), timeoutMs);
    try { action(); } catch (error) { finish(error); }
  });
}

async function openLocalVideo(run, file) {
  run.decoder = document.createElement("video");
  run.decoder.preload = "auto";
  run.decoder.muted = true;
  run.decoder.playsInline = true;
  run.url = URL.createObjectURL(file);
  await decoderEvent(run, "loadeddata", () => { run.decoder.src = run.url; run.decoder.load(); }, 2500);
  checkPreview(run);
  const { videoWidth: width, videoHeight: height, duration } = run.decoder;
  if (!width || !height || !Number.isFinite(duration) || duration <= 0) throw new BrowserDecodeError("The browser cannot read video dimensions or duration.");
  return { width, height, duration };
}

async function localJpeg(run, at, canvas) {
  checkPreview(run);
  if (Math.abs(run.decoder.currentTime - at) > .0001) {
    await decoderEvent(run, "seeked", () => { run.decoder.currentTime = at; });
  }
  checkPreview(run);
  canvas.getContext("2d").drawImage(run.decoder, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", .94));
  if (!blob) throw new BrowserDecodeError("Cannot create a video frame in this browser.");
  return blob;
}

function uploadOriginal(run, file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    run.xhr = xhr;
    xhr.open("POST", `/api/previews/${run.jobId}/original`);
    xhr.upload.onprogress = (event) => {
      if (activePreview !== run || run.stopped) return;
      $("originalProgress").textContent = event.lengthComputable
        ? `Đang lưu video gốc ở nền: ${Math.round(event.loaded * 100 / event.total)}%` : "Đang lưu video gốc ở nền…";
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status < 200 || xhr.status >= 300) { reject(new Error(data.detail || `Original upload failed (${xhr.status}).`)); return; }
      if (activePreview === run && !run.stopped) $("originalProgress").textContent = "Đã nhận bản gốc. Video kết quả sẽ được lưu sau khi phân tích xong.";
      resolve(data);
    };
    xhr.onerror = () => reject(new Error("Original video upload failed. Check the connection and select the video again."));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

function finishPreviewUi(abortNetwork) {
  const run = activePreview;
  if (!run) return;
  run.stopped = true;
  activePreview = null;
  const keepBridge = !abortNetwork && ownsPlaybackBridge(run);
  if (!keepBridge) run.controller.abort();
  if (run.socket && !keepBridge) run.socket.close();
  finishSmoothAnalysis(run, abortNetwork);
  if (abortNetwork && run.xhr && run.xhr.readyState !== 4) run.xhr.abort();
  releaseDecoder(run);
  $("cancelAnalysis").classList.add("hidden");
}

async function stopPreview(run, message = "Đã dừng phân tích. Bạn có thể chọn video khác.") {
  if (activePreview !== run || run.stopped) return;
  const jobId = run.jobId;
  finishPreviewUi(true);
  activeJob = null;
  finishProgress();
  showError(message);
  if (jobId) {
    try {
      await api(`/api/previews/${jobId}/cancel`, { method: "POST", keepalive: true });
      const job = await api(`/api/jobs/${jobId}`);
      if (!activeJob && job.partial_result?.model?.backend) renderResult(job.partial_result);
      await Promise.all([loadEvents(), loadAlerts(), loadHistory()]);
    } catch (_) { /* The server expiry task also closes abandoned sessions. */ }
  }
  if (!activeJob && typeof resetVideoObservations === "function") resetVideoObservations(message);
}

async function selectVideoFile(file) {
  if (!file) return;
  if (activeJob) { showError("Video hiện tại đang được phân tích. Bấm Dừng nếu muốn chọn video khác."); return; }
  releaseSmoothPlayback();
  if ($("videoReadMode").value === "upload") { uploadFile(file); return; }
  let frozen, interval;
  try {
    if (savingCamera || cameraHasDraft()) throw new Error("Cấu hình camera chưa được lưu. Vào Thiết lập để lưu hoặc tải lại trước khi chọn video.");
    frozen = { options: readAnalysisOptions(), camera: selectedCamera ? JSON.parse(JSON.stringify(selectedCamera)) : null };
    interval = Number($("previewInterval").value);
    if (!Number.isFinite(interval) || interval < .01 || interval > 5 || interval > frozen.options.eventPolicy.max_gap_sec) {
      throw new Error("Read interval must be 0.01–5 seconds and no larger than the maximum observation gap.");
    }
  } catch (error) { showError(error.message); return; }
  const run = { controller: new AbortController(), stopped: false, reading: true, frameCount: 0, jobId: null, decoder: null, url: null, xhr: null,
    exportVideo: $("exportVideo").checked, originalStarted: false, socket: null,
    maxGap: frozen.options.eventPolicy.max_gap_sec, interval,
    scope: frozen.camera?.roi ? "roi" : "full_frame" };
  try {
    activePreview = run;
    activeJob = "opening";
    $("downloadResult").classList.add("hidden");
    $("analysisError").classList.add("hidden");
    $("uploadInfo").classList.remove("hidden");
    $("uploadInfo").textContent = file.name;
    document.querySelector(".upload-panel").classList.add("has-video");
    $("originalProgress").classList.remove("hidden");
    $("originalProgress").textContent = run.exportVideo ? "Kết quả hiện ngay; video kết quả được lưu ở nền." : "Kết quả hiện trong lúc đọc. Không cần tải lên toàn bộ file trước.";
    $("cancelAnalysis").classList.remove("hidden");
    $("analysisProgress").classList.remove("hidden");
    $("progressBar").style.width = "0%";
    $("progressPercent").textContent = "0%";
    $("progressText").textContent = "Đang mở video…";
    $("resultSection").classList.add("hidden");
    $("videoPlayer").pause();
    $("videoPlayer").classList.remove("visible");
    $("previewImage").classList.remove("visible");
    $("emptyState").classList.remove("hidden");
    $("emptyState").querySelector("strong").textContent = "Đang đọc video…";
    $("emptyState").querySelector("span").textContent = "Kết quả đầu tiên sẽ hiện sau khi khung hình được phân tích.";
    $("liveSeverity").title = "Confirmed state at the latest analysed frame";
    $("mediaSubtitle").textContent = file.name;
    resetVideoObservations("Đang chuẩn bị khung hình đầu tiên…");
    if (matchMedia("(max-width: 980px)").matches) document.querySelector(".media-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    let media;
    try { media = await openLocalVideo(run, file); }
    catch (error) {
      if (!(error instanceof BrowserDecodeError)) throw error;
      releaseDecoder(run);
      await readWithServer(run, file, frozen, interval);
      return;
    }
    checkPreview(run);
    startSmoothPlayback(run, URL.createObjectURL(file));
    const options = frozen.camera ? { camera_id: frozen.camera.id, camera_revision: frozen.camera.revision }
      : { sample_every_n_frames: frozen.options.sampleEveryN, low_ratio: frozen.options.low,
          moderate_ratio: frozen.options.moderate, critical_ratio: frozen.options.critical, event_policy: frozen.options.eventPolicy };
    const created = await api("/api/previews", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, size_bytes: file.size, duration_sec: media.duration,
                             width: media.width, height: media.height, interval_sec: interval, options, export_video: run.exportVideo }), signal: run.controller.signal });
    run.jobId = created.job_id;
    if (run.stopped || activePreview !== run) {
      api(`/api/previews/${run.jobId}/cancel`, { method: "POST", keepalive: true }).catch(() => {});
      return;
    }
    activeJob = run.jobId;
    $("uploadInfo").innerHTML = `<b>${escapeHtml(file.name)}</b><span>${media.width} × ${media.height} · ${media.duration.toFixed(1)} giây</span>`;
    showFirstAnalysisStatus();
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, created.max_frame_edge / Math.max(media.width, media.height));
    canvas.width = Math.max(2, Math.round(media.width * scale));
    canvas.height = Math.max(2, Math.round(media.height * scale));
    pollJob(run.jobId);
    for (let sequence = 0; sequence * interval < media.duration; sequence++) {
      const at = sequence * interval;
      const frame = await localJpeg(run, at, canvas);
      const result = await api(`/api/previews/${run.jobId}/frames?sequence=${sequence}&time_sec=${at}`, {
        method: "POST", body: frame, headers: { "Content-Type": "image/jpeg" }, signal: run.controller.signal,
      });
      checkPreview(run);
      await presentPreviewFrame(run, file, result, media.duration);
    }
    checkPreview(run);
    const completed = await api(`/api/previews/${run.jobId}/finish`, { method: "POST", signal: run.controller.signal });
    checkPreview(run);
    completeReading(run, completed);
  } catch (error) {
    if (run.stopped || activePreview !== run || error.name === "AbortError") return;
    const message = !run.decoder && !run.jobId
      ? "Không thể mở video. Tải lại trang và chọn file để thử lại." : error.message;
    await stopPreview(run, message);
    if (!activeJob && !$("previewImage").classList.contains("visible")) {
      $("emptyState").querySelector("strong").textContent = "Chưa đọc được video";
      $("emptyState").querySelector("span").textContent = "Chọn lại video để thử lại.";
    }
  }
}

async function presentPreviewFrame(run, file, result, duration) {
  checkPreview(run);
  run.frameCount = result.analyzed_frames;
  if (run.frameCount === 1) loadHealth(false);
  $("emptyState").classList.add("hidden");
  if (!recordSmoothObservation(run, result)) {
    $("previewImage").src = `data:image/jpeg;base64,${result.annotated_jpeg}`;
    $("previewImage").classList.add("visible");
    renderObservation("video", result.live);
  }
  const at = result.time_sec;
  $("progressBar").style.width = `${90 * at / duration}%`;
  $("progressPercent").textContent = `${Math.round(90 * at / duration)}%`;
  $("progressText").textContent = `Đã đọc ${at.toFixed(1)} / ${duration.toFixed(1)} giây`;
  document.dispatchEvent(new CustomEvent("analysis-preview-frame", { detail: { jobId: run.jobId, sequence: result.sequence, time: at, originalReady: result.original_ready } }));
  if (run.exportVideo && !run.originalStarted) {
    run.originalStarted = true;
    uploadOriginal(run, file).catch((error) => {
      if (activePreview === run && !run.stopped) stopPreview(run, error.message);
    });
  }
}

function completeReading(run, completed) {
  run.reading = false;
  finishSmoothAnalysis(run);
  releaseDecoder(run);
  if (completed.status === "done") {
    finishPreviewUi(false);
    activeJob = null;
    finishProgress();
  } else $("progressText").textContent = "Đã phân tích xong. Đang lưu video kết quả ở nền…";
  if (completed.analysis_result) renderResult(completed.analysis_result);
  loadEvents(); loadAlerts(); loadHistory();
}

function showFirstAnalysisStatus() {
  $("progressText").textContent = "AI đang phân tích khung đầu tiên…";
  $("emptyState").querySelector("strong").textContent = "Đang phân tích…";
  $("emptyState").querySelector("span").textContent = "Kết quả sẽ tự hiện tại đây.";
}

async function readWithServer(run, file, frozen, interval) {
  $("progressText").textContent = "Đang đọc video bằng bộ giải mã mở rộng…";
  const options = frozen.camera ? { camera_id: frozen.camera.id, camera_revision: frozen.camera.revision }
    : { sample_every_n_frames: frozen.options.sampleEveryN, low_ratio: frozen.options.low,
        moderate_ratio: frozen.options.moderate, critical_ratio: frozen.options.critical, event_policy: frozen.options.eventPolicy };
  const created = await api("/api/local-readers", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, size_bytes: file.size, interval_sec: interval, options, export_video: run.exportVideo, smooth_playback: true }),
    signal: run.controller.signal });
  checkPreview(run);
  await new Promise((resolve, reject) => {
    const url = new URL(`/api/local-readers/${created.reader_id}/bridge`, location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = run.socket = new WebSocket(url);
    let done = false, playbackDone = false, duration = 0;
    const timer = setTimeout(() => fail(new Error("Không đọc được video kịp thời. Kiểm tra kết nối hoặc thử file khác.")), 30000);
    const fail = (error) => { clearTimeout(timer); if (!done) { done = true; socket.close(); reject(error); } };
    run.controller.signal.addEventListener("abort", () => fail(new DOMException("Analysis cancelled", "AbortError")), { once: true });
    socket.onmessage = async (event) => {
      try {
        if ((run.stopped || activePreview !== run) && !ownsPlaybackBridge(run)) throw new DOMException("Cancelled", "AbortError");
        const message = JSON.parse(event.data);
        if (message.type === "read") {
          if (!Number.isInteger(message.offset) || !Number.isInteger(message.length) || message.offset < 0 || message.length < 1 ||
              message.length > created.chunk_bytes || message.offset + message.length > file.size) throw new Error("Yêu cầu đọc file không hợp lệ.");
          const bytes = await file.slice(message.offset, message.offset + message.length).arrayBuffer();
          if ((run.stopped || activePreview !== run) && !ownsPlaybackBridge(run)) return;
          const packet = new Uint8Array(bytes.byteLength + 4);
          new DataView(packet.buffer).setUint32(0, message.id);
          packet.set(new Uint8Array(bytes), 4);
          socket.send(packet);
        } else if (message.type === "ready") {
          clearTimeout(timer);
          run.jobId = activeJob = message.job_id;
          duration = message.duration_sec;
          if (message.playback_url) startSmoothPlayback(run, message.playback_url, true);
          showFirstAnalysisStatus();
          pollJob(run.jobId);
        } else if (message.type === "observation") {
          await presentPreviewFrame(run, file, message, duration);
          socket.send(JSON.stringify({ type: "ack", sequence: message.sequence }));
        } else if (message.type === "complete") {
          done = true;
          clearTimeout(timer);
          completeReading(run, message);
          resolve();
          if (playbackDone) socket.close();
        } else if (message.type === "playback_complete") {
          playbackDone = true;
          if (ownsPlaybackBridge(run) && !smoothPlayback.failed) $("playbackNote").textContent = "Vùng nước được tô xanh theo mốc AI của video. Chỉ số và mức cảnh báo ở bên dưới.";
          if (done) socket.close();
        } else if (message.type === "error") fail(new Error(message.message));
      } catch (error) { fail(error); }
    };
    socket.onerror = () => fail(new Error("Không kết nối được bộ đọc video. Kiểm tra kết nối và thử lại."));
    socket.onclose = () => {
      if (!done) fail(new Error("Kết nối đọc video đã ngắt. Giữ trang này mở trong khi phân tích."));
      socket.onmessage = socket.onerror = socket.onclose = null;
    };
  });
}

function initializePreviewControls() {
  $("cancelAnalysis").addEventListener("click", () => {
    if (activePreview) stopPreview(activePreview);
  });
  window.addEventListener("pagehide", () => {
    if (activePreview?.jobId) navigator.sendBeacon(`/api/previews/${activePreview.jobId}/cancel`);
    finishPreviewUi(true);
    releaseSmoothPlayback();
  });
}
