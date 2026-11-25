/* =========================================================
   ORBITBASE - SCRIPT.JS COMPLETO
   Fluxo novo:
   - sem upload manual de bases
   - seleção apenas por estações RBMC no mapa
   - ponto alvo pré-visualizado antes da criação do job
   ========================================================= */

//const API_BASE_URL = "https://84a4-191-6-124-131.ngrok-free.app";
// Local (Live Server / arquivo): API em 127.0.0.1:8000. Em produção: mesma origem, prefixo /api.
const API_BASE_URL = (["localhost", "127.0.0.1", ""].includes(window.location.hostname) ? "http://127.0.0.1:8000" : `${window.location.origin}/api`);

let orbitbaseMap = null;
let previewTargetMarker = null;
let previewTargetCircle = null;
let jobLayersGroup = null;
let rbmcStationLayerGroup = null;

let jobsCache = [];
let editingJobId = null;

let vrsProfilesCache = [];
let activeVrsProfile = null;

let gnssStationsCache = [];
let selectedGnssStations = new Set();

const processingModal = document.getElementById("processingModal");
const processingModalJobName = document.getElementById("processingModalJobName");
const processingModalBar = document.getElementById("processingModalBar");
const processingModalPercent = document.getElementById("processingModalPercent");
const processingModalMessage = document.getElementById("processingModalMessage");
const processingModalDetail = document.getElementById("processingModalDetail");
const processingModalCloseBtn = document.getElementById("processingModalCloseBtn");

/* =========================================================
   AUTH / PAGE HELPERS
   ========================================================= */

let processingPollTimer = null;
let processingActiveJobId = null;

function openProcessingModal(jobName = "") {
  const modal = document.getElementById("processingModal");
  const bar = document.getElementById("processingModalBar");
  const percent = document.getElementById("processingModalPercent");
  const message = document.getElementById("processingModalMessage");
  const detail = document.getElementById("processingModalDetail");
  const closeBtn = document.getElementById("processingModalCloseBtn");
  const jobNameEl = document.getElementById("processingModalJobName");

  if (!modal) return;

  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");

  if (jobNameEl) {
    jobNameEl.textContent = jobName ? `Job: ${jobName}` : "";
  }

  if (bar) bar.style.width = "2%";
  if (percent) percent.textContent = "2%";
  if (message) message.textContent = "Iniciando processamento...";
  if (detail) detail.textContent = "Preparando validação e processamento do job.";
  if (closeBtn) closeBtn.classList.add("hidden");
}

function updateProcessingModal(progress) {
  const bar = document.getElementById("processingModalBar");
  const percent = document.getElementById("processingModalPercent");
  const message = document.getElementById("processingModalMessage");
  const detail = document.getElementById("processingModalDetail");
  const closeBtn = document.getElementById("processingModalCloseBtn");

  const value = Number(progress?.percent ?? 0);
  const safePercent = Math.max(0, Math.min(100, value));

  if (bar) bar.style.width = `${safePercent}%`;
  if (percent) percent.textContent = `${safePercent}%`;
  if (message) message.textContent = progress?.message || "Processando...";
  if (detail) detail.textContent = progress?.detail || "";

  if (progress?.status === "completed" || progress?.status === "failed") {
    if (closeBtn) closeBtn.classList.remove("hidden");
  }
}

function closeProcessingModal() {
  const modal = document.getElementById("processingModal");
  if (modal) modal.classList.add("hidden");
  document.body.classList.remove("modal-open");

  stopProcessingPolling();
}

function stopProcessingPolling() {
  if (processingPollTimer) {
    clearInterval(processingPollTimer);
    processingPollTimer = null;
  }
  processingActiveJobId = null;
}

async function fetchJobProgress(jobId) {
  const token = localStorage.getItem("orbitbase_token");
  const response = await fetch(`${API_BASE_URL}/jobs/${jobId}/progress`, {
    method: "GET",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
    },
  });

  if (!response.ok) {
    throw new Error(`Falha ao consultar progresso do job ${jobId}.`);
  }

  return await response.json();
}

function startProcessingPolling(jobId) {
  stopProcessingPolling();
  processingActiveJobId = jobId;

  const run = async () => {
    try {
      const progress = await fetchJobProgress(jobId);
      updateProcessingModal(progress);

      if (progress?.status === "completed" || progress?.status === "failed") {
        stopProcessingPolling();
        await refreshJobsTableSafe();
      }
    } catch (error) {
      console.error("Erro ao consultar progresso:", error);
    }
  };

  run();
  processingPollTimer = setInterval(run, 1000);
}


function initDashboardModeToggle() {
  const toggleBtn = document.getElementById("toggle-dashboard-mode-btn");
  if (!toggleBtn) return;

  if (isAdvancedDashboardPage()) {
    toggleBtn.textContent = "Modo básico";
  } else {
    toggleBtn.textContent = "Modo avançado";
  }


  toggleBtn.addEventListener("click", () => {
    toggleDashboardMode();
  });
}

function finishProcessingModal(progress, isError = false) {
  updateProcessingModal(progress);

  if (isError) {
    processingModalMessage.textContent = progress?.message || "Falha no processamento";
  }

  processingModalCloseBtn.classList.remove("hidden");
}

processingModalCloseBtn?.addEventListener("click", async () => {
  closeProcessingModal();
  try {
    await refreshJobsTableSafe();
  } catch (error) {
    console.error("Erro ao atualizar jobs:", error);
  }
});




function getToken() {
  return localStorage.getItem("orbitbase_token");
}

function setToken(token) {
  localStorage.setItem("orbitbase_token", token);
}

function removeToken() {
  localStorage.removeItem("orbitbase_token");
}

const ORBITBASE_MODE_KEY = "orbitbase_mode";

function getCurrentMode() {
  return localStorage.getItem(ORBITBASE_MODE_KEY) || "basic";
}

function setCurrentMode(mode) {
  localStorage.setItem(
    ORBITBASE_MODE_KEY,
    mode === "advanced" ? "advanced" : "basic"
  );
}

function getDashboardUrlByMode(mode) {
  // Interface única: o login sempre leva ao painel principal. As telas
  // clássica (classic/dashboard.html) e avançada ficam acessíveis por URL.
  return "./dashboard.html";
}

function isAdvancedDashboardPage() {
  return window.location.pathname.includes("dashboard_advanced.html");
}

function isBasicDashboardPage() {
  return window.location.pathname.includes("dashboard.html") &&
         !window.location.pathname.includes("dashboard_advanced.html");
}

function isAnyDashboardPage() {
  return isBasicDashboardPage() || isAdvancedDashboardPage();
}

function goToCurrentModeDashboard() {
  window.location.href = getDashboardUrlByMode(getCurrentMode());
}

function toggleDashboardMode() {
  const nextMode = isAdvancedDashboardPage() ? "basic" : "advanced";
  setCurrentMode(nextMode);
  window.location.href = getDashboardUrlByMode(nextMode);
}

function isAuthPage() {
  return window.location.pathname.includes("auth.html");
}

function isDashboardPage() {
  return isBasicDashboardPage();
}

function renderProcessingProgress(progress) {
  let box = document.getElementById("processing-progress-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "processing-progress-box";
    box.style.margin = "16px 0";
    box.innerHTML = `
      <div style="font-weight:600; margin-bottom:8px;">Andamento do processamento</div>
      <div style="width:100%; height:18px; background:#e5e7eb; border-radius:999px; overflow:hidden;">
        <div id="processing-progress-fill" style="height:100%; width:0%; background:#2563eb; transition:width .35s;"></div>
      </div>
      <div id="processing-progress-text" style="margin-top:8px; font-size:14px;"></div>
    `;
    const anchor = document.querySelector(".jobs-section, #jobs-list, body");
    anchor.prepend(box);
  }

  const fill = document.getElementById("processing-progress-fill");
  const text = document.getElementById("processing-progress-text");

  const pct = Number(progress?.percent || 0);
  fill.style.width = `${pct}%`;
  text.textContent = `${pct}% — ${progress?.message || ""} ${progress?.detail ? " | " + progress.detail : ""}`;
}

/* =========================================================
   REQUESTS
   ========================================================= */

function authHeaders(extra = {}) {
  const token = getToken();

  return {
    "ngrok-skip-browser-warning": "1",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra
  };
}

