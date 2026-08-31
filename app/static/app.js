const STORAGE_KEY = "current_video_job";
const POLL_DELAY_MS = 900;

const state = {
  jobId: null,
  pollToken: 0,
  downloadUrl: null,
  lastPhase: null,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  Object.assign(elements, {
    form: document.querySelector("#videoForm"),
    input: document.querySelector("#videoUrl"),
    analyze: document.querySelector("#analyzeButton"),
    download: document.querySelector("#downloadButton"),
    previewShell: document.querySelector("#previewShell"),
    preview: document.querySelector("#videoPreview"),
    placeholder: document.querySelector("#previewPlaceholder"),
    progress: document.querySelector("#progressBar"),
    progressValue: document.querySelector("#progressValue"),
    copyPanel: document.querySelector("#copyPanel"),
    publishedBlock: document.querySelector("#publishedBlock"),
    publishedText: document.querySelector("#publishedText"),
    publishedCopy: document.querySelector("#publishedCopyButton"),
    speechBlock: document.querySelector("#speechBlock"),
    speechState: document.querySelector("#speechState"),
    audioSummary: document.querySelector("#audioSummary"),
    speechText: document.querySelector("#speechText"),
    speechNotice: document.querySelector("#speechNotice"),
    speechCopy: document.querySelector("#speechCopyButton"),
    copyEmpty: document.querySelector("#copyEmpty"),
    error: document.querySelector("#errorText"),
    liveStatus: document.querySelector("#liveStatus"),
  });

  elements.form.addEventListener("submit", startAnalysis);
  elements.input.addEventListener("input", clearError);
  elements.download.addEventListener("click", (event) => {
    if (!state.downloadUrl) event.preventDefault();
  });
  elements.publishedCopy.addEventListener("click", () => {
    copyResult(elements.publishedText, elements.publishedCopy, "发布文案");
  });
  elements.speechCopy.addEventListener("click", () => {
    copyResult(elements.speechText, elements.speechCopy, "讲话文稿");
  });

  const savedJob = sessionStorage.getItem(STORAGE_KEY);
  if (/^[a-f0-9]{32}$/.test(savedJob || "")) restoreJob(savedJob);
});

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload?.detail;
    const message = detail?.message || (typeof detail === "string" ? detail : "解析失败，请稍后重试。");
    throw new Error(message);
  }
  return payload;
}

async function startAnalysis(event) {
  event.preventDefault();
  clearError();
  const url = elements.input.value.trim();
  if (!url) {
    elements.input.setCustomValidity("请粘贴视频链接");
    elements.input.reportValidity();
    elements.input.setCustomValidity("");
    return;
  }

  state.pollToken += 1;
  resetResult();
  setProcessing(true);
  setProgress(2);
  announce("正在分析视频");

  try {
    const job = await api("/api/v1/jobs", {
      method: "POST",
      body: JSON.stringify({ share_text: url }),
    });
    state.jobId = job.id;
    sessionStorage.setItem(STORAGE_KEY, job.id);
    applyJob(job);
    if (!job.terminal) pollJob(job.id, state.pollToken);
  } catch (error) {
    fail(error.message);
  }
}

async function restoreJob(jobId) {
  state.jobId = jobId;
  const token = ++state.pollToken;
  setProcessing(true);
  try {
    const job = await api(`/api/v1/jobs/${jobId}`);
    applyJob(job);
    if (!job.terminal) pollJob(jobId, token);
  } catch (_) {
    sessionStorage.removeItem(STORAGE_KEY);
    resetResult();
    setProcessing(false);
  }
}

async function pollJob(jobId, token) {
  if (token !== state.pollToken || jobId !== state.jobId) return;
  try {
    const job = await api(`/api/v1/jobs/${jobId}`);
    if (token !== state.pollToken) return;
    applyJob(job);
    if (!job.terminal) window.setTimeout(() => pollJob(jobId, token), POLL_DELAY_MS);
  } catch (error) {
    fail(error.message);
  }
}

