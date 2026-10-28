// Video playback has its own decoder and clock; AI results never pace the player.
let smoothPlayback = null;

function ownsPlaybackBridge(run) {
  return smoothPlayback?.bridge === run;
}

function clearSegmentationOverlay() {
  const canvas = $("segmentationOverlay");
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  canvas.classList.remove("visible");
}

function renderSegmentationOverlay(state, sample) {
  state.desiredOverlay = sample;
  if (state.visibleOverlay === sample) return;
  clearSegmentationOverlay();
  state.visibleOverlay = null;
  if (!sample?.overlay_png) return;
  let image = state.overlayCache.get(sample);
  if (!image) {
    image = new Image();
    state.overlayCache.set(sample, image);
    image.onload = () => {
      if (smoothPlayback === state && state.desiredOverlay === sample) renderSmoothObservation();
    };
    image.src = `data:image/png;base64,${sample.overlay_png}`;
    // Keep compressed samples for seeking, but only a few decoded GPU/CPU images.
    while (state.overlayCache.size > 4) {
      const oldest = state.overlayCache.keys().next().value;
      state.overlayCache.get(oldest).onload = null;
      state.overlayCache.delete(oldest);
    }
  }
  if (!image.complete || !image.naturalWidth) return;
  const canvas = $("segmentationOverlay");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  canvas.getContext("2d").drawImage(image, 0, 0);
  canvas.classList.add("visible");
  state.visibleOverlay = sample;
}

function stopPlaybackClock(state) {
  const video = $("videoPlayer");
  if (state.frameCallback != null) video.cancelVideoFrameCallback(state.frameCallback);
  if (state.animationCallback != null) cancelAnimationFrame(state.animationCallback);
  state.frameCallback = state.animationCallback = null;
}

function schedulePlaybackClock(state) {
  const video = $("videoPlayer");
  if (smoothPlayback !== state || state.cancelled || state.failed || video.paused || video.ended) return;
  if (video.requestVideoFrameCallback) {
    if (state.frameCallback != null) return;
    state.frameCallback = video.requestVideoFrameCallback((now, frame) => {
      state.frameCallback = null;
      renderSmoothObservation(frame.mediaTime);
      schedulePlaybackClock(state);
    });
  } else if (state.animationCallback == null) {
    state.animationCallback = requestAnimationFrame(() => {
      state.animationCallback = null;
      renderSmoothObservation();
      schedulePlaybackClock(state);
    });
  }
}

function releaseSmoothPlayback() {
  const previous = smoothPlayback;
  smoothPlayback = null;
  clearSegmentationOverlay();
  if (!previous) return;
  stopPlaybackClock(previous);
  $("videoPlayer").removeEventListener("playing", previous.onPlaying);
  for (const image of previous.overlayCache.values()) image.onload = null;
  previous.overlayCache.clear();
  $("videoPlayer").oncanplay = null;
  $("videoPlayer").onerror = null;
  $("videoPlayer").pause();
  $("videoPlayer").removeAttribute("src");
  $("videoPlayer").load();
  if (previous.url.startsWith("blob:")) URL.revokeObjectURL(previous.url);
  if (previous.bridge?.socket) previous.bridge.socket.close();
  $("playbackNote").textContent = "";
  $("analysisSnapshotPanel").classList.add("hidden");
  $("analysisSnapshot").removeAttribute("src");
}