let activeProgressPolls = new Map();

function stopJobProgressPolling(jobId) {
  const timerId = activeProgressPolls.get(jobId);
  if (timerId) {
    clearTimeout(timerId);
    activeProgressPolls.delete(jobId);
  }
}

function updateJobRowProgress(jobId, progress) {
  const row = document.querySelector(`tr[data-job-id="${jobId}"]`);
  if (!row) return;

  const fill = row.querySelector(".job-progress-fill");
  const text = row.querySelector(".job-progress-text");

  const pct = Number(progress?.percent || 0);
  const message = progress?.message || "";
  const detail = progress?.detail ? ` | ${progress.detail}` : "";

  if (fill) fill.style.width = `${pct}%`;
  if (text) text.textContent = `${pct}% — ${message}${detail}`;
}

async function pollJobProgress(jobId, onUpdate) {
  let keepPolling = true;

  while (keepPolling) {
    try {
      const response = await fetch(`${API_BASE_URL}/jobs/${jobId}/progress`, {
        headers: authHeaders(),
      });

      if (!response.ok) {
        throw new Error("Não foi possível consultar o progresso do job.");
      }

      const progress = await response.json();
      onUpdate(progress);

      const step = progress?.step;
      const status = progress?.status;

      if (
        step === "done" ||
        step === "error" ||
        status === "completed" ||
        status === "failed"
      ) {
        keepPolling = false;
        return progress;
      }

      await new Promise((resolve) => setTimeout(resolve, 1200));
    } catch (error) {
      onUpdate({
        percent: 100,
        step: "error",
        message: "Falha ao consultar progresso",
        detail: error.message || "Erro desconhecido.",
        status: "failed",
      });
      return {
        percent: 100,
        step: "error",
        message: "Falha ao consultar progresso",
        detail: error.message || "Erro desconhecido.",
        status: "failed",
      };
    }
  }
}



function renderProcessingProgress(progress) {
  let box = document.getElementById("processing-progress-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "processing-progress-box";
    box.style.margin = "16px 0";
    box.innerHTML = `
      <div style="font-weight:600; margin-bottom:8px;">Andamento do processamento</div>
      <div style="width:100%; height:18px; background:#e5e7eb; border-radius:999px; overflow:hidden;">
        <div id="processing-progress-fill" style="height:100%; width:0%; background:#2563eb; transition:width .35s;"></div>
      </div>
      <div id="processing-progress-text" style="margin-top:8px; font-size:14px;"></div>
    `;
    const anchor = document.querySelector(".jobs-section, #jobs-list, body");
    anchor.prepend(box);
  }

  const fill = document.getElementById("processing-progress-fill");
  const text = document.getElementById("processing-progress-text");

  const pct = Number(progress?.percent || 0);
  const message = progress?.message || "";
  const detail = progress?.detail ? ` | ${progress.detail}` : "";

  if (fill) {
    fill.style.width = `${pct}%`;
  }

  if (text) {
    text.textContent = `${pct}% — ${message}${detail}`;
  }
}


async function pollJobProgress(jobId) {
  stopJobProgressPolling(jobId);

  const run = async () => {
    try {
      const resp = await fetch(`${API_BASE_URL}/jobs/${jobId}/progress`, {
        headers: authHeaders()
      });

      if (!resp.ok) {
        throw new Error(`Falha ao consultar progresso do job ${jobId}.`);
      }

      const progress = await resp.json();

      renderProcessingProgress(progress);
      updateJobRowProgress(jobId, progress);

      const pct = Number(progress?.percent || 0);
      const step = progress?.step || "";

      if (pct < 100 && step !== "error" && step !== "done") {
        const timerId = setTimeout(run, 1500);
        activeProgressPolls.set(jobId, timerId);
      } else {
        stopJobProgressPolling(jobId);
      }
    } catch (err) {
      console.error("Erro ao consultar progresso:", err);
      const timerId = setTimeout(run, 2500);
      activeProgressPolls.set(jobId, timerId);
    }
  };

  await run();
}

async function apiRequest(endpoint, options = {}) {
  const headers = {
    ...(options.body instanceof FormData
      ? authHeaders(options.headers || {})
      : authHeaders({
          "Content-Type": "application/json",
          ...(options.headers || {})
        }))
  };

  const response = await fetch(`${API_BASE_URL}${endpoint}`, {
    ...options,
    headers
  });

  const contentType = response.headers.get("content-type") || "";
  let data = null;

  if (contentType.includes("application/json")) {
    data = await response.json();
  } else {
    data = await response.text();
  }

  if (!response.ok) {
    let errorMessage = "Não foi possível concluir a requisição.";

    if (data && typeof data === "object") {
      if (Array.isArray(data.detail)) {
        errorMessage = data.detail
          .map((item) => {
            const loc = Array.isArray(item.loc) ? item.loc.join(" > ") : "campo";
            return `${loc}: ${item.msg}`;
          })
          .join(" | ");
      } else {
        errorMessage = data.detail || errorMessage;
      }
    } else if (typeof data === "string" && data.trim()) {
      errorMessage = data;
    }

    const error = new Error(errorMessage);
    error.status = response.status;
    throw error;
  }

  return data;
}
/* =========================================================
   API WRAPPERS
   ========================================================= */

async function login(email, password) {
  const data = await apiRequest("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password })
  });

  setToken(data.access_token);
  return data;
}

async function fetchCurrentUser() {
  return await apiRequest("/users/me");
}

async function fetchUsage() {
  return await apiRequest("/users/me/usage");
}

async function fetchJobs() {
  return await apiRequest("/jobs");
}

