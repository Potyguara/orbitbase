/* Interface única de processamento (motor único).
 * Carregado depois de script.js: reaproveita login, mapa, formulário e criação
 * de jobs, e troca o processamento e os downloads pelos do motor único. */

(function () {
  const API = API_BASE_URL;
  let unifiedStatus = {};
  let unifiedPoll = null;

  function token() {
    return localStorage.getItem("orbitbase_token");
  }

  function headers(json = false) {
    const h = { Authorization: `Bearer ${token()}` };
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function withSeconds(value) {
    const v = String(value || "").trim();
    return v.length === 5 ? `${v}:00` : v;
  }

  // Aceita HH:MM e HH:MM:SS (o script.js original só aceitava HH:MM).
  window.validateJobFormFields = function () {
    const name = document.getElementById("job-name")?.value.trim();
    const target = getTargetFromForm();
    const sessionDate = document.getElementById("session-date")?.value;
    const startTime = withSeconds(document.getElementById("start-time")?.value);
    const endTime = withSeconds(document.getElementById("end-time")?.value);

    if (!name) throw new Error("Informe o nome do job.");
    if (!target) throw new Error("Informe latitude, longitude e altura válidas do ponto.");
    if (!sessionDate) throw new Error("Informe a data.");
    if (!startTime) throw new Error("Informe a hora inicial.");
    if (!endTime) throw new Error("Informe a hora final.");

    const start = new Date(`${sessionDate}T${startTime}`);
    const end = new Date(`${sessionDate}T${endTime}`);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
      throw new Error("Data/hora inválida.");
    }
    if (end <= start) throw new Error("A hora final deve ser maior que a hora inicial.");

    const minutes = Math.max(1, Math.ceil((end - start) / 60000));
    const occ = document.getElementById("occupation-minutes");
    const req = document.getElementById("job-minutes");
    if (occ) occ.value = minutes;
    if (req) req.value = minutes;
  };

  async function fetchUnifiedStatus() {
    try {
      const res = await fetch(`${API}/jobs/unified/status`, { headers: headers() });
      if (!res.ok) return {};
      const data = await res.json();
      return data.jobs || {};
    } catch (_) {
      return {};
    }
  }

  function jobCard(job, st) {
    const ready = !!st?.ready;
    const codes = Array.isArray(job.selected_station_codes) ? job.selected_station_codes : [];
    const period = `${escapeHtml(job.session_date || "-")} · ${escapeHtml(job.start_time || "--")} → ${escapeHtml(job.end_time || "--")}`;
    let statusLine = "Pronto para processar";
    let statusClass = "ob-badge";
    if (st?.status === "running") {
      statusLine = `Processando… ${st.percent ?? 0}%`;
      statusClass = "ob-badge ob-badge-run";
    } else if (st?.status === "failed") {
      statusLine = "Falhou no último processamento";
      statusClass = "ob-badge ob-badge-fail";
    } else if (ready) {
      const master = st.master_station
        ? ` · base mestra ${escapeHtml(st.master_station)} (${Number(st.master_distance_km || 0).toFixed(1)} km)`
        : "";
      statusLine = `Base gerada: ${st.epochs_written ?? "-"} épocas a ${st.step_seconds ?? "-"} s${master}`;
      statusClass = "ob-badge ob-badge-ok";
    }
    const dis = ready ? "" : "disabled";
    return `
      <article class="ob-job" data-job-id="${job.id}">
        <div class="ob-job-main">
          <div class="ob-job-title">
            <strong>${escapeHtml(job.name)}</strong>
            <span class="ob-job-id">#${job.id}</span>
          </div>
          <div class="ob-job-meta">${period}</div>
          <div class="ob-job-meta">RBMC: ${escapeHtml(codes.join(", ") || "-")}</div>
          <span class="${statusClass}">${statusLine}</span>
        </div>
        <div class="ob-job-actions">
          <button class="ob-btn ob-btn-ghost ob-btn-sm" type="button" onclick="editJob(${job.id})">Editar</button>
          <button class="ob-btn ob-btn-primary ob-btn-sm" type="button" onclick="handleProcessReal(${job.id})">
            ${ready ? "Reprocessar" : "Processar"}
          </button>
          <div class="ob-dl-group">
            <button class="ob-btn ob-btn-dl" type="button" ${dis} onclick="obDownload(${job.id}, 'rinex', this)">RINEX</button>
            <button class="ob-btn ob-btn-dl" type="button" ${dis} onclick="obDownload(${job.id}, 'rbmc', this)">RBMC</button>
            <button class="ob-btn ob-btn-dl" type="button" ${dis} onclick="obDownload(${job.id}, 'ephemeris', this)">EFEMÉRIDES</button>
          </div>
        </div>
      </article>`;
  }

  window.renderJobs = function (jobs) {
    jobsCache = jobs || [];
    const box = document.getElementById("unified-jobs");
    if (!box) return;
    if (!jobsCache.length) {
      box.innerHTML = '<div class="ob-empty">Nenhum job ainda. Preencha os passos acima e clique em "Criar job".</div>';
      return;
    }
    box.innerHTML = jobsCache.map((job) => jobCard(job, unifiedStatus[String(job.id)])).join("");
    fetchUnifiedStatus().then((status) => {
      unifiedStatus = status;
      box.innerHTML = jobsCache.map((job) => jobCard(job, unifiedStatus[String(job.id)])).join("");
    });
  };

  function downloadNotice(text, isError) {
    // aviso visível mesmo com o modal de processamento aberto
    let box = document.getElementById("ob-download-notice");
    if (!box) {
      box = document.createElement("div");
      box.id = "ob-download-notice";
      box.className = "ob-download-notice";
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.classList.toggle("ob-download-notice-error", !!isError);
    box.classList.add("ob-download-notice-show");
    clearTimeout(box._timer);
    if (text) box._timer = setTimeout(() => box.classList.remove("ob-download-notice-show"), isError ? 9000 : 4000);
  }

  const LABELS = { rinex: "RINEX", rbmc: "RBMC", ephemeris: "EFEMÉRIDES" };

  window.obDownload = async function (jobId, kind, button) {
    const btn = button || (window.event && window.event.currentTarget) || null;
    const label = LABELS[kind] || kind;
    if (btn) { btn.disabled = true; btn.dataset.label = btn.textContent; btn.textContent = "Baixando…"; }
    downloadNotice(`Preparando ${label}… arquivos de 1 s podem ter dezenas de MB.`);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/download/${kind}`, { headers: headers() });
      if (!res.ok) {
        let detail = `Não foi possível baixar ${label} (erro ${res.status}).`;
        try { detail = (await res.json())?.detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const total = Number(res.headers.get("Content-Length") || 0);
      let blob;
      if (res.body && res.body.getReader) {
        const reader = res.body.getReader();
        const chunks = [];
        let received = 0;
        let last = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          received += value.length;
          const now = Date.now();
          if (now - last > 400) {
            last = now;
            const mb = (received / 1048576).toFixed(1);
            const pct = total ? ` (${Math.round((100 * received) / total)}%)` : "";
            downloadNotice(`Baixando ${label}: ${mb} MB${pct}. Não feche nem recarregue a página.`);
            if (btn) btn.textContent = total ? `${Math.round((100 * received) / total)}%` : `${mb} MB`;
          }
        }
        blob = new Blob(chunks, { type: res.headers.get("Content-Type") || "application/octet-stream" });
      } else {
        blob = await res.blob();
      }
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = match ? match[1] : `job_${jobId}_${kind}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // revogar na hora cancela downloads grandes em alguns navegadores
      setTimeout(() => URL.revokeObjectURL(url), 120000);
      downloadNotice(`${label}: download iniciado (${(blob.size / 1048576).toFixed(1)} MB).`);
    } catch (error) {
      downloadNotice(error.message || `Falha no download de ${label}.`, true);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label || label; }
    }
  };

  function downloadsHtml(jobId) {
    return `
      <p>Base gerada. Baixe os arquivos:</p>
      <div class="ob-dl-group">
        <button class="ob-btn ob-btn-dl" type="button" onclick="obDownload(${jobId}, 'rinex', this)">RINEX</button>
        <button class="ob-btn ob-btn-dl" type="button" onclick="obDownload(${jobId}, 'rbmc', this)">RBMC</button>
        <button class="ob-btn ob-btn-dl" type="button" onclick="obDownload(${jobId}, 'ephemeris', this)">EFEMÉRIDES</button>
      </div>`;
  }

  async function pollUnified(jobId) {
    try {
      const res = await fetch(`${API}/jobs/${jobId}/progress-unified`, { headers: headers() });
      if (!res.ok) return;
      const p = await res.json();
      if (p.status === "idle") return;
      updateProcessingModal({ percent: p.percent || 0, status: "running", message: p.message || "Processando…", detail: "" });
    } catch (_) {}
  }

  // Receptor no RINEX: o usuário escolhe um equipamento salvo no perfil
  // (Meus equipamentos). O RINEX recebe exatamente o que está no cadastro:
  // REC # = ID interno da placa, TYPE, firmware; ANT # / TYPE = serial e antena.
  let equipmentList = [];

  const $ = (id) => document.getElementById(id);

  function selectedEquipment() {
    const id = $("unified-equipment")?.value || "";
    return equipmentList.find((e) => String(e.id) === id) || null;
  }

  function receiverChoice() {
    const eq = selectedEquipment();
    if (!eq) return { type: null, number: null, version: null, antennaType: null, antennaNumber: null };
    return {
      type: eq.receiver_type || null, number: eq.board_id || null, version: eq.firmware || null,
      antennaType: eq.antenna_type || null, antennaNumber: eq.antenna_serial || null,
    };
  }

  function equipmentSummary(eq) {
    if (!eq) return "O RINEX sai como VRS genérica (REC # VRS, antena VIRTUAL).";
    const parts = [
      eq.receiver_type || "modelo não informado",
      eq.board_id ? `placa ${eq.board_id}` : "sem ID da placa",
      eq.firmware ? `firmware ${eq.firmware}` : null,
      eq.antenna_type ? `antena ${eq.antenna_type}` : "antena VIRTUAL",
    ];
    return parts.filter(Boolean).join(" · ");
  }

  function showSummary() {
    const el = $("unified-equipment-summary");
    if (el) el.textContent = equipmentSummary(selectedEquipment());
  }

  // Cabeçalho ASCII do .tps (JPS/Topcon): rcv/id, rcv/sn, rcv/ver/main e o modelo.
  function parseTpsHeader(text) {
    const pick = (re) => { const m = text.match(re); return m ? m[1].trim() : ""; };
    const id = pick(/rcv\/id="([^"]+)"/);
    const sn = pick(/rcv\/sn="([^"]+)"/);
    const fw = pick(/rcv\/ver\/main="([^"]+)"/);
    let model = pick(/JPS\s+([A-Z0-9_\-]+)\s+Receiver Log File/i);
    if (!model) model = pick(/rcv=\{[^,]+,([A-Z0-9_\-]+),/i);
    const labelSerial = (sn.match(/([0-9]{3,}[-0-9]*)\s*$/) || [])[1] || "";
    // firmware: só a versão e a família ("4.7 HSR"), sem a data
    const firmware = fw.split(/\s+/).slice(0, 2).join(" ");
    return { id, model: model.toUpperCase(), labelSerial, firmware };
  }

  function setSelectOrOther(selId, otherId, value) {
    const sel = $(selId);
    const other = $(otherId);
    if (!sel) return;
    if (!value) sel.value = "";
    else if (Array.from(sel.options).some((o) => o.value === value || o.text === value)) sel.value = value;
    else { sel.value = "__other"; if (other) other.value = value; }
    if (other) other.classList.toggle("hidden", sel.value !== "__other");
  }

  function readSelect(selId, otherId) {
    const sel = $(selId);
    let v = sel ? sel.value : "";
    if (v === "__other") v = ($(otherId)?.value || "").trim();
    return v || null;
  }

  function setVal(id, value) { const el = $(id); if (el) el.value = value || ""; }
  function val(id) { return ($(id)?.value || "").trim() || null; }

  function formStatus(text) { const el = $("unified-equipment-status"); if (el) el.textContent = text || ""; }

  // ---- formulário de cadastro (fica fechado no uso do dia a dia) ----
  let editingId = null;

  function fillForm(eq) {
    editingId = eq ? eq.id : null;
    setVal("unified-equipment-name", eq?.name);
    setSelectOrOther("unified-receiver", "unified-receiver-other", eq?.receiver_type);
    setVal("unified-receiver-number", eq?.board_id);
    setVal("unified-receiver-version", eq?.firmware);
    setSelectOrOther("unified-antenna", "unified-antenna-other", eq?.antenna_type);
    setVal("unified-antenna-number", eq?.antenna_serial);
    setVal("unified-tps-file", "");
    const tps = $("unified-tps-status"); if (tps) tps.textContent = "";
    const title = $("unified-equipment-title");
    if (title) title.textContent = eq ? `Editar ${eq.name}` : "Novo equipamento";
    $("unified-equipment-save").textContent = eq ? "Salvar alterações" : "Salvar equipamento";
    $("unified-equipment-delete")?.classList.toggle("hidden", !eq);
    $("unified-equipment-save-new")?.classList.toggle("hidden", !eq);
    formStatus("");
  }

  function openPanel(eq) {
    fillForm(eq);
    $("unified-equipment-panel")?.classList.remove("hidden");
    $("unified-equipment-name")?.focus();
  }

  function closePanel() { $("unified-equipment-panel")?.classList.add("hidden"); }

  function formBody() {
    const receiver = readSelect("unified-receiver", "unified-receiver-other");
    const board = val("unified-receiver-number");
    const name = val("unified-equipment-name") ||
      [receiver, val("unified-antenna-number") || board].filter(Boolean).join(" ");
    return {
      name, receiver_type: receiver, board_id: board, firmware: val("unified-receiver-version"),
      antenna_type: readSelect("unified-antenna", "unified-antenna-other"),
      antenna_serial: val("unified-antenna-number"),
    };
  }

  function renderEquipment(selectedId) {
    const sel = $("unified-equipment");
    if (!sel) return;
    sel.innerHTML = '<option value="">Padrão VRS (sem receptor)</option>';
    equipmentList.forEach((eq) => {
      const opt = document.createElement("option");
      opt.value = String(eq.id);
      opt.textContent = eq.board_id ? `${eq.name} · ${eq.board_id}` : eq.name;
      sel.appendChild(opt);
    });
    sel.value = equipmentList.some((e) => String(e.id) === String(selectedId)) ? String(selectedId) : "";
    const manage = $("unified-equipment-manage");
    if (manage) manage.textContent = sel.value ? "Editar este equipamento" : "Cadastrar equipamento";
    showSummary();
  }

  async function loadEquipment(selectedId) {
    let saved = "";
    try { saved = localStorage.getItem("ob_equipment_id") || ""; } catch (_) {}
    try {
      const res = await fetch(`${API}/users/me/equipment`, { headers: headers() });
      if (res.ok) equipmentList = await res.json();
    } catch (_) {}
    renderEquipment(selectedId ?? saved);
  }

  function rememberSelection(id) {
    try { localStorage.setItem("ob_equipment_id", id ? String(id) : ""); } catch (_) {}
  }

  async function saveEquipment(asNew) {
    const body = formBody();
    if (!body.name) { formStatus("Dê um apelido ou escolha o modelo."); return; }
    if (!body.board_id) { formStatus("Informe o ID interno da placa (rcv/id): é ele que o Topcon lê."); return; }
    const editing = !asNew && editingId != null;
    try {
      const res = await fetch(editing ? `${API}/users/me/equipment/${editingId}` : `${API}/users/me/equipment`, {
        method: editing ? "PUT" : "POST", headers: headers(true), body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { formStatus(data?.detail || "Não consegui salvar."); return; }
      rememberSelection(data.id);
      await loadEquipment(data.id);
      closePanel();
    } catch (_) { formStatus("Não consegui salvar."); }
  }

  async function deleteEquipment() {
    if (editingId == null) return;
    try {
      const res = await fetch(`${API}/users/me/equipment/${editingId}`, { method: "DELETE", headers: headers() });
      if (!res.ok) { formStatus("Não consegui excluir."); return; }
      rememberSelection("");
      await loadEquipment("");
      closePanel();
    } catch (_) { formStatus("Não consegui excluir."); }
  }

  function setupTpsReader() {
    const input = $("unified-tps-file");
    const status = $("unified-tps-status");
    if (!input) return;
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      try {
        const info = parseTpsHeader(await file.slice(0, 65536).text());
        if (!info.id) { if (status) status.textContent = "Não achei rcv/id no início deste arquivo."; return; }
        setVal("unified-receiver-number", info.id);
        if (info.firmware) setVal("unified-receiver-version", info.firmware);
        if (info.model) {
          setSelectOrOther("unified-receiver", "unified-receiver-other", `TPS ${info.model}`);
          setSelectOrOther("unified-antenna", "unified-antenna-other", `TPS${info.model}`);
        }
        if (info.labelSerial) setVal("unified-antenna-number", info.labelSerial);
        if (!val("unified-equipment-name") && info.model) {
          setVal("unified-equipment-name", `${info.model.replace(/_/g, " ")} ${info.labelSerial || info.id}`.trim());
        }
        if (status) {
          status.textContent = `Lido: placa ${info.id}` + (info.model ? ` · ${info.model}` : "") +
            (info.labelSerial ? ` · rótulo ${info.labelSerial}` : "") + ". Confira e salve.";
        }
      } catch (_) {
        if (status) status.textContent = "Não consegui ler o .tps.";
      }
    });
  }

  function setupReceiverField() {
    const sel = $("unified-equipment");
    if (!sel) return;
    sel.addEventListener("change", () => {
      rememberSelection(sel.value);
      renderEquipment(sel.value);
      closePanel();
    });
    $("unified-equipment-manage")?.addEventListener("click", () => openPanel(selectedEquipment()));
    $("unified-equipment-close")?.addEventListener("click", closePanel);
    $("unified-equipment-save")?.addEventListener("click", () => saveEquipment(false));
    $("unified-equipment-save-new")?.addEventListener("click", () => saveEquipment(true));
    $("unified-equipment-delete")?.addEventListener("click", deleteEquipment);
    ["unified-receiver", "unified-antenna"].forEach((id) => {
      $(id)?.addEventListener("change", () =>
        $(`${id}-other`)?.classList.toggle("hidden", $(id).value !== "__other"));
    });
    setupTpsReader();
    loadEquipment();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setupReceiverField);
  else setupReceiverField();

  window.handleProcessReal = async function (jobId) {
    const job = (jobsCache || []).find((j) => Number(j.id) === Number(jobId));
    const step = Number(document.getElementById("unified-step")?.value || 1);
    const downloads = document.getElementById("processingModalDownloads");
    if (downloads) { downloads.classList.add("hidden"); downloads.innerHTML = ""; }

    openProcessingModal(job?.name || `Job ${jobId}`);
    updateProcessingModal({ percent: 2, status: "running", message: "Iniciando…", detail: "" });
    if (unifiedPoll) clearInterval(unifiedPoll);
    unifiedPoll = setInterval(() => pollUnified(jobId), 3000);

    try {
      const res = await fetch(`${API}/jobs/${jobId}/process-unified`, {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({
          start_time: job?.start_time || null,
          end_time: job?.end_time || null,
          step_seconds: step,
          ...(() => {
            const c = receiverChoice();
            return {
              receiver_type: c.type, receiver_number: c.number, receiver_version: c.version,
              antenna_type: c.antennaType, antenna_number: c.antennaNumber,
            };
          })(),
        }),
      });
      let payload = {};
      try { payload = await res.json(); } catch (_) {}
      clearInterval(unifiedPoll);
      unifiedPoll = null;

      if (!res.ok) {
        finishProcessingModal({
          percent: 100, status: "failed", message: "Falha no processamento",
          detail: payload?.detail || "Erro inesperado no processamento.",
        }, true);
      } else {
        const r = payload.result || {};
        finishProcessingModal({
          percent: 100, status: "completed", message: "Base VRS gerada",
          detail: `${r.epochs_written} épocas a ${r.step_seconds} s · base mestra ${r.master_station || "-"}` +
            (r.master_distance_km ? ` (${Number(r.master_distance_km).toFixed(1)} km)` : "") +
            (r.receiver ? ` · REC # ${r.receiver.number} (${r.receiver.type})` : ""),
        });
        if (downloads) { downloads.innerHTML = downloadsHtml(jobId); downloads.classList.remove("hidden"); }
      }
    } catch (error) {
      clearInterval(unifiedPoll);
      unifiedPoll = null;
      finishProcessingModal({ percent: 100, status: "failed", message: "Falha no processamento", detail: error?.message || "" }, true);
    }
    await refreshJobsTableSafe();
  };
})();
