const API_BASE = "http://127.0.0.1:8000";
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
  const useJson = method !== "GET";

  return apiFetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...getAuthHeaders(useJson),
      ...(options.headers || {}),
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
    "Configure suas credenciais Earthdata / CDDIS no perfil antes de usar o modo avançado."
  );
  window.location.href = "./profile.html?from=advanced";
  throw new Error("Credenciais Earthdata / CDDIS não configuradas.");
}

  return true;
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

  if (!payload.advanced_config.target_step_seconds || payload.advanced_config.target_step_seconds <= 0) {
    throw new Error("Informe um passo temporal alvo válido.");
  }
}

async function processAdvancedJob(jobId) {
  const payload = buildAdvancedPayload();

  validateAdvancedForm(jobId, payload);

  clearAdvancedMessage();
  showAdvancedMessage("Iniciando processamento avançado...");
  setAdvancedProgress(2, "Iniciando...", "Preparando requisição.");

  stopAdvancedPolling();

  // consulta imediata
  await pollAdvancedProgress(jobId);

  advancedPollingTimer = setInterval(() => {
    pollAdvancedProgress(jobId);
  }, 1500);

  try {
    const result = await apiFetchAdvanced(`/jobs/${jobId}/process-advanced`, {
      method: "POST",
      body: JSON.stringify(payload),
    });

    // consulta final logo após concluir
    await pollAdvancedProgress(jobId);

    showAdvancedMessage(result.message || "Processamento avançado concluído.");
    return result;
  } catch (error) {
    stopAdvancedPolling();
    console.error("ERRO POST process-advanced:", error);
    showAdvancedMessage(error.message || "Falha no processamento avançado.", true);
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

function initAdvancedDashboardPage() {
  if (!isAdvancedPage()) return;

  bindAdvancedTopbarButtons();

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

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    try {
      await ensureEarthdataConfigured();

      const jobId = Number(
        document.getElementById("advanced-job-id")?.value || 0
      );

      await processAdvancedJob(jobId);
    } catch (error) {
      console.error("Falha no modo avançado:", error);
      showAdvancedMessage(
        error.message || "Falha no modo avançado.",
        true
      );
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (!isAdvancedPage()) return;
  initAdvancedDashboardPage();
});