(function () {
  const loginView = document.getElementById("loginView");
  const appView = document.getElementById("appView");
  const loginForm = document.getElementById("loginForm");
  const pwInput = document.getElementById("pwInput");
  const loginErr = document.getElementById("loginErr");
  const logoutBtn = document.getElementById("logoutBtn");

  const ideaInput = document.getElementById("ideaInput");
  const wpmInput = document.getElementById("wpmInput");
  const buildPromptBtn = document.getElementById("buildPromptBtn");
  const promptErr = document.getElementById("promptErr");
  const promptWrap = document.getElementById("promptWrap");
  const promptOutput = document.getElementById("promptOutput");
  const copyPromptBtn = document.getElementById("copyPromptBtn");
  const copyStatus = document.getElementById("copyStatus");

  const jsonInput = document.getElementById("jsonInput");
  const validateBtn = document.getElementById("validateBtn");
  const jsonStatus = document.getElementById("jsonStatus");

  const samplePlayer = document.getElementById("samplePlayer");
  const voiceList = document.getElementById("voiceList");
  const speedInput = document.getElementById("speedInput");
  const bgModeSelect = document.getElementById("bgModeSelect");
  const manualBgWrap = document.getElementById("manualBgWrap");
  const bgIndexInput = document.getElementById("bgIndexInput");
  const nextBgHint = document.getElementById("nextBgHint");
  const renderBtn = document.getElementById("renderBtn");
  const renderErr = document.getElementById("renderErr");

  const refreshJobsBtn = document.getElementById("refreshJobsBtn");
  const jobsList = document.getElementById("jobsList");

  let voices = [];
  let selectedVoiceId = "";
  let pollTimer = null;

  function showErr(el, msg) {
    el.textContent = msg;
    el.classList.remove("hidden");
  }
  function hideEl(el) {
    el.classList.add("hidden");
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  }

  async function checkAuth() {
    const r = await api("/api/me");
    if (r.ok && r.data.authenticated) {
      loginView.classList.add("hidden");
      appView.classList.remove("hidden");
      logoutBtn.classList.remove("hidden");
      await Promise.all([loadVoices(), loadJobs()]);
    } else {
      loginView.classList.remove("hidden");
      appView.classList.add("hidden");
      logoutBtn.classList.add("hidden");
    }
  }

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideEl(loginErr);
    const r = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: pwInput.value }),
    });
    if (r.ok) {
      pwInput.value = "";
      await checkAuth();
    } else {
      showErr(loginErr, r.data.error || "Login failed");
    }
  });

  logoutBtn.addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" });
    if (pollTimer) clearInterval(pollTimer);
    await checkAuth();
  });

  buildPromptBtn.addEventListener("click", async () => {
    hideEl(promptErr);
    copyStatus.textContent = "";
    const idea = ideaInput.value.trim();
    if (!idea) {
      showErr(promptErr, "Enter a story idea first.");
      return;
    }
    buildPromptBtn.disabled = true;
    try {
      const r = await api("/api/prompt", {
        method: "POST",
        body: JSON.stringify({ idea, target_wpm: Number(wpmInput.value) || 250 }),
      });
      if (!r.ok) {
        showErr(promptErr, r.data.error || "Failed to build prompt");
        return;
      }
      promptOutput.value = r.data.prompt;
      promptWrap.classList.remove("hidden");
    } finally {
      buildPromptBtn.disabled = false;
    }
  });

  copyPromptBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(promptOutput.value);
      copyStatus.textContent = "Copied!";
    } catch {
      promptOutput.select();
      document.execCommand("copy");
      copyStatus.textContent = "Copied!";
    }
  });

  async function runValidation() {
    jsonStatus.innerHTML = "";
    const raw = jsonInput.value.trim();
    if (!raw) {
      jsonStatus.innerHTML = '<div class="err">Paste a production JSON first.</div>';
      return null;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      jsonStatus.innerHTML = `<div class="err">JSON syntax error: ${escapeHtml(e.message)}</div>`;
      return null;
    }
    const r = await api("/api/validate", {
      method: "POST",
      body: JSON.stringify({ production_json: parsed }),
    });
    if (!r.ok || !r.data.valid) {
      const errs = (r.data.errors || ["Validation failed"]).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
      jsonStatus.innerHTML = `<div class="err"><strong>Validation errors:</strong><ul>${errs}</ul></div>`;
      return null;
    }
    const totalWords = (parsed.parts || []).reduce((acc, p) => {
      return acc + (p.lines || []).reduce((la, l) => la + String(l.text || "").split(/\s+/).filter(Boolean).length, 0);
    }, 0);
    const wpm = Number(wpmInput.value) || 250;
    const estSec = Math.round((totalWords / wpm) * 60);
    jsonStatus.innerHTML = `<div class="ok">Valid (${escapeHtml(parsed.format)}): "${escapeHtml(parsed.series_title)}" &mdash; ${parsed.parts.length} part(s), ${totalWords} words (~${estSec}s at ${wpm} WPM).</div>`;
    return parsed;
  }

  validateBtn.addEventListener("click", runValidation);

  async function loadVoices() {
    const res = await fetch("/voices.json");
    const cfg = await res.json();
    voices = cfg.voices || [];
    if (voices.length && !selectedVoiceId) {
      selectedVoiceId = voices[0].id;
      speedInput.value = voices[0].default_rate || "+35%";
    }
    renderVoices();
  }

  function renderVoices() {
    voiceList.innerHTML = "";
    voices.forEach((v) => {
      const div = document.createElement("div");
      div.className = "voice-item" + (v.id === selectedVoiceId ? " selected" : "");
      div.innerHTML = `
        <label style="margin:0; font-weight:normal; cursor:pointer; flex:1 1 260px;">
          <input type="radio" name="voicePick" value="${escapeHtml(v.id)}" ${v.id === selectedVoiceId ? "checked" : ""} />
          <strong>${escapeHtml(v.name)}</strong> (${escapeHtml(v.gender)}, ${escapeHtml(v.default_rate)})
          <div class="muted">${escapeHtml(v.description)}</div>
        </label>
        <div class="row" style="margin-top:0;">
          <button type="button" class="secondary play-btn" data-src="${escapeHtml(v.sample_neutral || `/samples/${v.id}_neutral.mp3`)}">Play neutral</button>
          <button type="button" class="secondary play-btn" data-src="${escapeHtml(v.sample_emotional || `/samples/${v.id}_emotional.mp3`)}">Play emotional</button>
        </div>
      `;
      div.querySelector('input[type="radio"]').addEventListener("change", () => {
        selectedVoiceId = v.id;
        speedInput.value = v.default_rate || "+35%";
        renderVoices();
      });
      div.querySelectorAll(".play-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          samplePlayer.src = btn.dataset.src;
          samplePlayer.classList.remove("hidden");
          samplePlayer.play().catch(() => {});
        });
      });
      voiceList.appendChild(div);
    });
  }

  bgModeSelect.addEventListener("change", () => {
    manualBgWrap.classList.toggle("hidden", bgModeSelect.value !== "manual");
  });

  renderBtn.addEventListener("click", async () => {
    hideEl(renderErr);
    const parsed = await runValidation();
    if (!parsed) return;

    renderBtn.disabled = true;
    try {
      const r = await api("/api/render", {
        method: "POST",
        body: JSON.stringify({
          production_json: parsed,
          voice_id: selectedVoiceId,
          voice_speed: speedInput.value.trim() || "+35%",
          target_wpm: Number(wpmInput.value) || 250,
          background_mode: bgModeSelect.value,
          background_index: Number(bgIndexInput.value) || 0,
        }),
      });
      if (!r.ok) {
        showErr(renderErr, r.data.error || "Render request failed");
        return;
      }
      await loadJobs();
    } finally {
      renderBtn.disabled = false;
    }
  });

  async function loadJobs() {
    const r = await api("/api/jobs");
    if (!r.ok) return;
    nextBgHint.textContent = `Next sequential background clip #${r.data.next_background_index ?? 0}`;
    const jobs = r.data.jobs || [];
    if (!jobs.length) {
      jobsList.innerHTML = '<div class="muted">No jobs yet.</div>';
      return;
    }
    let hasActive = false;
    jobsList.innerHTML = jobs.map((j) => {
      if (j.status === "queued" || j.status === "running") hasActive = true;
      const partsHtml = (j.parts || []).map((p) => {
        const dl = `/api/download?job_id=${encodeURIComponent(j.job_id)}&part=${p.part_number}`;
        const attrText = p.attribution ? `<pre>${escapeHtml(p.attribution)}</pre>` : "";
        return `
          <div style="margin-top:8px; padding-top:8px; border-top:1px solid #e4e4e7;">
            <div><strong>Part ${p.part_number}:</strong> ${escapeHtml(p.title || "")} (${Math.round(p.duration_sec || 0)}s)</div>
            <div class="row">
              <a href="${dl}" download="${escapeHtml(p.filename || "")}"><button type="button">Download Part ${p.part_number} MP4</button></a>
            </div>
            ${attrText ? `<div class="muted" style="margin-top:6px;">Attribution to paste in description:</div>${attrText}` : ""}
          </div>
        `;
      }).join("");
      const errHtml = j.error ? `<div class="err">${escapeHtml(j.error)}</div>` : "";
      return `
        <div class="card" style="margin-bottom:10px;">
          <div><strong>${escapeHtml(j.series_title || j.job_id)}</strong> &mdash; Status: <strong>${escapeHtml(j.status)}</strong></div>
          <div class="muted">Job: ${escapeHtml(j.job_id)} | Voice: ${escapeHtml(j.voice_id)} (${escapeHtml(j.voice_speed)}) | Created: ${escapeHtml(j.created_at)}</div>
          ${errHtml}
          ${partsHtml}
        </div>
      `;
    }).join("");

    if (hasActive && !pollTimer) {
      pollTimer = setInterval(loadJobs, 5000);
    } else if (!hasActive && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  refreshJobsBtn.addEventListener("click", loadJobs);

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  checkAuth();
})();
