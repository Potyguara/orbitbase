const API_BASE_URL = "http://127.0.0.1:8000";
const TOKEN_KEY = "orbitbase_token";

function getProfileOrigin() {
  const params = new URLSearchParams(window.location.search);
  const from = params.get("from");
  return from === "advanced" ? "advanced" : "basic";
}

function getDashboardUrlFromOrigin() {
  return getProfileOrigin() === "advanced"
    ? "./dashboard_advanced.html"
    : "./dashboard.html";
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function removeToken() {
  localStorage.removeItem(TOKEN_KEY);
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

function showMessage(message, isError = false) {
  const box = document.getElementById("earthdata-message-box");
  if (!box) return;

  box.textContent = message;
  box.classList.remove("hidden");
  box.style.color = isError ? "#b91c1c" : "#166534";
}

function clearMessage() {
  const box = document.getElementById("earthdata-message-box");
  if (!box) return;

  box.textContent = "";
  box.classList.add("hidden");
  box.style.color = "";
}

function setEarthdataStatus(text, configured = false) {
  const box = document.getElementById("earthdata-status-box");
  if (!box) return;

  box.textContent = text;
  box.style.color = configured ? "#166534" : "#92400e";
}

async function loadProfile() {
  const me = await apiFetch(`${API_BASE_URL}/users/me`, {
    method: "GET",
    headers: getAuthHeaders(false),
  });

  const nameInput = document.getElementById("profile-name");
  const emailInput = document.getElementById("profile-email");
  const planInput = document.getElementById("profile-plan");

  if (nameInput) nameInput.value = me.name || "";
  if (emailInput) emailInput.value = me.email || "";
  if (planInput) planInput.value = me.plan || "";
}

async function loadEarthdataCredentialsStatus() {
  const data = await apiFetch(`${API_BASE_URL}/users/me/advanced-credentials`, {
    method: "GET",
    headers: getAuthHeaders(false),
  });

  if (data.earthdata_configured) {
    setEarthdataStatus(
      `Credenciais configuradas para: ${data.earthdata_username_masked || "usuário protegido"}`,
      true
    );
  } else {
    setEarthdataStatus("Nenhuma credencial Earthdata / CDDIS configurada.", false);
  }

  return data;
}

async function saveEarthdataCredentials() {
  clearMessage();

  const username = (document.getElementById("earthdata-username")?.value || "").trim();
  const password = (document.getElementById("earthdata-password")?.value || "").trim();
  const button = document.getElementById("save-earthdata-btn");
  const originalText = button ? button.textContent : "Salvar credenciais";

  if (!username || !password) {
    showMessage("Informe usuário e senha Earthdata / CDDIS.", true);
    return;
  }

  try {
    if (button) {
      button.disabled = true;
      button.textContent = "Salvando...";
    }

    await apiFetch(`${API_BASE_URL}/users/me/advanced-credentials`, {
      method: "PUT",
      headers: getAuthHeaders(true),
      body: JSON.stringify({
        earthdata_username: username,
        earthdata_password: password,
      }),
    });

    const passwordInput = document.getElementById("earthdata-password");
    if (passwordInput) passwordInput.value = "";

    showMessage("Credenciais salvas com sucesso.");
    await loadEarthdataCredentialsStatus();
  } catch (error) {
    showMessage(error.message || "Não foi possível salvar as credenciais.", true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

async function deleteEarthdataCredentials() {
  clearMessage();

  const button = document.getElementById("delete-earthdata-btn");
  const originalText = button ? button.textContent : "Remover credenciais";

  try {
    if (button) {
      button.disabled = true;
      button.textContent = "Removendo...";
    }

    await apiFetch(`${API_BASE_URL}/users/me/advanced-credentials`, {
      method: "DELETE",
      headers: getAuthHeaders(false),
    });

    const usernameInput = document.getElementById("earthdata-username");
    const passwordInput = document.getElementById("earthdata-password");

    if (usernameInput) usernameInput.value = "";
    if (passwordInput) passwordInput.value = "";

    showMessage("Credenciais removidas com sucesso.");
    await loadEarthdataCredentialsStatus();
  } catch (error) {
    showMessage(error.message || "Não foi possível remover as credenciais.", true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function initProfilePage() {
  const token = getToken();
  if (!token) {
    window.location.href = "./auth.html";
    return;
  }

  const dashboardBtn = document.getElementById("go-dashboard-btn");
if (dashboardBtn && getProfileOrigin() === "advanced") {
  dashboardBtn.textContent = "Voltar ao avançado";
}

  document.getElementById("logout-btn")?.addEventListener("click", () => {
    removeToken();
    window.location.href = "./auth.html";
  });

document.getElementById("go-dashboard-btn")?.addEventListener("click", () => {
  window.location.href = getDashboardUrlFromOrigin();
});

document.getElementById("go-advanced-btn")?.addEventListener("click", () => {
  window.location.href = "./dashboard_advanced.html";
});

  document.getElementById("save-earthdata-btn")?.addEventListener("click", saveEarthdataCredentials);
  document.getElementById("delete-earthdata-btn")?.addEventListener("click", deleteEarthdataCredentials);

  Promise.all([loadProfile(), loadEarthdataCredentialsStatus()]).catch((error) => {
    showMessage(error.message || "Não foi possível carregar o perfil.", true);
  });
}

document.addEventListener("DOMContentLoaded", initProfilePage);