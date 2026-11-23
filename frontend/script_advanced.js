// Local (Live Server / arquivo): API em 127.0.0.1:8000. Em produção: mesma origem, prefixo /api.
const API_BASE = (["localhost", "127.0.0.1", ""].includes(window.location.hostname) ? "http://127.0.0.1:8000" : `${window.location.origin}/api`);
const TOKEN_KEY = "orbitbase_token";

let advancedPollingTimer = null;

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function removeToken() {
  localStorage.removeItem(TOKEN_KEY);
}

function isAdvancedPage() {
  return window.location.pathname.includes("dashboard_advanced.html");
}

function getAuthHeaders(json = true) {
  const token = getToken();
  const headers = {};

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  if (json) {
    headers["Content-Type"] = "application/json";
  }

  return headers;
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);

  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    data = null;
  }

  if (!response.ok) {
    throw new Error(data?.detail || data?.message || "Erro na requisição.");
  }

  return data;
}

async function apiFetchAdvanced(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const useJson = method !== "GET" && !(options.body instanceof FormData);

  const extraHeaders = { ...(options.headers || {}) };
  if (options.body instanceof FormData && extraHeaders["Content-Type"]) {
    delete extraHeaders["Content-Type"];
  }

  return apiFetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...getAuthHeaders(useJson),
      ...extraHeaders,
    },
  });
}

function showAdvancedMessage(message, isError = false) {
  const el = document.getElementById("advanced-message");
  if (!el) return;

  el.classList.remove("hidden");
  el.textContent = message;
  el.style.borderColor = isError ? "#ef4444" : "#22c55e";
}

function clearAdvancedMessage() {
  const el = document.getElementById("advanced-message");
  if (!el) return;

  el.classList.add("hidden");
  el.textContent = "";
  el.style.borderColor = "";
}

function setAdvancedProgress(percent, step, detail) {
  const bar = document.getElementById("advanced-progress-bar");
  const percentEl = document.getElementById("advanced-progress-percent");
  const stepEl = document.getElementById("advanced-progress-step");
  const detailEl = document.getElementById("advanced-progress-detail");

  const safePercent = Number(percent || 0);

  if (bar) bar.style.width = `${safePercent}%`;
  if (percentEl) percentEl.textContent = `${safePercent}%`;
  if (stepEl) stepEl.textContent = step || "Aguardando...";
  if (detailEl) detailEl.textContent = detail || "";
}

function stopAdvancedPolling() {
  if (advancedPollingTimer) {
    clearInterval(advancedPollingTimer);
    advancedPollingTimer = null;
  }
}

async function pollAdvancedProgress(jobId) {
  try {
    const progress = await apiFetchAdvanced(`/jobs/${jobId}/progress-advanced`, {
      method: "GET",
      headers: getAuthHeaders(false),
    });

    setAdvancedProgress(
      progress.percent || 0,
      progress.message || progress.step || "Processando...",
      progress.detail || ""
    );

    if (progress.status === "completed" || progress.status === "failed") {
      stopAdvancedPolling();
    }
  } catch (error) {
    console.error("Erro ao consultar progresso avançado:", error);
  }
}

async function ensureEarthdataConfigured() {
  const data = await apiFetch(`${API_BASE}/users/me/advanced-credentials`, {
    method: "GET",
    headers: getAuthHeaders(false),
  });

  if (!data.earthdata_configured) {
    alert(
      "Configure suas credenciais Earthdata / CDDIS no perfil antes de usar o modo avançado sem upload manual de SP3."
    );
    window.location.href = "./profile.html?from=advanced";
    throw new Error("Credenciais Earthdata / CDDIS não configuradas.");
  }

  return true;
}

function getAdvancedSp3Files() {
  const input = document.getElementById("advanced-sp3-file");
  if (!input || !input.files) return [];
  return Array.from(input.files);
}

function formatAdvancedFileList(files) {
  if (!files.length) return "Nenhum arquivo selecionado.";
  if (files.length === 1) return `1 arquivo selecionado: ${files[0].name}`;
  return `${files.length} arquivos selecionados: ${files.map((f) => f.name).join(", ")}`;
}