function applyJob(job) {
  setProgress(job.progress);
  if (job.phase !== state.lastPhase) {
    state.lastPhase = job.phase;
    announce(job.message || "正在处理视频");
  }

  const video = (job.artifacts || []).find((artifact) => artifact.key === "video");
  if (video) setVideo(video, job.terminal && ["completed", "partial"].includes(job.status));
  applyCopy(job);

  if (!job.terminal) return;
  setProcessing(false);
  if (video && ["completed", "partial"].includes(job.status)) {
    setProgress(100);
    announce(job.status === "partial" ? "视频已准备完成，部分文案未生成" : "视频和文案已准备完成");
    return;
  }
  fail(job.error_message || "解析失败，请更换链接后重试。");
}

function setVideo(artifact, downloadable) {
  if (elements.preview.dataset.src !== artifact.url) {
    elements.preview.pause();
    elements.preview.dataset.src = artifact.url;
    elements.preview.src = artifact.url;
    elements.preview.hidden = false;
    elements.placeholder.hidden = true;
    elements.previewShell.dataset.state = "ready";
    elements.preview.load();
  }
  if (downloadable) {
    state.downloadUrl = artifact.download_url;
    elements.download.href = artifact.download_url;
    elements.download.setAttribute("download", "");
    elements.download.classList.remove("disabled");
    elements.download.removeAttribute("aria-disabled");
    elements.download.removeAttribute("tabindex");
  }
}

function resetResult() {
  state.jobId = null;
  state.downloadUrl = null;
  state.lastPhase = null;
  sessionStorage.removeItem(STORAGE_KEY);
  elements.preview.pause();
  elements.preview.removeAttribute("src");
  elements.preview.removeAttribute("data-src");
  elements.preview.load();
  elements.preview.hidden = true;
  elements.placeholder.hidden = false;
  elements.previewShell.dataset.state = "empty";
  elements.download.removeAttribute("href");
  elements.download.removeAttribute("download");
  elements.download.classList.add("disabled");
  elements.download.setAttribute("aria-disabled", "true");
  elements.download.setAttribute("tabindex", "-1");
  resetCopy();
  setProgress(0);
}

function setProcessing(processing) {
  elements.analyze.disabled = processing;
  elements.analyze.classList.toggle("is-loading", processing);
  elements.analyze.textContent = processing ? "分析中…" : "分析";
  elements.form.setAttribute("aria-busy", String(processing));
}

function setProgress(value) {
  const progress = Math.max(0, Math.min(Number(value) || 0, 100));
  elements.progress.value = progress;
  elements.progress.textContent = `${Math.round(progress)}%`;
  elements.progressValue.value = `${Math.round(progress)}%`;
  elements.progressValue.textContent = `${Math.round(progress)}%`;
}

function fail(message) {
  setProcessing(false);
  sessionStorage.removeItem(STORAGE_KEY);
  setProgress(0);
  elements.error.textContent = message;
  elements.error.hidden = false;
  elements.input.setAttribute("aria-invalid", "true");
  announce(message);
}

function clearError() {
  elements.error.hidden = true;
  elements.error.textContent = "";
  elements.input.removeAttribute("aria-invalid");
}

function announce(message) {
  elements.liveStatus.textContent = message;
}