function startSmoothPlayback(run, url, native = false) {
  const state = smoothPlayback = { url, run, bridge: native ? run : null, samples: [],
    maxGap: Math.min(run.maxGap, Math.max(.25, run.interval * 1.5)),
    scope: run.scope, analysisDone: false, failed: false, cancelled: false,
    started: false, overlayCache: new Map(), desiredOverlay: null, visibleOverlay: null };
  const video = $("videoPlayer");
  clearSegmentationOverlay();
  video.src = url;
  video.muted = true;
  video.classList.add("visible");
  $("previewImage").classList.remove("visible");
  $("emptyState").classList.add("hidden");
  $("playbackNote").textContent = native
    ? "Đang mở video và phân tích khung đầu tiên…"
    : "AI đang phân tích khung đầu tiên. Video sẽ tự phát cùng vùng nước được đánh dấu.";
  state.startWhenReady = () => {
    if (smoothPlayback !== state || state.cancelled || state.started || !state.samples.length || video.readyState < 2) return;
    state.started = true;
    $("playbackNote").textContent = "Vùng nước được tô xanh theo mốc AI của video. Chỉ số và mức cảnh báo ở bên dưới.";
    video.play().catch(() => {
      if (smoothPlayback === state) $("playbackNote").textContent = "Nhấn nút phát để xem video. AI vẫn phân tích ở nền.";
    });
    video.oncanplay = null;
  };
  video.oncanplay = state.startWhenReady;
  state.onPlaying = () => schedulePlaybackClock(state);
  video.addEventListener("playing", state.onPlaying);
  video.onerror = () => {
    if (smoothPlayback !== state) return;
    state.failed = true;
    stopPlaybackClock(state);
    clearSegmentationOverlay();
    video.classList.remove("visible");
    $("playbackNote").textContent = state.cancelled ? "Đã dừng video." : "Chưa phát được video liên tục. Đang hiển thị ảnh AI có mốc thời gian.";
    if ($("analysisSnapshot").src) {
      $("previewImage").src = $("analysisSnapshot").src;
      $("previewImage").classList.add("visible");
    }
    const latest = state.samples.at(-1);
    renderObservation("video", latest || { ratio_scope: state.scope }, {
      available: Boolean(latest && !state.cancelled), note: "Ảnh AI lấy mẫu, không phải video phát liên tục." });
  };
  video.load();
}

function recordSmoothObservation(run, result) {
  const state = smoothPlayback;
  if (!state || state.run !== run) return false;
  state.samples.push({ ...result.live, time_sec: result.time_sec, overlay_png: result.overlay_png });
  $("analysisSnapshot").src = `data:image/jpeg;base64,${result.annotated_jpeg}`;
  $("analysisSnapshotTime").textContent = `Ảnh AI mới nhất · ${result.time_sec.toFixed(1)} giây`;
  $("analysisSnapshotPanel").classList.remove("hidden");
  if (state.failed) {
    $("previewImage").src = $("analysisSnapshot").src;
    $("previewImage").classList.add("visible");
    renderObservation("video", result.live, { note: "Ảnh AI lấy mẫu, không phải video phát liên tục." });
  } else {
    renderSmoothObservation();
    state.startWhenReady();
  }
  return true;
}

function renderSmoothObservation(mediaTime) {
  const state = smoothPlayback;
  if (!state || state.failed) return;
  const at = typeof mediaTime === "number" ? mediaTime : $("videoPlayer").currentTime;
  let left = 0, right = state.samples.length;
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    if (state.samples[mid].time_sec <= at + 1e-6) left = mid + 1;
    else right = mid;
  }
  const sample = state.samples[left - 1];
  const fresh = Boolean(!state.cancelled && sample && at - sample.time_sec <= state.maxGap);
  const latest = state.samples.at(-1)?.time_sec;
  renderSegmentationOverlay(state, fresh ? sample : null);
  const note = state.cancelled ? "Phân tích đã dừng." : !fresh
    ? `Video ở ${at.toFixed(1)} giây · ${latest == null ? "AI đang chuẩn bị kết quả đầu tiên…" : `AI đã đọc đến ${latest.toFixed(1)} giây`}`
    : `Vùng nước và chỉ số theo mẫu AI tại ${sample.time_sec.toFixed(1)} giây.`;
  const observationKey = `${left - 1}|${fresh}|${note}`;
  if (state.observationKey !== observationKey) {
    state.observationKey = observationKey;
    renderObservation("video", sample || { ratio_scope: state.scope }, { available: fresh, note });
  }
}

function finishSmoothAnalysis(run, cancelled = false) {
  if (smoothPlayback?.run !== run) return;
  smoothPlayback.analysisDone = true;
  smoothPlayback.cancelled = cancelled;
  if (cancelled) {
    $("videoPlayer").pause();
    stopPlaybackClock(smoothPlayback);
    renderSegmentationOverlay(smoothPlayback, null);
  }
}