function buildAdvancedPayload() {
  return {
    session_date: document.getElementById("advanced-session-date")?.value || "",
    start_time: document.getElementById("advanced-start-time")?.value.trim() || "",
    end_time: document.getElementById("advanced-end-time")?.value.trim() || "",
    advanced_config: {
      target_step_seconds: Number(
        document.getElementById("advanced-step-seconds")?.value || 1
      ),
      use_precise_ephemeris:
        document.getElementById("advanced-use-sp3")?.checked || false,
      use_precise_clock:
        document.getElementById("advanced-use-clk")?.checked || false,
      interpolate_code_observables:
        document.getElementById("advanced-interpolate-code")?.checked || false,
      interpolate_phase_observables:
        document.getElementById("advanced-interpolate-phase")?.checked || false,
      receiver_brand:
        document.getElementById("advanced-receiver-brand")?.value.trim() || "",
      receiver_model:
        document.getElementById("advanced-receiver-model")?.value.trim() || "",
      receiver_version:
        document.getElementById("advanced-receiver-version")?.value.trim() || "",
      antenna_model:
        document.getElementById("advanced-antenna-model")?.value.trim() || "",
    },
  };
}

async function downloadAdvancedFiles(jobId) {
  const response = await fetch(`${API_BASE}/jobs/${jobId}/export-advanced`, {
    method: "GET",
    headers: getAuthHeaders(false),
  });
  if (!response.ok) {
    let detail = "Não foi possível baixar os arquivos do modo avançado.";
    try {
      detail = (await response.json())?.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/);
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = match ? match[1] : `job_${jobId}_avancado.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

let advancedJobsCache = null;

async function getAdvancedJob(jobId) {
  if (!advancedJobsCache) {
    const data = await apiFetchAdvanced("/jobs", { method: "GET" });
    advancedJobsCache = Array.isArray(data) ? data : (data?.jobs || data?.items || []);
  }
  return advancedJobsCache.find((job) => Number(job.id) === Number(jobId)) || null;
}

// Preenche a data/horários com os do job e impede rodar numa data diferente
// da dos arquivos RBMC do job (o erro era "Nenhuma base possui épocas válidas").
async function fillAdvancedFormFromJob(jobId) {
  if (!jobId) return;
  try {
    const job = await getAdvancedJob(jobId);
    if (!job) return;
    const dateInput = document.getElementById("advanced-session-date");
    if (dateInput && job.session_date) dateInput.value = String(job.session_date).slice(0, 10);
    for (const [id, field] of [["advanced-start-time", "start_time"], ["advanced-end-time", "end_time"]]) {
      const input = document.getElementById(id);
      if (input && !input.value.trim() && job[field]) {
        const value = String(job[field]);
        input.value = value.length === 5 ? `${value}:00` : value;
      }
    }
  } catch (error) {
    console.warn("Não foi possível carregar os dados do job:", error);
  }
}

function validateAdvancedForm(jobId, payload) {
  if (!jobId || Number.isNaN(jobId) || jobId <= 0) {
    throw new Error("Informe um ID de job válido.");
  }

  if (!payload.session_date) {
    throw new Error("Informe a data da sessão.");
  }

  if (!payload.start_time) {
    throw new Error("Informe a hora inicial.");
  }

  if (!payload.end_time) {
    throw new Error("Informe a hora final.");
  }

  if (
    !payload.advanced_config.target_step_seconds ||
    payload.advanced_config.target_step_seconds <= 0
  ) {
    throw new Error("Informe um passo temporal alvo válido.");
  }
}

async function uploadAdvancedEphemeris(jobId, sessionDate, files) {
  if (!files || !files.length) {
    return {
      ok: true,
      uploaded: false,
      file_count: 0,
      message: "Nenhuma efeméride enviada.",
    };
  }

  const formData = new FormData();
  formData.append("session_date", sessionDate);

  for (const file of files) {
    formData.append("files", file);
  }

  showAdvancedMessage(`Enviando ${files.length} efeméride(s) do dia...`);
  setAdvancedProgress(
    6,
    "Upload manual de efemérides",
    formatAdvancedFileList(files)
  );

  const result = await apiFetchAdvanced(
    `/jobs/${jobId}/advanced-ephemeris-upload`,
    {
      method: "POST",
      body: formData,
    }
  );

  return {
    ok: true,
    uploaded: true,
    file_count: files.length,
    response: result,
    message:
      result?.message ||
      `${files.length} efeméride(s) enviada(s) com sucesso.`,
  };
}

async function processAdvancedJob(jobId) {
  const payload = buildAdvancedPayload();
  const sp3Files = getAdvancedSp3Files();

  validateAdvancedForm(jobId, payload);

  clearAdvancedMessage();
  stopAdvancedPolling();

  const wantsPreciseEphemeris = !!payload.advanced_config.use_precise_ephemeris;
  const hasManualSp3 = sp3Files.length > 0;

  if (wantsPreciseEphemeris && !hasManualSp3) {
    await ensureEarthdataConfigured();
  }

  if (hasManualSp3) {
    await uploadAdvancedEphemeris(jobId, payload.session_date, sp3Files);
  }

  showAdvancedMessage("Iniciando processamento avançado...");
  setAdvancedProgress(
    10,
    "Iniciando...",
    hasManualSp3
      ? `Efemérides carregadas manualmente. ${formatAdvancedFileList(sp3Files)}`
      : "Preparando requisição."
  );

  await pollAdvancedProgress(jobId);

  advancedPollingTimer = setInterval(() => {
    pollAdvancedProgress(jobId);
  }, 1500);

  try {
    const result = await apiFetchAdvanced(`/jobs/${jobId}/process-advanced`, {
      method: "POST",
      body: JSON.stringify(payload),
    });

    await pollAdvancedProgress(jobId);

    showAdvancedMessage(result.message || "Processamento avançado concluído.");
    return result;
  } catch (error) {
    stopAdvancedPolling();
    console.error("ERRO POST process-advanced:", error);
    showAdvancedMessage(
      error.message || "Falha no processamento avançado.",
      true
    );
    throw error;
  }
}

function bindAdvancedTopbarButtons() {
  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      stopAdvancedPolling();
      removeToken();
      window.location.href = "./auth.html";
    });
  }

  const basicBtn = document.getElementById("go-basic-btn");
  if (basicBtn) {
    basicBtn.addEventListener("click", () => {
      stopAdvancedPolling();
      window.location.href = "./dashboard.html";
    });
  }

  const profileBtn = document.getElementById("go-profile-btn");
  if (profileBtn) {
    profileBtn.addEventListener("click", () => {
      stopAdvancedPolling();
      window.location.href = "./profile.html?from=advanced";
    });
  }
}

function bindAdvancedSp3Input() {
  const input = document.getElementById("advanced-sp3-file");
  if (!input) return;

  input.addEventListener("change", () => {
    const files = getAdvancedSp3Files();
    clearAdvancedMessage();

    if (!files.length) {
      showAdvancedMessage("Nenhuma efeméride selecionada.");
      return;
    }

    showAdvancedMessage(formatAdvancedFileList(files));
  });
}

function initAdvancedDashboardPage() {
  if (!isAdvancedPage()) return;

  bindAdvancedTopbarButtons();
  bindAdvancedSp3Input();

  const token = getToken();
  if (!token) {
    showAdvancedMessage(
      "Sessão não encontrada. Faça login novamente para usar o modo avançado.",
      true
    );
    return;
  }

  const form = document.getElementById("advanced-job-form");
  if (!form) return;

  document.getElementById("advanced-download-btn")?.addEventListener("click", async () => {
    try {
      const jobId = Number(document.getElementById("advanced-job-id")?.value || 0);
      if (!jobId) throw new Error("Informe o ID do job para baixar os arquivos.");
      await downloadAdvancedFiles(jobId);
    } catch (error) {
      showAdvancedMessage(error.message || "Falha ao baixar os arquivos.", true);
    }
  });

  const jobIdInput = document.getElementById("advanced-job-id");
  jobIdInput?.addEventListener("change", () => {
    fillAdvancedFormFromJob(Number(jobIdInput.value || 0));
  });
  if (jobIdInput?.value) fillAdvancedFormFromJob(Number(jobIdInput.value));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    try {
      const jobId = Number(
        document.getElementById("advanced-job-id")?.value || 0
      );

      const job = await getAdvancedJob(jobId).catch(() => null);
      const jobDate = job?.session_date ? String(job.session_date).slice(0, 10) : "";
      const formDate = document.getElementById("advanced-session-date")?.value || "";
      if (jobDate && formDate && jobDate !== formDate) {
        throw new Error(
          `A data informada (${formDate}) é diferente da data do job ${jobId} (${jobDate}). ` +
          "As bases RBMC do job são desse dia; corrija a data ou crie um job novo."
        );
      }

      await processAdvancedJob(jobId);
      advancedJobsCache = null;
    } catch (error) {
      console.error("Falha no modo avançado:", error);
      showAdvancedMessage(
        error.message || "Falha no modo avançado.",
        true
      );
    }
  });
}

let unifiedPollingTimer = null;

async function pollUnifiedProgress(jobId) {
  try {
    const progress = await apiFetchAdvanced(`/jobs/${jobId}/progress-unified`, {
      method: "GET",
      headers: getAuthHeaders(false),
    });
    setAdvancedProgress(progress.percent || 0, progress.message || progress.step || "Processando...", "");
    if (progress.status === "completed" || progress.status === "failed") {
      clearInterval(unifiedPollingTimer);
      unifiedPollingTimer = null;
    }
  } catch (error) {
    console.error("Erro ao consultar progresso do motor único:", error);
  }
}

async function processUnifiedJob(jobId) {
  const startTime = document.getElementById("advanced-start-time")?.value.trim();
  const endTime = document.getElementById("advanced-end-time")?.value.trim();
  const step = Number(document.getElementById("advanced-step-seconds")?.value || 1);
  if (!jobId) throw new Error("Informe o ID do job.");
  if (![1, 5, 10, 15, 30].includes(step)) throw new Error("Intervalo deve ser 1, 5, 10, 15 ou 30 s.");

  const job = await getAdvancedJob(jobId).catch(() => null);
  const jobDate = job?.session_date ? String(job.session_date).slice(0, 10) : "";
  const formDate = document.getElementById("advanced-session-date")?.value || "";
  if (jobDate && formDate && jobDate !== formDate) {
    throw new Error(`A data informada (${formDate}) é diferente da data do job ${jobId} (${jobDate}).`);
  }

  clearAdvancedMessage();
  setAdvancedProgress(2, "Iniciando motor único", "");
  if (unifiedPollingTimer) clearInterval(unifiedPollingTimer);
  unifiedPollingTimer = setInterval(() => pollUnifiedProgress(jobId), 3000);
  try {
    const data = await apiFetchAdvanced(`/jobs/${jobId}/process-unified`, {
      method: "POST",
      headers: getAuthHeaders(true),
      body: JSON.stringify({ start_time: startTime, end_time: endTime, step_seconds: step }),
    });
    const r = data?.result || {};
    setAdvancedProgress(100, "Base gerada", "");
    showAdvancedMessage(
      `Motor único concluído: ${r.epochs_written} épocas a ${r.step_seconds} s ` +
      `(${r.window_local?.[0]?.slice(11)} a ${r.window_local?.[1]?.slice(11)}). Use "Baixar base do motor único".`
    );
  } finally {
    if (unifiedPollingTimer) clearInterval(unifiedPollingTimer);
    unifiedPollingTimer = null;
  }
}

async function downloadUnifiedFiles(jobId) {
  const response = await fetch(`${API_BASE}/jobs/${jobId}/export-unified`, {
    method: "GET",
    headers: getAuthHeaders(false),
  });
  if (!response.ok) {
    let detail = "Não foi possível baixar a base do motor único.";
    try {
      detail = (await response.json())?.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `job_${jobId}_base.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

function bindUnifiedButtons() {
  const jobIdOf = () => Number(document.getElementById("advanced-job-id")?.value || 0);
  document.getElementById("unified-process-btn")?.addEventListener("click", async () => {
    const btn = document.getElementById("unified-process-btn");
    try {
      if (btn) btn.disabled = true;
      await processUnifiedJob(jobIdOf());
    } catch (error) {
      showAdvancedMessage(error.message || "Falha no motor único.", true);
    } finally {
      if (btn) btn.disabled = false;
    }
  });
  document.getElementById("unified-download-btn")?.addEventListener("click", async () => {
    try {
      if (!jobIdOf()) throw new Error("Informe o ID do job para baixar a base.");
      await downloadUnifiedFiles(jobIdOf());
    } catch (error) {
      showAdvancedMessage(error.message || "Falha ao baixar a base.", true);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (!isAdvancedPage()) return;
  initAdvancedDashboardPage();
  bindUnifiedButtons();
});