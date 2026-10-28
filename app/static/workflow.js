// Primary tasks stay separate from one-time setup and operational details.
function showWorkspace(name) {
  const allowed = ["video", "camera", "history", "settings", "operations"];
  if (!allowed.includes(name)) name = "video";
  document.querySelectorAll(".workspace").forEach((el) => { el.hidden = el.id !== `workspace-${name}`; });
  document.querySelectorAll("[data-workspace]").forEach((button) => {
    if (button.dataset.workspace === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (currentChart) requestAnimationFrame(() => drawTimeline(currentChart.points, currentChart.limits));
}

function syncCameraPickers() {
  for (const [id, label] of [["quickCameraSelect", "Chọn camera"], ["videoCameraSelect", "Toàn khung hình (mặc định)"]]) {
    const select = $(id);
    select.replaceChildren(new Option(label, ""));
    cameraProfiles.forEach((camera) => select.add(new Option(camera.name, camera.id)));
    select.value = selectedCamera?.id || "";
    select.disabled = savingCamera;
  }
}

function initializeWorkflow() {
  document.querySelectorAll("[data-workspace]").forEach((button) => {
    button.addEventListener("click", () => showWorkspace(button.dataset.workspace));
  });
  document.querySelectorAll("[data-open-setup]").forEach((button) => button.addEventListener("click", () => {
    showWorkspace("settings");
    $("cameraName").focus();
  }));
  ["quickCameraSelect", "videoCameraSelect"].forEach((id) => $(id).addEventListener("change", (event) => {
    if (cameraHasDraft()) {
      showError("Bạn có cấu hình chưa lưu. Hãy lưu hoặc tải lại trong Thiết lập trước khi đổi camera.");
      syncCameraPickers();
      return;
    }
    applyCamera(cameraProfiles.find((camera) => camera.id === event.target.value));
  }));
  document.querySelectorAll("details").forEach((details) => details.addEventListener("toggle", () => {
    if (details.open && currentChart) requestAnimationFrame(() => drawTimeline(currentChart.points, currentChart.limits));
  }));
  showWorkspace("video");
}
