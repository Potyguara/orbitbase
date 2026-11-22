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
            (r.master_distance_km ? ` (${Number(r.master_distance_km).toFixed(1)} km)` : ""),
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
