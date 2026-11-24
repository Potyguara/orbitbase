/* Central de downloads: efemérides GPS/GLONASS e RINEX da RBMC por data. */
(function () {
  const API = ["localhost", "127.0.0.1", ""].includes(window.location.hostname)
    ? "http://127.0.0.1:8000"
    : `${window.location.origin}/api`;
  const MAX_STATIONS = 10;
  const MAX_DAYS = 31;

  const $ = (id) => document.getElementById(id);
  const selected = new Set();
  let stations = [];
  let poll = null;
  let currentTask = null;

  function token() {
    try { return localStorage.getItem("orbitbase_token"); } catch (_) { return null; }
  }

  function headers(json = false) {
    const h = { Authorization: `Bearer ${token()}`, "ngrok-skip-browser-warning": "1" };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  async function api(path, options = {}) {
    const res = await fetch(`${API}${path}`, options);
    if (res.status === 401) {
      window.location.href = "./auth.html";
      throw new Error("Sessão expirada.");
    }
    if (!res.ok) {
      let detail = `Erro ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function norm(text) {
    return String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  function parseDate(value) {
    if (!value) return null;
    const [y, m, d] = value.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  function dayOfYear(dt) {
    return Math.floor((dt - Date.UTC(dt.getUTCFullYear(), 0, 1)) / 86400000) + 1;
  }

  function fmt(dt) {
    return dt.toLocaleDateString("pt-BR", { timeZone: "UTC" });
  }

  function daysSelected() {
    const a = parseDate($("dl-start").value);
    if (!a) return [];
    const b = parseDate($("dl-end").value) || a;
    const [first, last] = a <= b ? [a, b] : [b, a];
    const out = [];
    for (let t = first.getTime(); t <= last.getTime() && out.length <= MAX_DAYS; t += 86400000) {
      out.push(new Date(t));
    }
    return out;
  }

  function refreshSummary() {
    const days = daysSelected();
    const doy = $("dl-doy");
    if (!days.length) {
      doy.textContent = "--";
    } else if (days.length === 1) {
      doy.textContent = `Dia ${String(dayOfYear(days[0])).padStart(3, "0")} de ${days[0].getUTCFullYear()}`;
    } else {
      const first = days[0], last = days[days.length - 1];
      doy.textContent = days.length > MAX_DAYS
        ? `Mais de ${MAX_DAYS} dias: reduza o período`
        : `Dias ${String(dayOfYear(first)).padStart(3, "0")} a ${String(dayOfYear(last)).padStart(3, "0")} (${days.length} dias)`;
    }

    const parts = [];
    if (days.length) {
      parts.push(days.length === 1 ? fmt(days[0]) : `${fmt(days[0])} a ${fmt(days[days.length - 1])}`);
    }
    if ($("dl-eph").checked) parts.push("efemérides GPS + GLONASS");
    if ($("dl-sp3").checked) parts.push("órbita precisa (SP3)");
    if (selected.size) parts.push(`RBMC: ${[...selected].join(", ")}`);
    $("dl-summary").textContent = parts.join(" · ");
    $("dl-count").textContent = `${selected.size} de ${MAX_STATIONS}`;
  }

  function renderChips() {
    $("dl-chips").innerHTML = [...selected].map((code) =>
      `<button type="button" class="ob-chip" data-code="${escapeHtml(code)}" title="Remover">${escapeHtml(code)} ×</button>`
    ).join("");
  }

  function renderStations() {
    const q = norm($("dl-search").value.trim());
    const uf = $("dl-state").value;
    const list = stations.filter((s) => {
      if (uf && s.state !== uf) return false;
      if (!q) return true;
      return norm(`${s.station_code} ${s.station_name} ${s.city || ""} ${s.state || ""}`).includes(q);
    });
    if (!list.length) {
      $("dl-stations").innerHTML = '<div class="ob-empty">Nenhuma estação encontrada.</div>';
      return;
    }
    $("dl-stations").innerHTML = list.map((s) => {
      const code = s.station_code.toUpperCase();
      const where = [s.city, s.state].filter(Boolean).join(" / ");
      const inactive = s.is_active === false ? ' <span class="ob-badge ob-badge-fail">inativa</span>' : "";
      return `<label class="ob-station">
        <input type="checkbox" value="${escapeHtml(code)}" ${selected.has(code) ? "checked" : ""} />
        <strong>${escapeHtml(code)}</strong>
        <span>${escapeHtml(where || s.station_name)}${inactive}</span>
      </label>`;
    }).join("");
  }

  function toggleStation(code, on) {
    if (on && !selected.has(code) && selected.size >= MAX_STATIONS) {
      alertMsg(`Escolha no máximo ${MAX_STATIONS} estações por vez.`);
      renderStations();
      return;
    }
    if (on) selected.add(code); else selected.delete(code);
    renderChips();
    renderStations();
    refreshSummary();
  }

  function alertMsg(text, error = true) {
    const box = $("dl-progress");
    box.hidden = false;
    $("dl-message").innerHTML = `<span class="ob-badge ${error ? "ob-badge-fail" : "ob-badge-ok"}">${escapeHtml(text)}</span>`;
  }

  async function loadStations() {
    try {
      const res = await api("/gnss-stations?limit=1000", { headers: headers() });
      stations = (await res.json()).sort((a, b) => a.station_code.localeCompare(b.station_code));
      const states = [...new Set(stations.map((s) => s.state).filter(Boolean))].sort();
      $("dl-state").innerHTML = '<option value="">Todas</option>' +
        states.map((uf) => `<option value="${escapeHtml(uf)}">${escapeHtml(uf)}</option>`).join("");
      renderStations();
    } catch (err) {
      $("dl-stations").innerHTML = `<div class="ob-empty">Não foi possível carregar as estações: ${escapeHtml(err.message)}</div>`;
    }
  }

  async function loadUser() {
    try {
      const res = await api("/users/me", { headers: headers() });
      const me = await res.json();
      $("user-name-nav").textContent = me.name || me.email || "Conta";
    } catch (_) {
      $("user-name-nav").textContent = "Conta";
    }
  }

  function showStatus(st) {
    $("dl-progress").hidden = false;
    $("dl-bar").style.width = `${Math.max(3, st.percent || 0)}%`;
    const badge = st.status === "done" ? "ob-badge-ok" : st.status === "failed" ? "ob-badge-fail" : "ob-badge-run";
    const label = st.status === "done" ? "Pronto" : st.status === "failed" ? "Falhou" : "Baixando";
    const size = st.file_size ? ` · ${(st.file_size / 1048576).toFixed(1).replace(".", ",")} MB` : "";
    $("dl-message").innerHTML = `<span class="ob-badge ${badge}">${label}</span> ${escapeHtml(st.message || "")}${size}`;
    $("dl-log").innerHTML = (st.log || []).map((l) => `<li>${escapeHtml(l)}</li>`).join("");
    $("dl-save").hidden = st.status !== "done";
  }

  async function saveZip() {
    if (!currentTask) return;
    const btn = $("dl-save");
    btn.disabled = true;
    try {
      const res = await api(`/downloads/${currentTask}/file`, { headers: headers() });
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const name = (cd.match(/filename="?([^";]+)"?/) || [])[1] || "orbitbase_downloads.zip";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (err) {
      alertMsg(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  async function pollTask() {
    try {
      const res = await api(`/downloads/${currentTask}`, { headers: headers() });
      const st = await res.json();
      showStatus(st);
      if (st.status === "done" || st.status === "failed") {
        clearInterval(poll);
        poll = null;
        $("dl-submit").disabled = false;
        if (st.status === "done") saveZip();
      }
    } catch (err) {
      clearInterval(poll);
      poll = null;
      $("dl-submit").disabled = false;
      alertMsg(err.message);
    }
  }

  async function submit(ev) {
    ev.preventDefault();
    const start = $("dl-start").value;
    if (!start) return alertMsg("Informe a data inicial.");
    if (daysSelected().length > MAX_DAYS) return alertMsg(`Escolha no máximo ${MAX_DAYS} dias por vez.`);
    if (!$("dl-eph").checked && !$("dl-sp3").checked && !selected.size) {
      return alertMsg("Marque as efemérides, a órbita precisa ou escolha ao menos uma estação RBMC.");
    }
    $("dl-submit").disabled = true;
    $("dl-save").hidden = true;
    $("dl-log").innerHTML = "";
    showStatus({ status: "running", percent: 0, message: "Iniciando..." });
    try {
      const res = await api("/downloads", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({
          start_date: start,
          end_date: $("dl-end").value || null,
          ephemeris: $("dl-eph").checked,
          include_rinex3: $("dl-rnx3").checked,
          precise: $("dl-sp3").checked,
          stations: [...selected],
          convert_hatanaka: $("dl-crx").checked,
        }),
      });
      currentTask = (await res.json()).task_id;
      if (poll) clearInterval(poll);
      poll = setInterval(pollTask, 2000);
      pollTask();
    } catch (err) {
      $("dl-submit").disabled = false;
      alertMsg(err.message);
    }
  }

  function init() {
    if (!token()) {
      window.location.href = "./auth.html";
      return;
    }
    const yesterday = new Date(Date.now() - 86400000);
    const iso = new Date(yesterday.getTime() - yesterday.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    $("dl-start").max = iso;
    $("dl-end").max = iso;
    $("dl-start").value = iso;

    ["dl-start", "dl-end", "dl-eph", "dl-sp3"].forEach((id) => $(id).addEventListener("change", refreshSummary));
    $("dl-eph").addEventListener("change", () => { $("dl-rnx3").disabled = !$("dl-eph").checked; });
    $("dl-search").addEventListener("input", renderStations);
    $("dl-state").addEventListener("change", renderStations);
    $("dl-stations").addEventListener("change", (ev) => {
      if (ev.target.matches("input[type=checkbox]")) toggleStation(ev.target.value, ev.target.checked);
    });
    $("dl-chips").addEventListener("click", (ev) => {
      const chip = ev.target.closest(".ob-chip");
      if (chip) toggleStation(chip.dataset.code, false);
    });
    $("dl-form").addEventListener("submit", submit);
    $("dl-save").addEventListener("click", saveZip);
    $("logout-btn").addEventListener("click", () => {
      try { localStorage.removeItem("orbitbase_token"); } catch (_) {}
      window.location.href = "./auth.html";
    });

    refreshSummary();
    loadUser();
    loadStations();
  }

  init();
})();