async function createJob(payload) {
  return await apiRequest("/jobs", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

async function updateJob(jobId, payload) {
  return await apiRequest(`/jobs/${jobId}`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

async function validateNetwork(jobId) {
  return await apiRequest(`/jobs/${jobId}/validate-network`, {
    method: "POST"
  });
}

async function processRealJob(jobId) {
  return await apiRequest(`/jobs/${jobId}/process-real`, {
    method: "POST"
  });
}

async function fetchGnssStations(params = {}) {
  const qs = new URLSearchParams();

  if (params.state) qs.set("state", params.state);
  if (params.active_only !== undefined) qs.set("active_only", String(params.active_only));
  if (params.warning_only !== undefined) qs.set("warning_only", String(params.warning_only));
  if (params.rinex_1s_only !== undefined) qs.set("rinex_1s_only", String(params.rinex_1s_only));
  if (params.limit !== undefined) qs.set("limit", String(params.limit));

  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return await apiRequest(`/gnss-stations${suffix}`);
}

async function syncRbmcStations() {
  return await apiRequest("/gnss-stations/sync", {
    method: "POST"
  });
}

/* =========================================================
   UI MESSAGES
   ========================================================= */

function showMessage(elementId, message, isError = false) {
  const box = document.getElementById(elementId);
  if (!box) return;

  box.textContent = message;
  box.classList.remove("hidden", "message-success", "message-error");
  box.classList.add(isError ? "message-error" : "message-success");
}

function clearMessage(elementId) {
  const box = document.getElementById(elementId);
  if (!box) return;

  box.textContent = "";
  box.classList.add("hidden");
  box.classList.remove("message-success", "message-error");
}

/* =========================================================
   LABELS / FLOW CONTROL
   ========================================================= */

function getNetworkStatusLabel(job) {
  if (job.network_validation_status === "completed") return "Validada";
  if (job.network_validation_status === "failed") return "Falhou";
  if (job.network_validation_status === "running") return "Validando";
  return "Pendente";
}

function getProcessingStatusLabel(job) {
  if (job.processing_status === "completed") return "Concluído";
  if (job.processing_status === "failed") return "Falhou";
  if (job.processing_status === "running") return "Processando";
  return "Pendente";
}

function getGeneralStatusLabel(job) {
  if (job.status === "completed") return "Concluído";
  if (job.status === "failed") return "Falhou";
  if (job.status === "running") return "Em execução";
  if (job.status === "queued") return "Em fila";
  if (job.status === "draft") return "Rascunho";
  return job.status || "Pendente";
}

function getNextStepHint(job) {
  if (job.export_count > 0) return "Fluxo concluído.";
  if (job.processing_status === "completed") return "Pronto para exportação.";
  if (job.network_validation_status === "completed") return "Pronto para processamento.";
  return "Valide a rede para continuar.";
}

/* =========================================================
   FORM MODE / EDITION
   ========================================================= */

function getEditingJob() {
  if (!editingJobId) return null;
  return jobsCache.find((item) => item.id === editingJobId) || null;
}

function setSubmitButtonState(isEditing) {
  const submitBtn = document.getElementById("job-submit-btn");
  const cancelBtn = document.getElementById("job-cancel-edit-btn");
  const hiddenEditingId = document.getElementById("editing-job-id");

  if (submitBtn) {
    submitBtn.textContent = isEditing ? "Salvar alterações" : "Criar job";
  }

  if (cancelBtn) {
    cancelBtn.classList.toggle("hidden", !isEditing);
  }

  if (hiddenEditingId) {
    hiddenEditingId.value = isEditing && editingJobId ? String(editingJobId) : "";
  }
}

function fillJobForm(job) {
  const setValue = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.value = value ?? "";
  };

  setValue("job-name", job.name);
  setValue("target-lat", job.target_lat);
  setValue("target-lon", job.target_lon);
  setValue("target-height", job.target_height);
  setValue("output-rinex-name", job.output_rinex_name);
  setValue("output-marker-name", job.output_marker_name);
  setValue("session-date", job.session_date);
  setValue("start-time", job.start_time);
  setValue("end-time", job.end_time);
  setValue("occupation-minutes", job.occupation_minutes);
  setValue("job-minutes", job.requested_minutes);
  setValue("job-notes", job.notes);

  updateTargetPreviewFromForm(false);
}

function clearSelectedStations() {
  selectedGnssStations.clear();
  renderSelectedStationsInfo();
  renderGnssStationsOnMap();
}

function resetTargetPreview() {
  if (previewTargetMarker && orbitbaseMap) {
    try {
      orbitbaseMap.removeLayer(previewTargetMarker);
    } catch (_) {}
    previewTargetMarker = null;
  }

  if (previewTargetCircle && orbitbaseMap) {
    try {
      orbitbaseMap.removeLayer(previewTargetCircle);
    } catch (_) {}
    previewTargetCircle = null;
  }
}

function resetJobForm() {
  const form = document.getElementById("job-form");
  if (form) form.reset();

  const jobMinutes = document.getElementById("job-minutes");
  if (jobMinutes) jobMinutes.value = "45";

  editingJobId = null;
  setSubmitButtonState(false);
  clearSelectedStations();
  resetTargetPreview();
}

window.editJob = function (jobId) {
  const job = jobsCache.find((item) => item.id === jobId);
  if (!job) return;

  editingJobId = job.id;
  fillJobForm(job);
  setSubmitButtonState(true);
  clearMessage("job-message");

  clearSelectedStations();
  if (Array.isArray(job.selected_station_codes)) {
    job.selected_station_codes.forEach((code) => selectedGnssStations.add(code));
    renderSelectedStationsInfo();
    renderGnssStationsOnMap();
  }

  updateTargetPreviewFromForm(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
};

function initCancelEditButton() {
  const cancelBtn = document.getElementById("job-cancel-edit-btn");
  if (!cancelBtn) return;

  cancelBtn.addEventListener("click", () => {
    resetJobForm();
    clearMessage("job-message");
  });
}

function initClearStationsButton() {
  const clearBtn = document.getElementById("clear-selected-stations-btn");
  if (!clearBtn) return;

  clearBtn.addEventListener("click", () => {
    clearSelectedStations();
    clearMessage("job-message");
  });
}

/* =========================================================
   TABLE RENDER
   ========================================================= */

function renderJobs(jobs) {
  const tbody = document.getElementById("jobs-table-body");
  if (!tbody) return;

  jobsCache = jobs || [];

  if (!jobs.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="11">
          Nenhum job cadastrado ainda. Crie o primeiro job para iniciar a geração da rede VRS.
        </td>
      </tr>
    `;
    return;
  }
tbody.innerHTML = jobs
  .map((job) => {
    const progressPercent = Number(job.progress_percent ?? 0);
    const progressLabel = job.progress_label || "Aguardando criação";
    const generalStatusLabel = job.status_label || getGeneralStatusLabel(job);
    const nextStepHint = job.next_step_hint || getNextStepHint(job);

    const canProcess = !!job.can_process;
    const canExport = !!job.can_export;

    const selectedCodes = Array.isArray(job.selected_station_codes)
      ? job.selected_station_codes
      : [];

    const selectedCodesLabel = selectedCodes.length
      ? selectedCodes.join(", ")
      : "-";

    const sessionDate = job.session_date || "-";
    const startTime = job.start_time || "--:--";
    const endTime = job.end_time || "--:--";
    const periodLabel = `${sessionDate}<br><small style="color:#64748b;">${startTime} → ${endTime}</small>`;

    return `
      <tr data-job-id="${job.id}">
        <td>${job.id}</td>

        <td>
          <strong>${job.name}</strong><br />
          <small style="color:#64748b;">${nextStepHint}</small>
        </td>

        <td>${selectedCodesLabel}</td>

        <td>${periodLabel}</td>

        <td>
          <div class="progress-wrap">
            <div class="progress-bar">
              <div class="progress-fill job-progress-fill" style="width: ${progressPercent}%;"></div>
            </div>
            <small class="job-progress-text">${progressLabel}</small>
          </div>
        </td>

        <td>${generalStatusLabel}</td>

        <td>${job.generated_minutes ?? "-"}</td>

        <td>${job.export_count ?? 0}</td>

        <td class="jobs-actions-cell">
          <div class="jobs-actions-stack">
            <button
              class="btn btn-outline btn-sm"
              type="button"
              onclick="editJob(${job.id})"
              title="Editar este job"
            >
              Editar
            </button>

            <button
              class="btn btn-secondary btn-sm ${canProcess ? "" : "btn-disabled"}"
              type="button"
              onclick="${canProcess ? `handleProcessReal(${job.id})` : ""}"
              ${canProcess ? "" : "disabled"}
              title="Processa este job"
            >
              Processar
            </button>

            <button
              class="btn btn-primary btn-sm ${canExport ? "" : "btn-disabled"}"
              type="button"
              onclick="${canExport ? `handleExport(${job.id})` : ""}"
              ${canExport ? "" : "disabled"}
              title="Exporta o pacote final do job"
            >
              Exportar
            </button>
          </div>
        </td>
      </tr>
    `;
  })
  .join("");
}
/* =========================================================
   MAP
   ========================================================= */

function createMapIfNeeded() {
  const mapElement = document.getElementById("network-map");
  if (!mapElement) return null;

  if (!orbitbaseMap) {
    orbitbaseMap = L.map("network-map").setView([-15, -52], 4);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(orbitbaseMap);

    jobLayersGroup = L.layerGroup().addTo(orbitbaseMap);
    rbmcStationLayerGroup = L.layerGroup().addTo(orbitbaseMap);
  }

  return orbitbaseMap;
}

function clearJobMapLayers() {
  if (jobLayersGroup) {
    jobLayersGroup.clearLayers();
  }
}

function renderGnssStationsOnMap() {
  const map = createMapIfNeeded();
  if (!map) return;

  if (rbmcStationLayerGroup) {
    rbmcStationLayerGroup.clearLayers();
  } else {
    rbmcStationLayerGroup = L.layerGroup().addTo(map);
  }

  gnssStationsCache.forEach((station) => {
    if (station.latitude == null || station.longitude == null) return;

    const isSelected = selectedGnssStations.has(station.station_code);

    const marker = L.circleMarker([station.latitude, station.longitude], {
      radius: isSelected ? 8 : 5,
      weight: 2,
      color: isSelected ? "#1d4ed8" : station.is_active ? "#15803d" : "#6b7280",
      fillColor: isSelected ? "#3b82f6" : station.is_active ? "#22c55e" : "#9ca3af",
      fillOpacity: 0.9
    });

    marker.bindPopup(`
      <strong>${station.station_code}</strong><br>
      ${station.station_name || "-"}<br>
      ${station.city || "-"} / ${station.state || "-"}<br>
      Ativa: ${station.is_active ? "sim" : "não"}<br>
      1s: ${station.supports_rinex_1s ? "sim" : "não"}<br>
      15s: ${station.supports_rinex_15s ? "sim" : "não"}<br><br>
      <button type="button" onclick="toggleGnssStationSelection('${station.station_code}')">
        ${isSelected ? "Remover" : "Selecionar"}
      </button>
    `);

    rbmcStationLayerGroup.addLayer(marker);
  });

  if (previewTargetMarker) {
    previewTargetMarker.bringToFront();
  }
}

function distanceKm(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 6371.0088 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function formatKm(km) {
  return km.toLocaleString("pt-BR", { maximumFractionDigits: km < 100 ? 1 : 0 }) + " km";
}

function renderSelectedStationsInfo() {
  const box = document.getElementById("selected-stations-summary");
  if (!box) return;

  const target = getTargetFromForm();
  const selected = gnssStationsCache
    .filter((s) => selectedGnssStations.has(s.station_code))
    .map((s) => ({
      station: s,
      km:
        target && s.latitude != null && s.longitude != null
          ? distanceKm(target.lat, target.lon, s.latitude, s.longitude)
          : null
    }));

  if (!selected.length) {
    box.innerHTML = "Nenhuma estação RBMC selecionada.";
    return;
  }

  if (target) selected.sort((a, b) => (a.km ?? Infinity) - (b.km ?? Infinity));

  box.innerHTML = `
    <strong>Selecionadas (${selected.length})</strong><br>
    ${selected
      .map(({ station: s, km }) =>
        `${s.station_code} - ${s.station_name || "-"}${km != null ? ` <small>(${formatKm(km)})</small>` : ""}`
      )
      .join("<br>")}
  `;
}

const NEAREST_COUNT_KEY = "orbitbase.nearestCount";

function restoreNearestCount() {
  const select = document.getElementById("nearest-count");
  if (!select) return;
  try {
    const saved = localStorage.getItem(NEAREST_COUNT_KEY);
    if (saved && select.querySelector(`option[value="${saved}"]`)) select.value = saved;
  } catch (_) { /* sem localStorage: fica o padrão */ }
}

function selectNearestActiveStations() {
  clearMessage("rbmc-sync-message");

  const target = getTargetFromForm();
  if (!target) {
    showMessage(
      "rbmc-sync-message",
      "Informe primeiro a latitude, a longitude e a altura do ponto da base.",
      true
    );
    return;
  }

  const count = Math.min(10, Math.max(1, Number(document.getElementById("nearest-count")?.value) || 4));

  const nearest = gnssStationsCache
    .filter((s) => s.is_active && s.latitude != null && s.longitude != null)
    .map((s) => ({ station: s, km: distanceKm(target.lat, target.lon, s.latitude, s.longitude) }))
    .sort((a, b) => a.km - b.km)
    .slice(0, count);

  if (!nearest.length) {
    showMessage("rbmc-sync-message", "Nenhuma estação RBMC ativa encontrada. Use \"Atualizar estações\".", true);
    return;
  }

  selectedGnssStations.clear();
  nearest.forEach(({ station }) => selectedGnssStations.add(station.station_code));
  renderSelectedStationsInfo();
  renderGnssStationsOnMap();

  if (orbitbaseMap) {
    const bounds = L.latLngBounds([[target.lat, target.lon]]);
    nearest.forEach(({ station }) => bounds.extend([station.latitude, station.longitude]));
    orbitbaseMap.fitBounds(bounds, { padding: [40, 40], maxZoom: 12 });
  }

  if (nearest.length < count) {
    showMessage(
      "rbmc-sync-message",
      `Só há ${nearest.length} estação(ões) ativa(s) com coordenadas; todas foram selecionadas.`,
      true
    );
  }
}

function initSelectNearestButton() {
  const btn = document.getElementById("select-nearest-btn");
  const select = document.getElementById("nearest-count");
  if (!btn || !select) return;

  restoreNearestCount();

  select.addEventListener("change", () => {
    try { localStorage.setItem(NEAREST_COUNT_KEY, select.value); } catch (_) { /* ignora */ }
  });

  // o seletor fica dentro do #job-form: o reset do formulário volta para o padrão,
  // então reaplica a última escolha do usuário logo depois.
  const form = select.form;
  if (form) form.addEventListener("reset", () => setTimeout(restoreNearestCount, 0));

  btn.addEventListener("click", selectNearestActiveStations);
}

function calculateJobMinutesAutomatically() {
  const startInput = document.getElementById("start-time");
  const endInput = document.getElementById("end-time");
  const occupationInput = document.getElementById("occupation-minutes");
  const jobMinutesInput = document.getElementById("job-minutes");

  if (!startInput || !endInput || !occupationInput || !jobMinutesInput) return;

  const start = (startInput.value || "").trim();
  const end = (endInput.value || "").trim();

  if (!start || !end) {
    occupationInput.value = "";
    jobMinutesInput.value = "";
    return;
  }

  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);

  if (
    Number.isNaN(sh) || Number.isNaN(sm) ||
    Number.isNaN(eh) || Number.isNaN(em)
  ) {
    occupationInput.value = "";
    jobMinutesInput.value = "";
    return;
  }

  let startMinutes = sh * 60 + sm;
  let endMinutes = eh * 60 + em;

  if (endMinutes < startMinutes) {
    endMinutes += 24 * 60;
  }

  const total = endMinutes - startMinutes;

  if (total <= 0) {
    occupationInput.value = "";
    jobMinutesInput.value = "";
    return;
  }

  occupationInput.value = total;
  jobMinutesInput.value = total;
}

window.toggleGnssStationSelection = function (stationCode) {
  if (selectedGnssStations.has(stationCode)) {
    selectedGnssStations.delete(stationCode);
  } else {
    selectedGnssStations.add(stationCode);
  }

  renderSelectedStationsInfo();
  renderGnssStationsOnMap();
};

function getTargetFromForm() {
  const latRaw = document.getElementById("target-lat")?.value?.trim() || "";
  const lonRaw = document.getElementById("target-lon")?.value?.trim() || "";
  const hRaw = document.getElementById("target-height")?.value?.trim() || "";

  const lat = latRaw !== "" ? Number(latRaw) : null;
  const lon = lonRaw !== "" ? Number(lonRaw) : null;
  const h = hRaw !== "" ? Number(hRaw) : null;

  if (
    lat == null || lon == null || h == null ||
    Number.isNaN(lat) || Number.isNaN(lon) || Number.isNaN(h)
  ) {
    return null;
  }

  return { lat, lon, h };
}

function updateTargetPreviewFromForm(fitBounds = false) {
  const map = createMapIfNeeded();
  if (!map) return;

  const target = getTargetFromForm();

  resetTargetPreview();
  renderSelectedStationsInfo();

  if (!target) {
    renderNetworkMap(null, jobsCache);
    return;
  }

  previewTargetMarker = L.circleMarker([target.lat, target.lon], {
    radius: 9,
    weight: 2,
    color: "#b91c1c",
    fillColor: "#ef4444",
    fillOpacity: 0.9
  })
    .bindPopup(`
      <strong>Alvo VRS (pré-visualização)</strong><br>
      Lat: ${target.lat}<br>
      Lon: ${target.lon}<br>
      Alt.: ${target.h} m
    `)
    .addTo(map);

  previewTargetCircle = L.circle([target.lat, target.lon], {
    radius: 3000,
    weight: 1,
    color: "#f87171",
    fillColor: "#fecaca",
    fillOpacity: 0.15
  }).addTo(map);

  renderGnssStationsOnMap();

  if (fitBounds) {
    const bounds = [[target.lat, target.lon]];
    gnssStationsCache
      .filter((s) => selectedGnssStations.has(s.station_code) && s.latitude != null && s.longitude != null)
      .forEach((s) => bounds.push([s.latitude, s.longitude]));

    if (bounds.length > 1) {
      map.fitBounds(bounds, { padding: [40, 40] });
    } else {
      map.setView([target.lat, target.lon], 11);
    }
  }
}

function renderNetworkMap(user, jobs) {
  const map = createMapIfNeeded();
  if (!map) return;

  clearJobMapLayers();
  renderGnssStationsOnMap();

  if (previewTargetMarker) {
    setTimeout(() => map.invalidateSize(), 100);
    return;
  }

  const latestJobWithCoords = jobs.find(
    (job) =>
      job.target_lat !== null &&
      job.target_lat !== undefined &&
      job.target_lon !== null &&
      job.target_lon !== undefined
  );

  if (!latestJobWithCoords) {
    map.setView([-15, -52], 4);
    setTimeout(() => map.invalidateSize(), 100);
    return;
  }

  const bounds = [];

  const vrsLat = Number(latestJobWithCoords.target_lat);
  const vrsLon = Number(latestJobWithCoords.target_lon);

  const vrsMarker = L.circleMarker([vrsLat, vrsLon], {
    radius: 8,
    weight: 2,
    color: "#dc2626",
    fillColor: "#ef4444",
    fillOpacity: 0.85
  })
    .bindPopup(`
      <strong>VRS</strong><br>
      Job: ${latestJobWithCoords.name}<br>
      Lat: ${vrsLat}<br>
      Lon: ${vrsLon}
    `)
    .addTo(jobLayersGroup);

  bounds.push([vrsLat, vrsLon]);

  const bases = [
    {
      name: "Base 1",
      lat: latestJobWithCoords.base_1_lat,
      lon: latestJobWithCoords.base_1_lon
    },
    {
      name: "Base 2",
      lat: latestJobWithCoords.base_2_lat,
      lon: latestJobWithCoords.base_2_lon
    },
    {
      name: "Base 3",
      lat: latestJobWithCoords.base_3_lat,
      lon: latestJobWithCoords.base_3_lon
    }
  ];

  bases.forEach((base) => {
    if (
      base.lat !== undefined &&
      base.lat !== null &&
      base.lon !== undefined &&
      base.lon !== null
    ) {
      const lat = Number(base.lat);
      const lon = Number(base.lon);

      L.circleMarker([lat, lon], {
        radius: 7,
        weight: 2,
        color: "#15803d",
        fillColor: "#22c55e",
        fillOpacity: 0.85
      })
        .bindPopup(`
          <strong>${base.name}</strong><br>
          Lat: ${lat}<br>
          Lon: ${lon}
        `)
        .addTo(jobLayersGroup);

      L.polyline(
        [
          [lat, lon],
          [vrsLat, vrsLon]
        ],
        {
          color: "#16a34a",
          weight: 2
        }
      ).addTo(jobLayersGroup);

      bounds.push([lat, lon]);
    }
  });

  if (bounds.length === 1) {
    map.setView(bounds[0], 12);
  } else if (bounds.length > 1) {
    map.fitBounds(bounds, { padding: [30, 30] });
  } else {
    map.setView([-15, -52], 4);
  }

  setTimeout(() => {
    map.invalidateSize();
  }, 100);
}

/* =========================================================
   DASHBOARD LOAD
   ========================================================= */

async function loadDashboard() {
  try {
    const [user, usage, jobs, stations] = await Promise.all([
      fetchCurrentUser(),
      fetchUsage(),
      fetchJobs(),
      fetchGnssStations({ active_only: true, limit: 2000 })
    ]);

    gnssStationsCache = Array.isArray(stations) ? stations : [];
    jobsCache = Array.isArray(jobs) ? jobs : [];

    const userName = document.getElementById("user-name");
    const userEmail = document.getElementById("user-email");
    const userPlan = document.getElementById("user-plan");
    const userNameNav = document.getElementById("user-name-nav");

    const usageMonth = document.getElementById("usage-month");
    const includedMinutes = document.getElementById("included-minutes");
    const usedMinutes = document.getElementById("used-minutes");
    const includedExports = document.getElementById("included-exports");
    const usedExports = document.getElementById("used-exports");

    if (userName) userName.textContent = user.name || "--";
    if (userEmail) userEmail.textContent = user.email || "--";
    if (userPlan) userPlan.textContent = user.plan || "--";
    if (userNameNav) userNameNav.textContent = user.name || "Usuário";

    if (usageMonth) usageMonth.textContent = usage.ref_month || "--";
    if (includedMinutes) includedMinutes.textContent = usage.included_minutes ?? "--";
    if (usedMinutes) usedMinutes.textContent = usage.used_minutes ?? "--";
    if (includedExports) includedExports.textContent = usage.included_exports ?? "--";
    if (usedExports) usedExports.textContent = usage.used_exports ?? "--";

    renderSelectedStationsInfo();
    renderJobs(jobsCache);
    renderNetworkMap(user, jobsCache);
    updateTargetPreviewFromForm(false);
  } catch (error) {
    console.error("Erro ao carregar dashboard:", error);

    if (error.status === 401 || error.status === 403) {
      removeToken();
      alert("Sua sessão expirou. Faça login novamente para continuar.");
      window.location.href = "./auth.html";
      return;
    }

    showMessage(
      "job-message",
      `Erro ao carregar o dashboard: ${error.message || "falha interna do servidor."}`,
      true
    );
  }
}

/* =========================================================
   JOB ACTIONS
   ========================================================= */

window.handleValidateNetwork = async function (jobId) {
  clearMessage("job-message");

  try {
    const result = await validateNetwork(jobId);
    showMessage(
      "job-message",
      result.network_validation_summary ||
        "Rede validada com sucesso. O próximo passo é processar o job."
    );
    await loadDashboard();
  } catch (error) {
    showMessage(
      "job-message",
      error.message || "Não foi possível validar a rede.",
      true
    );
  }
};

async function refreshJobsTableSafe() {
  // loadDashboard busca e RENDERIZA a tabela (fetchJobs só busca os dados),
  // assim o botão Exportar habilita sem precisar recarregar a página.
  if (typeof loadDashboard === "function") {
    return await loadDashboard();
  }

  if (typeof loadJobs === "function") {
    return await loadJobs();
  }

  if (typeof fetchJobs === "function") {
    return await fetchJobs();
  }

  if (typeof loadJobsTable === "function") {
    return await loadJobsTable();
  }

  if (typeof loadDashboardData === "function") {
    return await loadDashboardData();
  }

  if (typeof initDashboard === "function") {
    return await initDashboard();
  }

  console.warn("Nenhuma função de recarga da tabela de jobs foi encontrada.");
}




async function handleProcessReal(jobId, jobName = "") {
  const token = localStorage.getItem("orbitbase_token");
  openProcessingModal(jobName);
  startProcessingPolling(jobId);

  try {
    const response = await fetch(`${API_BASE_URL}/jobs/${jobId}/process-real`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      },
    });

    let payload = {};
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }

    if (!response.ok) {
      updateProcessingModal({
        percent: 100,
        status: "failed",
        message: "Falha no processamento",
        detail: payload?.detail || payload?.message || "Erro inesperado no processamento.",
      });
      stopProcessingPolling();
      await refreshJobsTableSafe();
      return;
    }

    // não força 100% aqui
    // quem manda no andamento agora é o /progress
    // apenas garante uma última atualização caso o backend finalize antes do próximo polling
    try {
      const finalProgress = await fetchJobProgress(jobId);
      updateProcessingModal(finalProgress);
    } catch (_) {}

    await refreshJobsTableSafe();

  } catch (error) {
    updateProcessingModal({
      percent: 100,
      status: "failed",
      message: "Falha no processamento",
      detail: error?.message || "Erro inesperado no frontend.",
    });
    stopProcessingPolling();
    await refreshJobsTableSafe();
  }
}
window.handleExport = async function (jobId) {
  clearMessage("job-message");

  try {
    const token = getToken();
    const response = await fetch(`${API_BASE_URL}/jobs/${jobId}/export`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "ngrok-skip-browser-warning": "1"
      }
    });

    if (!response.ok) {
      let errorMessage = "Não foi possível exportar o pacote do job.";
      try {
        const data = await response.json();
        errorMessage = data.detail || errorMessage;
      } catch (_) {}
      throw new Error(errorMessage);
    }

    const blob = await response.blob();
    const downloadUrl = window.URL.createObjectURL(blob);

    const disposition = response.headers.get("Content-Disposition");
    let filename = `job_${jobId}_export.zip`;

    if (disposition) {
      const match = disposition.match(/filename="?([^"]+)"?/);
      if (match && match[1]) {
        filename = match[1];
      }
    }

    const a = document.createElement("a");
    a.href = downloadUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();

    window.URL.revokeObjectURL(downloadUrl);

    showMessage(
      "job-message",
      "Pacote ZIP exportado com sucesso. O fluxo deste job foi concluído."
    );
    await loadDashboard();
  } catch (error) {
    showMessage(
      "job-message",
      error.message || "Falha ao exportar o pacote do job.",
      true
    );
  }
};

async function handleSyncRbmcStations() {
  clearMessage("rbmc-sync-message");

  const syncButton = document.getElementById("sync-rbmc-btn");
  const originalText = syncButton ? syncButton.textContent : "Atualizar estações RBMC";

  try {
    if (syncButton) {
      syncButton.disabled = true;
      syncButton.textContent = "Sincronizando...";
    }

    showMessage(
      "rbmc-sync-message",
      "Sincronizando estações RBMC. Aguarde..."
    );

    const result = await syncRbmcStations();

    const reportsFound = result?.reports_found ?? 0;
    const inserted = result?.inserted ?? 0;
    const updated = result?.updated ?? 0;
    const failed = result?.failed ?? 0;

    let finalMessage =
      `Sincronização concluída. Relatórios encontrados: ${reportsFound}. ` +
      `Inseridas: ${inserted}. Atualizadas: ${updated}. Falhas: ${failed}.`;

    if (failed > 0 && Array.isArray(result.errors) && result.errors.length > 0) {
      const firstError = result.errors[0];
      const errorText =
        firstError?.error || firstError?.file_name || "Erro não detalhado.";
      finalMessage += ` Primeiro erro: ${errorText}`;
    }

    showMessage("rbmc-sync-message", finalMessage, failed > 0);
    await loadDashboard();
  } catch (error) {
    showMessage(
      "rbmc-sync-message",
      error.message || "Não foi possível atualizar a lista de estações RBMC.",
      true
    );
  } finally {
    if (syncButton) {
      syncButton.disabled = false;
      syncButton.textContent = originalText;
    }
  }
}

/* =========================================================
   AUTH PAGE
   ========================================================= */

function initAuthPage() {
  const loginForm = document.getElementById("login-form");

if (getToken()) {
  goToCurrentModeDashboard();
  return;
}

  if (loginForm) {
    loginForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearMessage("auth-message");

      const email = document.getElementById("login-email").value.trim();
      const password = document.getElementById("login-password").value;

      try {
        await login(email, password);
        showMessage("auth-message", "Login realizado com sucesso.");
        goToCurrentModeDashboard();
      } catch (error) {
        showMessage(
          "auth-message",
          error.message || "Não foi possível realizar o login.",
          true
        );
      }
    });
  }

  const googleButtons = document.querySelectorAll(".google-soon");
  googleButtons.forEach((button) => {
    button.addEventListener("click", () => {
      showMessage(
        "auth-message",
        "O login com Google será disponibilizado em breve.",
        true
      );
    });
  });
}

/* =========================================================
   JOB FORM HELPERS
   ========================================================= */

function validateJobFormFields() {
  const name = document.getElementById("job-name")?.value.trim();
  const target = getTargetFromForm();
  const sessionDate = document.getElementById("session-date")?.value;
  const startTime = document.getElementById("start-time")?.value;
  const endTime = document.getElementById("end-time")?.value;
  const occupationMinutes = document.getElementById("occupation-minutes")?.value;
  const requestedMinutes = document.getElementById("job-minutes")?.value;

  if (!name) throw new Error("Informe o nome do job.");
  if (!target) throw new Error("Informe latitude, longitude e altitude válidas da VRS.");
  if (!sessionDate) throw new Error("Informe a data da sessão.");
  if (!startTime) throw new Error("Informe a hora inicial.");
  if (!endTime) throw new Error("Informe a hora final.");
  if (!occupationMinutes) throw new Error("Informe o tempo de ocupação.");
  if (!requestedMinutes) throw new Error("Informe os minutos solicitados.");

  if (Number(requestedMinutes) <= 0) {
    throw new Error("Os minutos solicitados devem ser maiores que zero.");
  }

  if (Number(occupationMinutes) <= 0) {
    throw new Error("O tempo de ocupação deve ser maior que zero.");
  }

  const start = new Date(`${sessionDate}T${startTime}:00`);
  const end = new Date(`${sessionDate}T${endTime}:00`);

  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    throw new Error("Data/hora inválida.");
  }

  const diffMinutes = Math.round((end.getTime() - start.getTime()) / 60000);

  if (diffMinutes <= 0) {
    throw new Error("A hora final deve ser maior que a hora inicial.");
  }

  if (Number(occupationMinutes) > diffMinutes) {
    throw new Error("A janela informada é menor que o tempo de ocupação solicitado.");
  }

  if (Number(requestedMinutes) > diffMinutes) {
    throw new Error("Os minutos solicitados são maiores que a janela informada.");
  }
}

function validateSelectedStations() {
  if (selectedGnssStations.size < 3) {
    throw new Error("Selecione no mínimo 3 estações RBMC no mapa.");
  }
}

function bindTargetPreviewEvents() {
  const ids = ["target-lat", "target-lon", "target-height"];
  ids.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", () => updateTargetPreviewFromForm(false));
    el.addEventListener("change", () => updateTargetPreviewFromForm(true));
  });
}

/* =========================================================
   VRS PROFILES MODAL
   ========================================================= */

function openVrsConfigModal() {
  document.getElementById("vrsConfigModal")?.classList.remove("hidden");
}

function closeVrsConfigModal() {
  document.getElementById("vrsConfigModal")?.classList.add("hidden");
}

function setVrsFeedback(message, isError = false) {
  const el = document.getElementById("vrsConfigFeedback");
  if (!el) return;
  el.textContent = message;
  el.style.color = isError ? "#b91c1c" : "#065f46";
}

function clearVrsFeedback() {
  setVrsFeedback("");
}

function buildConfigFromForm() {
  return {
    min_bases_per_observation: Number(document.getElementById("cfgMinBasesPerObservation").value),
    min_satellites_per_epoch: Number(document.getElementById("cfgMinSatellitesPerEpoch").value),
    max_satellites_per_epoch: Number(document.getElementById("cfgMaxSatellitesPerEpoch").value),
    min_satellite_presence_ratio: Number(document.getElementById("cfgMinSatellitePresenceRatio").value),
    min_satellite_longest_arc: Number(document.getElementById("cfgMinSatelliteLongestArc").value),
    local_continuity_window: Number(document.getElementById("cfgLocalContinuityWindow").value),
    min_valid_in_local_window: Number(document.getElementById("cfgMinValidInLocalWindow").value),
    prefer_nearest_base: document.getElementById("cfgPreferNearestBase").checked,
    strong_nearest_base_bias: document.getElementById("cfgStrongNearestBaseBias").checked,
    require_dual_frequency: document.getElementById("cfgRequireDualFrequency").checked,
    preserve_full_user_window: document.getElementById("cfgPreserveFullUserWindow").checked,
    generate_ppp_aux_file: document.getElementById("cfgGeneratePppAuxFile").checked,
    priority_obs: document.getElementById("cfgPriorityObs").value
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean),
    ppp_trials: [
      {
        strategy: document.getElementById("trial1Strategy").value,
        min_consecutive_epochs: Number(document.getElementById("trial1MinConsecutiveEpochs").value),
        min_satellites_per_epoch: Number(document.getElementById("trial1MinSatellitesPerEpoch").value),
        min_priority_obs_per_satellite_avg: Number(document.getElementById("trial1MinPriorityObs").value),
        max_gap_factor: Number(document.getElementById("trial1MaxGapFactor").value)
      },
      {
        strategy: document.getElementById("trial2Strategy").value,
        min_consecutive_epochs: Number(document.getElementById("trial2MinConsecutiveEpochs").value),
        min_satellites_per_epoch: Number(document.getElementById("trial2MinSatellitesPerEpoch").value),
        min_priority_obs_per_satellite_avg: Number(document.getElementById("trial2MinPriorityObs").value),
        max_gap_factor: Number(document.getElementById("trial2MaxGapFactor").value)
      },
      {
        strategy: document.getElementById("trial3Strategy").value,
        min_consecutive_epochs: Number(document.getElementById("trial3MinConsecutiveEpochs").value),
        min_satellites_per_epoch: Number(document.getElementById("trial3MinSatellitesPerEpoch").value),
        min_priority_obs_per_satellite_avg: Number(document.getElementById("trial3MinPriorityObs").value),
        max_gap_factor: Number(document.getElementById("trial3MaxGapFactor").value)
      }
    ]
  };
}

function fillFormWithConfig(config = {}) {
  document.getElementById("cfgMinBasesPerObservation").value = config.min_bases_per_observation ?? 2;
  document.getElementById("cfgMinSatellitesPerEpoch").value = config.min_satellites_per_epoch ?? 5;
  document.getElementById("cfgMaxSatellitesPerEpoch").value = config.max_satellites_per_epoch ?? 10;
  document.getElementById("cfgMinSatellitePresenceRatio").value = config.min_satellite_presence_ratio ?? 0.45;
  document.getElementById("cfgMinSatelliteLongestArc").value = config.min_satellite_longest_arc ?? 80;
  document.getElementById("cfgLocalContinuityWindow").value = config.local_continuity_window ?? 2;
  document.getElementById("cfgMinValidInLocalWindow").value = config.min_valid_in_local_window ?? 4;
  document.getElementById("cfgPreferNearestBase").checked = !!config.prefer_nearest_base;
  document.getElementById("cfgStrongNearestBaseBias").checked = !!config.strong_nearest_base_bias;
  document.getElementById("cfgRequireDualFrequency").checked = !!config.require_dual_frequency;
  document.getElementById("cfgPreserveFullUserWindow").checked = !!config.preserve_full_user_window;
  document.getElementById("cfgGeneratePppAuxFile").checked = !!config.generate_ppp_aux_file;
  document.getElementById("cfgPriorityObs").value = (config.priority_obs || []).join(",");

  const trials = config.ppp_trials || [];

  const fillTrial = (idx, trial) => {
    document.getElementById(`trial${idx}Strategy`).value = trial?.strategy ?? "";
    document.getElementById(`trial${idx}MinConsecutiveEpochs`).value = trial?.min_consecutive_epochs ?? "";
    document.getElementById(`trial${idx}MinSatellitesPerEpoch`).value = trial?.min_satellites_per_epoch ?? "";
    document.getElementById(`trial${idx}MinPriorityObs`).value = trial?.min_priority_obs_per_satellite_avg ?? "";
    document.getElementById(`trial${idx}MaxGapFactor`).value = trial?.max_gap_factor ?? "";
  };

  fillTrial(1, trials[0]);
  fillTrial(2, trials[1]);
  fillTrial(3, trials[2]);
}

function fillFormWithProfile(profile) {
  if (!profile) return;
  document.getElementById("vrsProfileName").value = profile.name || "";
  document.getElementById("vrsProfileDescription").value = profile.description || "";
  fillFormWithConfig(profile.config || {});
}

function clearVrsProfileForm() {
  document.getElementById("vrsProfileName").value = "";
  document.getElementById("vrsProfileDescription").value = "";
  fillFormWithConfig({
    min_bases_per_observation: 2,
    min_satellites_per_epoch: 5,
    max_satellites_per_epoch: 10,
    min_satellite_presence_ratio: 0.45,
    min_satellite_longest_arc: 80,
    local_continuity_window: 2,
    min_valid_in_local_window: 4,
    prefer_nearest_base: true,
    strong_nearest_base_bias: true,
    require_dual_frequency: true,
    preserve_full_user_window: true,
    generate_ppp_aux_file: true,
    priority_obs: ["P1", "C1", "L1", "P2", "C2", "L2", "C5", "S1", "S2", "S5"],
    ppp_trials: [
      {
        strategy: "strict",
        min_consecutive_epochs: 120,
        min_satellites_per_epoch: 8,
        min_priority_obs_per_satellite_avg: 4.0,
        max_gap_factor: 2.0
      },
      {
        strategy: "balanced",
        min_consecutive_epochs: 60,
        min_satellites_per_epoch: 6,
        min_priority_obs_per_satellite_avg: 3.0,
        max_gap_factor: 2.5
      },
      {
        strategy: "short_rover",
        min_consecutive_epochs: 20,
        min_satellites_per_epoch: 4,
        min_priority_obs_per_satellite_avg: 2.0,
        max_gap_factor: 3.0
      }
    ]
  });
}

function renderActiveProfile(profile) {
  const el = document.getElementById("activeVrsProfileInfo");
  if (!el) return;

  if (!profile) {
    el.innerHTML = "Nenhum perfil ativo encontrado.";
    return;
  }

  el.innerHTML = `
    <strong>${profile.name || "Sem nome"}</strong><br>
    <span>${profile.description || "Sem descrição"}</span><br>
    <small>ID: ${profile.id} | Sistema: ${profile.is_system ? "sim" : "não"} | Ativo: ${profile.is_active ? "sim" : "não"}</small>
  `;
}

function renderProfilesSelect(profiles) {
  const select = document.getElementById("vrsProfilesSelect");
  if (!select) return;

  select.innerHTML = "";

  profiles.forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = `${profile.name}${profile.is_active ? " (ativo)" : ""}`;
    select.appendChild(option);
  });

  if (activeVrsProfile?.id) {
    select.value = String(activeVrsProfile.id);
  }
}

async function loadActiveVrsProfile() {
  activeVrsProfile = await apiRequest("/vrs-profiles/active");
  renderActiveProfile(activeVrsProfile);
}

async function loadVrsProfiles() {
  vrsProfilesCache = await apiRequest("/vrs-profiles");
  renderProfilesSelect(vrsProfilesCache);
}

function getSelectedProfileFromSelect() {
  const select = document.getElementById("vrsProfilesSelect");
  if (!select) return null;
  const id = Number(select.value);
  return vrsProfilesCache.find((p) => Number(p.id) === id) || null;
}

async function refreshVrsProfilesAndActive() {
  await loadActiveVrsProfile();
  await loadVrsProfiles();
}

async function saveNewVrsProfile() {
  clearVrsFeedback();

  const payload = {
    name: document.getElementById("vrsProfileName").value.trim(),
    description: document.getElementById("vrsProfileDescription").value.trim(),
    config: buildConfigFromForm(),
    activate: false
  };

  if (!payload.name) {
    setVrsFeedback("Informe o nome do perfil.", true);
    return;
  }

  await apiRequest("/vrs-profiles", {
    method: "POST",
    body: JSON.stringify(payload)
  });

  setVrsFeedback("Perfil criado com sucesso.");
  await refreshVrsProfilesAndActive();
}

async function updateSelectedVrsProfile() {
  clearVrsFeedback();

  const selected = getSelectedProfileFromSelect();
  if (!selected) {
    setVrsFeedback("Selecione um perfil para atualizar.", true);
    return;
  }

  const payload = {
    name: document.getElementById("vrsProfileName").value.trim(),
    description: document.getElementById("vrsProfileDescription").value.trim(),
    config: buildConfigFromForm()
  };

  await apiRequest(`/vrs-profiles/${selected.id}`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });

  setVrsFeedback("Perfil atualizado com sucesso.");
  await refreshVrsProfilesAndActive();
}

async function activateSelectedVrsProfile() {
  clearVrsFeedback();

  const selected = getSelectedProfileFromSelect();
  if (!selected) {
    setVrsFeedback("Selecione um perfil para ativar.", true);
    return;
  }

  await apiRequest(`/vrs-profiles/${selected.id}/activate`, {
    method: "POST"
  });

  setVrsFeedback("Perfil ativado com sucesso.");
  await refreshVrsProfilesAndActive();
}

function bindVrsProfileSelectChange() {
  const select = document.getElementById("vrsProfilesSelect");
  if (!select) return;

  select.addEventListener("change", () => {
    const selected = getSelectedProfileFromSelect();
    if (selected) {
      fillFormWithProfile(selected);
    }
  });
}

async function initVrsConfigModal() {
  const openBtn = document.getElementById("openVrsConfigBtn");
  const closeBtn = document.getElementById("closeVrsConfigBtn");
  const backdrop = document.getElementById("vrsConfigBackdrop");
  const refreshBtn = document.getElementById("refreshVrsProfilesBtn");
  const activateBtn = document.getElementById("activateSelectedProfileBtn");
  const saveNewBtn = document.getElementById("saveNewVrsProfileBtn");
  const updateBtn = document.getElementById("updateCurrentVrsProfileBtn");
  const newBtn = document.getElementById("newVrsProfileBtn");

  if (!openBtn) return;

  openBtn.addEventListener("click", async () => {
    openVrsConfigModal();
    clearVrsFeedback();
    await refreshVrsProfilesAndActive();

    const selected = getSelectedProfileFromSelect();
    if (selected) {
      fillFormWithProfile(selected);
    } else if (activeVrsProfile) {
      fillFormWithProfile(activeVrsProfile);
    } else {
      clearVrsProfileForm();
    }
  });

  closeBtn?.addEventListener("click", closeVrsConfigModal);
  backdrop?.addEventListener("click", closeVrsConfigModal);

  refreshBtn?.addEventListener("click", async () => {
    await refreshVrsProfilesAndActive();
    setVrsFeedback("Lista atualizada.");
  });

  activateBtn?.addEventListener("click", activateSelectedVrsProfile);
  saveNewBtn?.addEventListener("click", saveNewVrsProfile);
  updateBtn?.addEventListener("click", updateSelectedVrsProfile);

  newBtn?.addEventListener("click", () => {
    clearVrsFeedback();
    clearVrsProfileForm();
  });

  bindVrsProfileSelectChange();
}

/* =========================================================
   DASHBOARD PAGE
   ========================================================= */

function initDashboardPage() {
  
  if (!getToken()) {
    window.location.href = "./auth.html";
    return;
  }
  
  initCancelEditButton();
  initClearStationsButton();
  initSelectNearestButton();
  setSubmitButtonState(false);
  initVrsConfigModal();
  bindTargetPreviewEvents();
  initDashboardModeToggle();

  
  const startTimeInput = document.getElementById("start-time");
  const endTimeInput = document.getElementById("end-time");
  const occupationMinutesInput = document.getElementById("occupation-minutes");
  const jobMinutesInput = document.getElementById("job-minutes");

  if (isAdvancedDashboardPage()) {
  setCurrentMode("advanced");
} else if (isBasicDashboardPage()) {
  setCurrentMode("basic");
}


  function calculateJobMinutesAutomatically() {
    if (!startTimeInput || !endTimeInput) return;

    const start = (startTimeInput.value || "").trim();
    const end = (endTimeInput.value || "").trim();

    if (!start || !end) {
      if (occupationMinutesInput) occupationMinutesInput.value = "";
      if (jobMinutesInput) jobMinutesInput.value = "";
      return;
    }

    const [sh, sm] = start.split(":").map(Number);
    const [eh, em] = end.split(":").map(Number);

    if (
      Number.isNaN(sh) || Number.isNaN(sm) ||
      Number.isNaN(eh) || Number.isNaN(em)
    ) {
      if (occupationMinutesInput) occupationMinutesInput.value = "";
      if (jobMinutesInput) jobMinutesInput.value = "";
      return;
    }

    let startMinutes = sh * 60 + sm;
    let endMinutes = eh * 60 + em;

    if (endMinutes < startMinutes) {
      endMinutes += 24 * 60;
    }

    const totalMinutes = endMinutes - startMinutes;

    if (totalMinutes <= 0) {
      if (occupationMinutesInput) occupationMinutesInput.value = "";
      if (jobMinutesInput) jobMinutesInput.value = "";
      return;
    }

    if (occupationMinutesInput) occupationMinutesInput.value = totalMinutes;
    if (jobMinutesInput) jobMinutesInput.value = totalMinutes;
  }

  if (startTimeInput) {
    startTimeInput.addEventListener("change", calculateJobMinutesAutomatically);
    startTimeInput.addEventListener("input", calculateJobMinutesAutomatically);
  }

  if (endTimeInput) {
    endTimeInput.addEventListener("change", calculateJobMinutesAutomatically);
    endTimeInput.addEventListener("input", calculateJobMinutesAutomatically);
  }

  calculateJobMinutesAutomatically();

  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      removeToken();
      window.location.href = "./auth.html";
    });
  }

  const previewBtn = document.getElementById("preview-target-btn");
  if (previewBtn) {
    previewBtn.addEventListener("click", () => {
      clearMessage("job-message");
      try {
        const target = getTargetFromForm();
        if (!target) {
          throw new Error("Informe latitude, longitude e altitude para pré-visualizar o alvo.");
        }
        updateTargetPreviewFromForm(true);
      } catch (error) {
        showMessage(
          "job-message",
          error.message || "Não foi possível pré-visualizar o alvo.",
          true
        );
      }
    });
  }

  const jobForm = document.getElementById("job-form");
  if (jobForm) {
    jobForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearMessage("job-message");

      calculateJobMinutesAutomatically();

      const submitButton = document.getElementById("job-submit-btn");
      const originalButtonText = submitButton ? submitButton.textContent : "";

      try {
        validateJobFormFields();
        validateSelectedStations();

        const target = getTargetFromForm();
        if (!target) {
          throw new Error("Informe coordenadas válidas e clique em 'Mostrar alvo no mapa' antes de criar o job.");
        }

        if (!previewTargetMarker) {
          throw new Error("Mostre o alvo no mapa antes de criar o job.");
        }

        if (submitButton) {
          submitButton.disabled = true;
          submitButton.textContent = editingJobId ? "Salvando..." : "Enviando...";
        }

        if (editingJobId) {
          showMessage("job-message", "Atualizando o job. Aguarde...");
        } else {
          showMessage(
            "job-message",
            "Montando o job com as estações RBMC selecionadas. Aguarde..."
          );
        }

        const payload = {
          name: document.getElementById("job-name").value.trim(),
          output_rinex_name:
            document.getElementById("output-rinex-name").value.trim() || null,
          output_marker_name:
            document.getElementById("output-marker-name").value.trim() || null,

          target_lat: target.lat,
          target_lon: target.lon,
          target_height: target.h,

          selected_station_codes: Array.from(selectedGnssStations),

          session_date: document.getElementById("session-date").value || null,
          start_time: document.getElementById("start-time").value || null,
          end_time: document.getElementById("end-time").value || null,

          occupation_minutes: occupationMinutesInput && occupationMinutesInput.value
            ? Number(occupationMinutesInput.value)
            : null,

          requested_minutes: jobMinutesInput && jobMinutesInput.value
            ? Number(jobMinutesInput.value)
            : null,

          notes: document.getElementById("job-notes").value.trim() || null
        };

        if (!payload.requested_minutes || payload.requested_minutes <= 0) {
          throw new Error("Não foi possível calcular automaticamente os minutos do job. Verifique a hora inicial e a hora final.");
        }

        if (editingJobId) {
          await updateJob(editingJobId, payload);
          showMessage("job-message", "Job atualizado com sucesso.");
        } else {
          await createJob(payload);
          showMessage("job-message", "Job criado com sucesso.");
        }

        resetJobForm();
        calculateJobMinutesAutomatically();
        await loadDashboard();
      } catch (error) {
        console.error("Erro no envio do job:", error);
        showMessage(
          "job-message",
          error.message || "Não foi possível salvar o job.",
          true
        );
      } finally {
        if (submitButton) {
          submitButton.disabled = false;
          submitButton.textContent = originalButtonText || "Criar job";
        }
      }
    });
  }

  const syncRbmcBtn = document.getElementById("sync-rbmc-btn");
  if (syncRbmcBtn) {
    syncRbmcBtn.addEventListener("click", handleSyncRbmcStations);
  }

  loadDashboard();
}

/* =========================================================
   LANDING / STATIC PAGES
   ========================================================= */



function initStaticPageEnhancements() {
  const anchorLinks = document.querySelectorAll('a[href^="#"]');

  anchorLinks.forEach((link) => {
    link.addEventListener("click", (event) => {
      const href = link.getAttribute("href");
      if (!href || href === "#") return;

      const target = document.querySelector(href);
      if (!target) return;

      event.preventDefault();
      target.scrollIntoView({
        behavior: "smooth",
        block: "start"
      });
    });
  });
}



/* =========================================================
   BOOT
   ========================================================= */

document.addEventListener("DOMContentLoaded", () => {
  if (isAuthPage()) {
    initAuthPage();
    return;
  }

  if (isBasicDashboardPage()) {
    initDashboardPage();
  }
});