function applyCopy(job) {
  const published = typeof job.copy?.published_text === "string" ? job.copy.published_text.trim() : "";
  const speech = typeof job.copy?.speech_text === "string" ? job.copy.speech_text.trim() : "";
  const speechStatus = job.copy?.speech_status || "pending";
  const audioStatus = job.audio?.status || "pending";
  const audioKind = job.audio?.kind || "unknown";
  const audioSummary = buildAudioSummary(job.audio);

  elements.publishedBlock.hidden = !published;
  elements.publishedText.textContent = published;

  elements.speechBlock.hidden = audioStatus === "not_requested" && speechStatus === "not_requested" && !speech;
  elements.audioSummary.textContent = audioSummary;
  elements.audioSummary.hidden = !audioSummary;
  elements.speechText.textContent = speech;
  elements.speechText.hidden = !speech;
  elements.speechCopy.hidden = !speech;

  if (speechStatus === "ready" && speech) {
    elements.speechState.textContent = audioStatus === "ready" ? (job.audio?.label || "讲话") : "已生成";
    elements.speechNotice.hidden = true;
    elements.speechNotice.textContent = "";
  } else if (speechStatus === "not_present") {
    elements.speechState.textContent = job.audio?.label || (audioKind === "no_audio" ? "无声音" : "无讲话");
    elements.speechNotice.hidden = true;
    elements.speechNotice.textContent = "";
  } else if (speechStatus === "not_requested") {
    elements.speechState.textContent = "未生成";
    elements.speechNotice.textContent = "这是旧任务，请重新分析以生成讲话文稿。";
    elements.speechNotice.hidden = false;
  } else if (speechStatus === "unavailable") {
    elements.speechState.textContent = audioStatus === "ready" ? (job.audio?.label || "未生成") : "未生成";
    elements.speechNotice.textContent = job.error_message || "检测到的讲话不够清晰，未生成文稿。";
    elements.speechNotice.hidden = false;
  } else {
    elements.speechState.textContent = audioStatus === "ready" ? (job.audio?.label || "提取中…") : "分析声音…";
    elements.speechNotice.textContent = audioStatus === "ready" ? "正在提取讲话文稿…" : "正在区分讲话和音乐/背景声…";
    elements.speechNotice.hidden = false;
  }

  const hasCopyArea = Boolean(published || speech || audioSummary || job.terminal);
  elements.copyPanel.hidden = !hasCopyArea;
  elements.copyEmpty.hidden = Boolean(published || speech || audioSummary || !job.terminal);
}

function buildAudioSummary(audio) {
  if (!audio || audio.status === "not_requested") return "";
  if (audio.status === "pending") return "正在分析声音类型…";
  if (audio.status === "unavailable") return "声音类型暂时无法可靠判定。";

  const summaries = {
    speech_only: "检测到清晰讲话。",
    speech_background: "检测到讲话，同时存在音乐或其他背景声。",
    non_speech: "检测到音乐或其他声音，未发现清晰的正常讲话。",
    no_audio: "没有检测到有效声音。",
    unknown: "声音类型暂时无法可靠判定。",
  };
  let result = summaries[audio.kind] || summaries.unknown;
  const title = typeof audio.music_title === "string" ? audio.music_title.trim() : "";
  const artists = Array.isArray(audio.music_artists) ? audio.music_artists.filter(Boolean).join("、") : "";
  if (title) result += ` 平台音乐：${title}${artists ? ` · ${artists}` : ""}。`;
  return result;
}

function resetCopy() {
  elements.copyPanel.hidden = true;
  elements.publishedBlock.hidden = true;
  elements.publishedText.textContent = "";
  elements.speechBlock.hidden = true;
  elements.speechBlock.open = false;
  elements.speechState.textContent = "";
  elements.audioSummary.textContent = "";
  elements.audioSummary.hidden = true;
  elements.speechText.textContent = "";
  elements.speechText.hidden = true;
  elements.speechCopy.hidden = true;
  elements.speechNotice.textContent = "";
  elements.speechNotice.hidden = true;
  elements.copyEmpty.hidden = true;
}

async function copyResult(source, button, label) {
  const text = source.textContent.trim();
  if (!text) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.setAttribute("readonly", "");
      helper.className = "clipboard-helper";
      document.body.appendChild(helper);
      helper.select();
      const copied = document.execCommand("copy");
      helper.remove();
      if (!copied) throw new Error("copy failed");
    }
    button.textContent = "已复制";
    announce(`${label}已复制`);
    window.setTimeout(() => { button.textContent = "复制"; }, 1600);
  } catch (_) {
    announce("复制失败，请长按或选中文本复制");
  }
}
