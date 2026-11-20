(function () {
  const MAX_DIRECT_UPLOAD_BYTES = 95 * 1024 * 1024; // 95 MiB

  const navBar = document.getElementById("navBar");
  const tabCreateBtn = document.getElementById("tabCreateBtn");
  const tabSettingsBtn = document.getElementById("tabSettingsBtn");
  const logoutBtn = document.getElementById("logoutBtn");

  const loginView = document.getElementById("loginView");
  const createView = document.getElementById("createView");
  const settingsView = document.getElementById("settingsView");

  const loginForm = document.getElementById("loginForm");
  const pwInput = document.getElementById("pwInput");
  const loginErr = document.getElementById("loginErr");

  // Create View controls
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

  const voiceSelect = document.getElementById("voiceSelect");
  const speedInput = document.getElementById("speedInput");
  const bgSelect = document.getElementById("bgSelect");
  const manualBgWrap = document.getElementById("manualBgWrap");
  const bgIndexInput = document.getElementById("bgIndexInput");
  const scheduleWrap = document.getElementById("scheduleWrap");
  const scheduleDtInput = document.getElementById("scheduleDtInput");
  const publishSummaryHint = document.getElementById("publishSummaryHint");
  const nextBgHint = document.getElementById("nextBgHint");
  const renderBtn = document.getElementById("renderBtn");
  const renderErr = document.getElementById("renderErr");

  const refreshJobsBtn = document.getElementById("refreshJobsBtn");
  const runTickBtn = document.getElementById("runTickBtn");
  const tickStatus = document.getElementById("tickStatus");
  const taskDeleteNotice = document.getElementById("taskDeleteNotice");
  const jobsList = document.getElementById("jobsList");

  const deleteConfirmModal = document.getElementById("deleteConfirmModal");
  const deleteConfirmTitle = document.getElementById("deleteConfirmTitle");
  const deleteConfirmBody = document.getElementById("deleteConfirmBody");
  const deleteCancelBtn = document.getElementById("deleteCancelBtn");
  const deleteConfirmBtn = document.getElementById("deleteConfirmBtn");

  const lastTasksById = new Map();
  const deletingTasks = new Set();
  let pendingDeleteTaskId = null;

  // Settings View controls
  const setDefaultVoice = document.getElementById("setDefaultVoice");
  const setDefaultBg = document.getElementById("setDefaultBg");
  const setCadenceCount = document.getElementById("setCadenceCount");
  const setCadenceUnit = document.getElementById("setCadenceUnit");
  const setPart2Delay = document.getElementById("setPart2Delay");
  const setDryRun = document.getElementById("setDryRun");
  const setMockMode = document.getElementById("setMockMode");
  const saveSettingsBtn = document.getElementById("saveSettingsBtn");
  const settingsStatus = document.getElementById("settingsStatus");
  const settingsErr = document.getElementById("settingsErr");

  const newKeyLabel = document.getElementById("newKeyLabel");
  const newKeySecret = document.getElementById("newKeySecret");
  const addKeyBtn = document.getElementById("addKeyBtn");
  const refreshAccountsBtn = document.getElementById("refreshAccountsBtn");
  const keysStatus = document.getElementById("keysStatus");
  const keysErr = document.getElementById("keysErr");
  const keysList = document.getElementById("keysList");
  const accountsList = document.getElementById("accountsList");

  const bgUploadFile = document.getElementById("bgUploadFile");
  const bgUploadTitle = document.getElementById("bgUploadTitle");
  const bgUploadNote = document.getElementById("bgUploadNote");
  const bgUploadAttr = document.getElementById("bgUploadAttr");
  const bgUploadBtn = document.getElementById("bgUploadBtn");
  const bgUploadErr = document.getElementById("bgUploadErr");

  const bgUrlInput = document.getElementById("bgUrlInput");
  const bgUrlTitle = document.getElementById("bgUrlTitle");
  const bgUrlNote = document.getElementById("bgUrlNote");
  const bgUrlAttr = document.getElementById("bgUrlAttr");
  const bgImportUrlBtn = document.getElementById("bgImportUrlBtn");
  const bgUrlErr = document.getElementById("bgUrlErr");

  const bgRefreshBtn = document.getElementById("bgRefreshBtn");
  const bgNormalizeAllBtn = document.getElementById("bgNormalizeAllBtn");
  const bgMgrStatus = document.getElementById("bgMgrStatus");
  const bgImportTasksWrap = document.getElementById("bgImportTasksWrap");
  const bgPreviewWrap = document.getElementById("bgPreviewWrap");
  const bgPreviewErr = document.getElementById("bgPreviewErr");
  const bgFilter = document.getElementById("bgFilter");
  const bgPreviewTitle = document.getElementById("bgPreviewTitle");
  const bgPreviewVideo = document.getElementById("bgPreviewVideo");
  const bgPreviewCloseBtn = document.getElementById("bgPreviewCloseBtn");
  const bgLibraryGrid = document.getElementById("bgLibraryGrid");

  let activeTab = "create";
  let voices = [], allVoices = [], hasVoiceStudioSamples = false;
  const setVoiceEngine = document.getElementById('setVoiceEngine');
  const setVoiceSpeedup = document.getElementById('setVoiceSpeedup');
  const engineStatus = document.getElementById('voiceEngineStatus');
  const voiceStudioPresetOptions = [
    {id:'vs-harper',name:'Harper',descriptor:'Female · American · lower register'},
    {id:'vs-rowan',name:'Rowan',descriptor:'Male · British · lower register'},
    {id:'vs-morgan',name:'Morgan',descriptor:'Female · British · mid register'},
  ].map(v => ({...v,engine:'voicestudio'}));
  let bgClips = [];
  let settings = {
    default_voice_id: "en-US-AndrewMultilingualNeural",
    default_background: "sequential",
    queue_cadence_count: 3,
    queue_cadence_unit: "day",
    part2_delay_minutes: 20,
    dry_run: false,
    mock_mode: false,
  };
  let zernioKeys = [];
  let zernioAccounts = [];
  let selectedVoiceId = "";
  let voiceOverridden = false;
  let bgOverridden = false;
  let bgDefaultsDirty = false;
  setDefaultBg.addEventListener("change", () => { bgDefaultsDirty = true; });
  let speedOverridden = false;
  let pollTimer = null;

  // Incremental task log cache: taskId -> { lastId, entries }
  const logCache = new Map();
  const openLogs = new Set();
  const deleteConfirmBg = new Set();
  let lastSeenBgImportDoneId = null;
  let backgroundsRequest = 0;
  let editingBackground = null;

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showErr(el, msg) {
    el.textContent = msg;
    el.classList.remove("hidden");
  }
  function hideEl(el) {
    el.classList.add("hidden");
  }

  function fmtBytes(bytes) {
    if (!bytes && bytes !== 0) return "";
    return `${(Number(bytes) / (1024 * 1024)).toFixed(1)} MB`;
  }

  function fmtLagosTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    try {
      return d.toLocaleString("en-GB", {
        timeZone: "Africa/Lagos",
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }) + " WAT";
    } catch {
      return iso;
    }
  }

  function getLagosInputDefault() {
    const t = new Date(Date.now() + 60 * 60 * 1000 + 60 * 60 * 1000); // now + 1h in UTC+1
    return t.toISOString().slice(0, 16);
  }

  function lagosLocalToUtcIso(val) {
    if (!val) return null;
    // Africa/Lagos is UTC+01:00 year-round (no DST)
    const d = new Date(`${val}:00+01:00`);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }

  async function api(path, opts = {}) {
    let res;
    try {
      const headers = opts.rawBody
        ? { ...(opts.headers || {}) }
        : { "Content-Type": "application/json", ...(opts.headers || {}) };
      res = await fetch(path, { credentials: "same-origin", ...opts, headers });
    } catch {
      return { ok: false, status: 0, data: { error: "Network unavailable. Check your connection and retry." } };
    }
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  }

  function switchTab(tab) {
    activeTab = tab;
    stopNarration();
    closeBackgroundPreview();
    createView.classList.toggle("hidden", tab !== "create");
    settingsView.classList.toggle("hidden", tab !== "settings");
    tabCreateBtn.className = tab === "create" ? "active-tab" : "secondary";
    tabSettingsBtn.className = tab === "settings" ? "active-tab" : "secondary";
    if (tab === "settings") {
      loadZernioKeysAndAccounts();
      loadBackgrounds();
    }
  }

  tabCreateBtn.addEventListener("click", () => switchTab("create"));
  tabSettingsBtn.addEventListener("click", () => switchTab("settings"));

  function updateCalibratedRate() {
    if (speedOverridden) return;
    const voice = voices.find((v) => v.id === selectedVoiceId);
    if (!voice?.base_wpm) return;
    const rate = Math.max(-50, Math.min(65, Math.round(((Number(wpmInput.value) || 250) / voice.base_wpm - 1) * 100)));
    speedInput.value = `${rate >= 0 ? "+" : ""}${rate}%`;
  }
  speedInput.addEventListener("input", () => { speedOverridden = true; });
  wpmInput.addEventListener("input", updateCalibratedRate);

  async function checkAuth() {
    const r = await api("/api/me");
    if (r.ok && r.data.authenticated) {
      loginView.classList.add("hidden");
      navBar.classList.remove("hidden");
      switchTab(activeTab);
      await loadVoices();
      await Promise.all([loadSettings(), loadBackgrounds(), loadZernioKeysAndAccounts()]);
      applyDefaultsToCreateScreen();
      await loadJobs();
    } else {
      stopNarration();
      closeBackgroundPreview();
      if (editingBackground) closeBackgroundEditor();
      loginView.classList.remove("hidden");
      createView.classList.add("hidden");
      settingsView.classList.add("hidden");
      navBar.classList.add("hidden");
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
    pollTimer = null;
    await checkAuth();
  });

  // --- Voices ---
  function populateVoiceOptions(select, engine) {
    const list = allVoices.filter(v => v.engine === engine);
    select.innerHTML = list.map(v => '<option value="' + escapeHtml(v.id) + '">' + escapeHtml(v.name) + ' (' + escapeHtml(v.descriptor || v.gender) + ')</option>').join('');
    return list;
  }

  async function loadVoices() {
    const cfg = await (await fetch('/voices.json')).json();
    const response = await fetch('/api/voice-samples/voices.json').catch(() => null);
    const release = response?.ok ? (await response.json().catch(() => ({}))).voices || [] : [];
    const proxy = filename => filename ? '/api/voice-samples/' + encodeURIComponent(filename) : undefined;
    hasVoiceStudioSamples = release.some(v => v.engine === 'voicestudio');
    allVoices = (cfg.voices || []).map(v => {
      const r = release.find(x => x.engine === 'edge' && x.id === v.id);
      return {...v, engine:'edge', descriptor: r?.descriptor || [v.gender,v.accent].filter(Boolean).join(' · '), sample_neutral: proxy(r?.sample_file) || v.sample_neutral, sample_emotional: proxy(r?.emotional_sample_file) || v.sample_emotional};
    }).concat(voiceStudioPresetOptions.map(v => {
      const r = release.find(x => x.engine === v.engine && x.id === v.id);
      return {...v, sample_neutral: proxy(r?.sample_file), sample_emotional: proxy(r?.emotional_sample_file)};
    }));
  }

  function populateSettingsVoices() {
    stopNarration();
    populateVoiceOptions(setDefaultVoice, setVoiceEngine.value);
    const saved = setVoiceEngine.value === 'voicestudio' ? settings.voicestudio_voice_id : settings.default_voice_id;
    if (Array.from(setDefaultVoice.options).some(option => option.value === saved)) setDefaultVoice.value = saved;
    engineStatus.textContent = setVoiceEngine.value === 'edge' ? 'Edge TTS ready.' : hasVoiceStudioSamples ? 'VoiceStudio samples ready. A cold runner downloads the model.' : 'VoiceStudio samples not published yet. First run downloads the model.';
  }
  setVoiceEngine.addEventListener('change', populateSettingsVoices);

  const players = Array.from(document.querySelectorAll("[data-narration]")).map((wrap) => {
    const select = wrap.dataset.narration === "settings" ? setDefaultVoice : voiceSelect;
    wrap.innerHTML = `<div class="row">
      <button class="secondary" type="button" data-sample="neutral">Play neutral</button>
      <button class="secondary" type="button" data-sample="emotional">Play dramatic</button>
    </div><div class="muted" role="status"></div><audio class="hidden" controls preload="none" aria-label="Narration sample"></audio>`;
    const audio = wrap.querySelector("audio");
    const status = wrap.querySelector('[role="status"]');
    select.addEventListener("change", () => stopNarration());
    audio.addEventListener("error", () => {
      if (audio.hasAttribute("src")) status.textContent = "Couldn't load this narration sample.";
    });
    wrap.querySelectorAll("[data-sample]").forEach((button) => button.addEventListener("click", () => {
      const voice = allVoices.find((v) => v.id === select.value);
      if (!voice) { status.textContent = "Choose a narrator first."; return; }
      stopNarration();
      closeBackgroundPreview();
      const kind = button.dataset.sample;
      const src = voice['sample_' + kind];
      if (!src) { status.textContent = 'This sample has not been published yet.'; return; }
      audio.src = src;
      status.textContent = `${voice.name} · ${kind === "neutral" ? "Neutral" : "Dramatic"} sample`;
      audio.classList.remove("hidden");
      audio.play().catch((err) => {
        if (err.name !== "AbortError" && audio.getAttribute("src") === src && !audio.error) {
          status.textContent = "Use the audio player's play button to start the sample.";
        }
      });
    }));
    return { audio, status };
  });

  function stopNarration() {
    for (const { audio, status } of players) {
      if (!audio.hasAttribute("src")) continue;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      hideEl(audio);
      status.textContent = "";
    }
  }

  voiceSelect.addEventListener("change", () => {
    selectedVoiceId = voiceSelect.value;
    voiceOverridden = true;
    speedOverridden = false;
    updateCalibratedRate();
  });

  // --- SF-006: Settings ---
  async function loadSettings() {
    const r = await api("/api/settings");
    if (!r.ok || !r.data.settings) return;
    settings = r.data.settings;
    setVoiceEngine.value = settings.voice_engine || 'edge';
    setVoiceSpeedup.value = settings.voicestudio_speedup ?? 1.25;
    populateSettingsVoices();
    if (!bgDefaultsDirty) setDefaultBg.value = settings.default_background || "sequential";
    setCadenceCount.value = String(settings.queue_cadence_count ?? 3);
    setCadenceUnit.value = settings.queue_cadence_unit || "day";
    setPart2Delay.value = String(settings.part2_delay_minutes ?? 20);
    setDryRun.checked = Boolean(settings.dry_run);
    setMockMode.checked = Boolean(settings.mock_mode);
    updatePublishHint();
  }

  function applyDefaultsToCreateScreen() {
    const engine = settings.voice_engine || 'edge';
    const noteEl = document.getElementById('createEngineNote');
    if (noteEl) {
      noteEl.textContent = engine === 'voicestudio'
        ? `Engine: VoiceStudio (${settings.voicestudio_speedup ?? 1.25}x speed-up)`
        : 'Engine: Edge TTS';
    }
    voices = populateVoiceOptions(voiceSelect, engine);
    speedInput.disabled = engine === 'voicestudio';
    const saved = engine === 'voicestudio' ? settings.voicestudio_voice_id : settings.default_voice_id;
    if (!voiceOverridden || !voices.some(v => v.id === selectedVoiceId)) selectedVoiceId = voices.some(v => v.id === saved) ? saved : voices[0]?.id;
    voiceSelect.value = selectedVoiceId;
    updateCalibratedRate();
    if (!bgOverridden) {
      const defBg = settings.default_background || "sequential";
      const hasOption = Array.from(bgSelect.options).some((o) => o.value === defBg);
      bgSelect.value = hasOption ? defBg : "sequential";
      manualBgWrap.classList.toggle("hidden", bgSelect.value !== "manual");
    }
    updatePublishHint();
  }

  function updatePublishHint() {
    const flags = [];
    if (settings.dry_run) flags.push("DRY RUN ON (no live posts or deletions)");
    if (settings.mock_mode) flags.push("MOCK ADAPTER ON");
    const accMsg = zernioAccounts.length
      ? `${zernioAccounts.length} connected account(s) across ${zernioKeys.length} key(s)`
      : `0 connected Zernio accounts (renders will complete & wait for keys before publishing)`;
    const cadenceMsg = `Queue cadence: ${settings.queue_cadence_count}/${settings.queue_cadence_unit} | Part 2 delay: ${settings.part2_delay_minutes} min`;
    publishSummaryHint.textContent = `${accMsg}. ${cadenceMsg}.${flags.length ? " [" + flags.join(", ") + "]" : ""}`;
  }

  saveSettingsBtn.addEventListener("click", async () => {
    hideEl(settingsErr);
    settingsStatus.textContent = "Saving...";
    saveSettingsBtn.disabled = true;
    try {
      const r = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          voice_engine: setVoiceEngine.value,
          voicestudio_speedup: Number(setVoiceSpeedup.value),
          ...(setVoiceEngine.value === 'edge' ? {default_voice_id: setDefaultVoice.value} : {voicestudio_voice_id: setDefaultVoice.value}),
          default_background: setDefaultBg.value,
          queue_cadence_count: Number(setCadenceCount.value) || 3,
          queue_cadence_unit: setCadenceUnit.value,
          part2_delay_minutes: Number(setPart2Delay.value) || 20,
          dry_run: setDryRun.checked,
          mock_mode: setMockMode.checked,
        }),
      });
      if (!r.ok) {
        settingsStatus.textContent = "";
        showErr(settingsErr, r.data.error || "Failed to save settings");
        return;
      }
      settings = r.data.settings;
      voiceOverridden = false;
      bgOverridden = false;
      applyDefaultsToCreateScreen();
      bgDefaultsDirty = false;
      renderBackgroundLibrary();
      settingsStatus.textContent = "Saved!";
    } finally {
      saveSettingsBtn.disabled = false;
    }
  });

  // --- SF-005: Zernio Keys & Accounts ---
  async function loadZernioKeysAndAccounts() {
    const [kr, ar] = await Promise.all([api("/api/zernio/keys"), api("/api/zernio/accounts")]);
    if (kr.ok) zernioKeys = kr.data.keys || [];
    if (ar.ok) zernioAccounts = ar.data.accounts || [];
    renderZernioKeys();
    renderZernioAccounts();
    updatePublishHint();
  }

  function renderZernioKeys() {
    if (!zernioKeys.length) {
      keysList.innerHTML = '<div class="muted">No Zernio API keys added yet.</div>';
      return;
    }
    keysList.innerHTML = `
      <table>
        <thead>
          <tr><th>Label</th><th>Key</th><th>Status</th><th>Actions</th></tr>
        </thead>
        <tbody>
          ${zernioKeys
            .map((k) => {
              const badge =
                k.last_test_ok === 1
                  ? '<span class="badge ok">OK</span>'
                  : k.last_test_ok === 0
                  ? '<span class="badge err">Error</span>'
                  : '<span class="badge">Untested</span>';
              return `
                <tr>
                  <td><strong>${escapeHtml(k.label)}</strong></td>
                  <td class="mono">${escapeHtml(k.masked)}</td>
                  <td>${badge} <span class="muted">${escapeHtml(k.last_test_msg || "")}</span></td>
                  <td>
                    <button type="button" class="secondary test-key-btn" data-id="${escapeHtml(k.id)}">Test</button>
                    <button type="button" class="danger del-key-btn" data-id="${escapeHtml(k.id)}">Delete</button>
                  </td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    `;
    keysList.querySelectorAll(".test-key-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        keysStatus.textContent = "Testing connection...";
        const r = await api(`/api/zernio/keys/test?id=${encodeURIComponent(btn.dataset.id)}`, { method: "POST" });
        await loadZernioKeysAndAccounts();
        await loadJobs();
        keysStatus.textContent = r.data.test_result?.ok
          ? `Connected (${r.data.test_result.accounts_count} accounts).${r.data.unlocked_tasks ? ` Started ${r.data.unlocked_tasks} waiting task(s).` : ""}`
          : r.data.test_result?.error || "Test finished";
        btn.disabled = false;
      });
    });
    keysList.querySelectorAll(".del-key-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        await api(`/api/zernio/keys?id=${encodeURIComponent(btn.dataset.id)}`, { method: "DELETE" });
        await loadZernioKeysAndAccounts();
      });
    });
  }

  function renderZernioAccounts() {
    if (!zernioAccounts.length) {
      accountsList.innerHTML = '<div class="muted">No active connected accounts found.</div>';
      return;
    }
    accountsList.innerHTML = `
      <table>
        <thead>
          <tr><th>Key Label</th><th>Platform</th><th>Account Name</th><th>Username</th><th>ID</th></tr>
        </thead>
        <tbody>
          ${zernioAccounts
            .map(
              (a) => `
              <tr>
                <td>${escapeHtml(a.key_label || a.key_id)}</td>
                <td><strong>${escapeHtml(a.platform)}</strong></td>
                <td>${escapeHtml(a.account_name)}</td>
                <td class="muted">${escapeHtml(a.username || "")}</td>
                <td class="mono muted">${escapeHtml(a.account_id)}</td>
              </tr>
            `
            )
            .join("")}
        </tbody>
      </table>
    `;
  }

  addKeyBtn.addEventListener("click", async () => {
    hideEl(keysErr);
    keysStatus.textContent = "";
    const label = newKeyLabel.value.trim();
    const apiKey = newKeySecret.value.trim();
    if (!label || !apiKey) {
      showErr(keysErr, "Enter both a label and Zernio API key.");
      return;
    }
    addKeyBtn.disabled = true;
    keysStatus.textContent = "Saving & testing...";
    try {
      const r = await api("/api/zernio/keys", {
        method: "POST",
        body: JSON.stringify({ label, api_key: apiKey }),
      });
      if (!r.ok) {
        keysStatus.textContent = "";
        showErr(keysErr, r.data.error || "Failed to add key");
        return;
      }
      newKeyLabel.value = "";
      newKeySecret.value = "";
      await loadZernioKeysAndAccounts();
      await loadJobs();
      keysStatus.textContent = r.data.test_result?.ok
        ? `Added & connected (${r.data.test_result.accounts_count} account(s)).${r.data.unlocked_tasks ? ` Unlocked ${r.data.unlocked_tasks} waiting task(s).` : ""}`
        : `Saved key, but test failed: ${r.data.test_result?.error || "error"}`;
    } finally {
      addKeyBtn.disabled = false;
    }
  });

  refreshAccountsBtn.addEventListener("click", async () => {
    hideEl(keysErr);
    keysStatus.textContent = "Refreshing all accounts...";
    refreshAccountsBtn.disabled = true;
    try {
      const r = await api("/api/zernio/accounts/refresh", { method: "POST" });
      await loadZernioKeysAndAccounts();
      await loadJobs();
      keysStatus.textContent = `Refreshed ${zernioAccounts.length} account(s).${r.data.unlocked_tasks ? ` Unlocked ${r.data.unlocked_tasks} waiting task(s).` : ""}`;
    } finally {
      refreshAccountsBtn.disabled = false;
    }
  });

  // --- SF-008: Background Video Manager ---
  async function loadBackgrounds() {
    const requestId = ++backgroundsRequest;
    const r = await api("/api/backgrounds");
    if (requestId !== backgroundsRequest) return;
    if (!r.ok) {
      bgMgrStatus.textContent = r.data.error || "Couldn't load the background library. Try Refresh library.";
      if (!bgClips.length) bgLibraryGrid.textContent = "Library unavailable.";
      return;
    }
    bgClips = r.data.clips || [];
    nextBgHint.textContent = `Next sequential background clip #${r.data.next_background_index ?? 0}`;

    // Populate dropdowns on Create screen & Settings screen
    const prevCreateVal = bgSelect.value;
    const prevSetVal = setDefaultBg.value;

    const clipOpts = bgClips
      .filter((c) => c.asset_exists !== false && !c.unlisted)
      .map((c) => `<option value="${escapeHtml(c.filename)}">${escapeHtml(c.title || c.filename)}</option>`)
      .join("");

    bgSelect.innerHTML =
      `<option value="sequential">Next in sequence (library rotation)</option>` +
      clipOpts +
      `<option value="manual">Manual index (0-based)</option>`;

    setDefaultBg.innerHTML =
      `<option value="sequential">Next in sequence (rotate library)</option>` + clipOpts;

    if (bgOverridden && Array.from(bgSelect.options).some((o) => o.value === prevCreateVal)) {
      bgSelect.value = prevCreateVal;
    } else {
      bgOverridden = false;
      const defBg = settings.default_background || "sequential";
      bgSelect.value = Array.from(bgSelect.options).some((o) => o.value === defBg) ? defBg : "sequential";
    }
    manualBgWrap.classList.toggle("hidden", bgSelect.value !== "manual");

    const wantedSetBg = (bgDefaultsDirty ? prevSetVal : settings.default_background) || "sequential";
    setDefaultBg.value = Array.from(setDefaultBg.options).some((o) => o.value === wantedSetBg)
      ? wantedSetBg
      : "sequential";

    renderBackgroundLibrary();
  }

  function renderBackgroundLibrary() {
    // Don't destroy an open form when the jobs poll refreshes the clip list.
    if (editingBackground) return;
    const query = bgFilter.value.trim().toLowerCase();
    const clips = bgClips.filter((c) => [c.title, c.filename, c.note].some((v) => String(v || "").toLowerCase().includes(query)));
    if (!clips.length) {
      bgLibraryGrid.textContent = bgClips.length ? "No clips match your search." : "No background clips in library yet.";
      return;
    }
    bgLibraryGrid.innerHTML = clips.map((c) => {
      const isDefault = settings.default_background === c.filename;
      const confirming = deleteConfirmBg.has(c.filename);
      const thumb = c.thumbnail
        ? `<img class="bg-thumb" src="/api/backgrounds/thumb?file=${encodeURIComponent(c.thumbnail)}" alt="" loading="lazy" />`
        : `<div class="bg-thumb muted" style="display:grid;place-items:center;color:white;">No thumbnail</div>`;
      const meta = [c.duration_sec ? `${Math.round(c.duration_sec)} s` : "", c.size_bytes ? fmtBytes(c.size_bytes) : ""].filter(Boolean);
      return `<article class="bg-card" data-file="${escapeHtml(c.filename)}">
        ${thumb}
        <div><strong>${escapeHtml(c.title || c.filename)}</strong> ${isDefault ? '<span class="badge ok">Default</span>' : ""}</div>
        <div class="muted">${escapeHtml(meta.join(" · "))}${c.unlisted ? " · Not in rotation" : ""}</div>
        ${c.note ? `<div class="muted">${escapeHtml(c.note)}</div>` : ""}
        <div class="row">
          <button type="button" class="secondary" data-action="preview" ${c.asset_exists === false ? "disabled" : ""}>Preview</button>
          <button type="button" class="secondary" data-action="edit">Edit details</button>
        </div>
        <details ${confirming ? "open" : ""}>
          <summary class="muted">Manage</summary>
          <div class="muted">${escapeHtml(c.filename)}</div>
          ${[c.attribution, c.additional_credit].filter(Boolean).map((credit) => `<div class="muted">Credit: ${escapeHtml(credit)}</div>`).join("")}
          <div class="row">
            <button type="button" class="secondary" data-action="default" ${isDefault || c.asset_exists === false || c.unlisted ? "disabled" : ""}>Set default</button>
            ${confirming ? `<button type="button" class="danger" data-action="confirm-delete">Confirm delete</button>
              <button type="button" class="secondary" data-action="cancel-delete">Cancel</button>`
            : `<button type="button" class="danger" data-action="delete">Delete</button>`}
          </div>
        </details>
      </article>`;
    }).join("");
  }

  const bgEditForm = document.getElementById("bgEditForm");
  const bgEditTitle = document.getElementById("bgEditTitle");
  const bgEditNote = document.getElementById("bgEditNote");
  const bgEditCredit = document.getElementById("bgEditCredit");
  const bgEditOriginalCredit = document.getElementById("bgEditOriginalCredit");
  const bgEditErr = document.getElementById("bgEditErr");
  const bgEditSave = document.getElementById("bgEditSave");

  function closeBackgroundEditor() {
    editingBackground = null;
    hideEl(bgEditForm);
    renderBackgroundLibrary();
  }

  function closeBackgroundPreview() {
    bgPreviewVideo.pause();
    if (bgPreviewVideo.hasAttribute("src")) {
      bgPreviewVideo.removeAttribute("src");
      bgPreviewVideo.removeAttribute("poster");
      bgPreviewVideo.load();
    }
    hideEl(bgPreviewErr);
    hideEl(bgPreviewWrap);
  }

  bgPreviewCloseBtn.addEventListener("click", closeBackgroundPreview);
  bgPreviewVideo.addEventListener("error", () => {
    if (bgPreviewVideo.hasAttribute("src")) showErr(bgPreviewErr, "Couldn't play this clip. Refresh the library and try again.");
  });
  bgFilter.addEventListener("input", renderBackgroundLibrary);
  document.getElementById("bgEditCancel").addEventListener("click", closeBackgroundEditor);
  bgEditForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!editingBackground || bgEditSave.disabled) return;
    const filename = editingBackground;
    hideEl(bgEditErr);
    bgEditSave.disabled = true;
    const r = await api(`/api/backgrounds?filename=${encodeURIComponent(filename)}`, {
      method: "PATCH",
      body: JSON.stringify({ title: bgEditTitle.value, note: bgEditNote.value, additional_credit: bgEditCredit.value }),
    });
    bgEditSave.disabled = false;
    if (!r.ok) {
      if (editingBackground === filename) showErr(bgEditErr, r.data.error || "Couldn't save clip details.");
      return;
    }
    if (editingBackground === filename) closeBackgroundEditor();
    bgMgrStatus.textContent = "Clip details saved.";
    await loadBackgrounds();
  });

  bgLibraryGrid.addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-action]");
    if (!btn || btn.disabled) return;
    const file = btn.closest("[data-file]").dataset.file;
    const clip = bgClips.find((c) => c.filename === file);
    if (!clip) return;
    const endpoint = `/api/backgrounds?filename=${encodeURIComponent(file)}`;
    switch (btn.dataset.action) {
      case "preview":
        stopNarration();
        closeBackgroundPreview();
        bgPreviewTitle.textContent = `Preview: ${clip.title || file}`;
        if (clip.thumbnail) bgPreviewVideo.poster = `/api/backgrounds/thumb?file=${encodeURIComponent(clip.thumbnail)}`;
        bgPreviewVideo.src = `/api/backgrounds/stream?file=${encodeURIComponent(file)}`;
        bgPreviewWrap.classList.remove("hidden");
        bgPreviewWrap.scrollIntoView({ block: "nearest" });
        bgPreviewVideo.play().catch(() => {}); // Native controls remain available if autoplay is denied.
        break;
      case "edit":
        if (editingBackground) { bgEditTitle.focus(); break; }
        editingBackground = file;
        bgEditTitle.value = clip.title || file;
        bgEditNote.value = clip.note || "";
        bgEditCredit.value = clip.additional_credit || "";
        bgEditOriginalCredit.textContent = clip.attribution ? `Existing credit (kept): ${clip.attribution}` : "No original credit recorded.";
        hideEl(bgEditErr);
        bgEditForm.classList.remove("hidden");
        bgEditTitle.focus();
        break;
      case "default": {
        btn.disabled = true;
        const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ default_background: file }) });
        btn.disabled = false;
        if (!r.ok) { bgMgrStatus.textContent = r.data.error || "Couldn't set default."; break; }
        settings = r.data.settings;
        bgDefaultsDirty = false;
        setDefaultBg.value = file;
        applyDefaultsToCreateScreen();
        renderBackgroundLibrary();
        bgMgrStatus.textContent = "Default background saved.";
        break;
      }
      case "delete":
        deleteConfirmBg.add(file);
        renderBackgroundLibrary();
        break;
      case "cancel-delete":
        deleteConfirmBg.delete(file);
        renderBackgroundLibrary();
        break;
      case "confirm-delete": {
        btn.disabled = true;
        bgMgrStatus.textContent = `Deleting ${file}…`;
        const r = await api(endpoint, { method: "DELETE" });
        btn.disabled = false;
        if (!r.ok) { bgMgrStatus.textContent = r.data.error || "Couldn't delete the clip."; break; }
        deleteConfirmBg.delete(file);
        if (editingBackground === file) closeBackgroundEditor();
        if (bgPreviewVideo.getAttribute("src")?.endsWith(`=${encodeURIComponent(file)}`)) closeBackgroundPreview();
        if (settings.default_background === file) settings.default_background = "sequential";
        await loadBackgrounds();
        bgMgrStatus.textContent = `Deleted ${file}.`;
        break;
      }
    }
  });

  bgUploadBtn.addEventListener("click", async () => {
    hideEl(bgUploadErr);
    const file = bgUploadFile.files?.[0];
    if (!file) {
      showErr(bgUploadErr, "Choose a video file first.");
      return;
    }
    if (file.size > MAX_DIRECT_UPLOAD_BYTES) {
      showErr(
        bgUploadErr,
        `File is ${fmtBytes(file.size)}, which exceeds StoryForge's direct-upload limit (95 MiB). Use URL / Google Drive import instead.`
      );
      return;
    }
    bgUploadBtn.disabled = true;
    bgMgrStatus.textContent = `Uploading ${file.name} (${fmtBytes(file.size)})...`;
    try {
      const qs = new URLSearchParams({
        filename: file.name,
        title: bgUploadTitle.value.trim(),
        note: bgUploadNote.value.trim(),
        attribution: bgUploadAttr.value.trim(),
      });
      const r = await api(`/api/backgrounds/upload?${qs.toString()}`, {
        method: "POST",
        rawBody: true,
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      if (!r.ok) {
        bgMgrStatus.textContent = "";
        showErr(bgUploadErr, r.data.error || "Upload failed");
        return;
      }
      bgUploadFile.value = "";
      bgUploadTitle.value = "";
      bgUploadNote.value = "";
      bgUploadAttr.value = "";
      if (r.data.task_id) openLogs.add(r.data.task_id);
      bgMgrStatus.textContent = "Uploaded! Normalizing in GitHub Actions...";
      await loadJobs();
    } finally {
      bgUploadBtn.disabled = false;
    }
  });

  bgImportUrlBtn.addEventListener("click", async () => {
    hideEl(bgUrlErr);
    const urlVal = bgUrlInput.value.trim();
    if (!urlVal) {
      showErr(bgUrlErr, "Enter a video URL or Google Drive share link.");
      return;
    }
    bgImportUrlBtn.disabled = true;
    bgMgrStatus.textContent = "Dispatching URL import...";
    try {
      const r = await api("/api/backgrounds/import-url", {
        method: "POST",
        body: JSON.stringify({
          url: urlVal,
          title: bgUrlTitle.value.trim(),
          note: bgUrlNote.value.trim(),
          attribution: bgUrlAttr.value.trim(),
        }),
      });
      if (!r.ok) {
        bgMgrStatus.textContent = "";
        showErr(bgUrlErr, r.data.error || "Import dispatch failed");
        return;
      }
      bgUrlInput.value = "";
      bgUrlTitle.value = "";
      bgUrlNote.value = "";
      bgUrlAttr.value = "";
      if (r.data.task_id) openLogs.add(r.data.task_id);
      bgMgrStatus.textContent = "Import queued in GitHub Actions...";
      await loadJobs();
    } finally {
      bgImportUrlBtn.disabled = false;
    }
  });

  bgNormalizeAllBtn.addEventListener("click", async () => {
    bgNormalizeAllBtn.disabled = true;
    bgMgrStatus.textContent = "Dispatching library normalization...";
    try {
      const r = await api("/api/backgrounds/normalize-existing", { method: "POST" });
      if (r.ok && r.data.task_id) {
        openLogs.add(r.data.task_id);
        bgMgrStatus.textContent = "Normalization queued in GitHub Actions...";
        await loadJobs();
      } else {
        bgMgrStatus.textContent = r.data.error || "Dispatch failed";
      }
    } finally {
      bgNormalizeAllBtn.disabled = false;
    }
  });

  bgRefreshBtn.addEventListener("click", async () => {
    bgMgrStatus.textContent = "";
    await loadBackgrounds();
  });

  // --- Prompt & Production JSON ---
  buildPromptBtn.addEventListener("click", async () => {
    hideEl(promptErr);
    copyStatus.textContent = "";
    const idea = ideaInput.value;
    if (!idea.trim()) {
      promptOutput.value = "";
      promptWrap.classList.add("hidden");
      showErr(promptErr, "Enter a story idea first.");
      return;
    }
    buildPromptBtn.disabled = true;
    try {
      const r = await api("/api/prompt", {
        method: "POST",
        body: JSON.stringify({ idea, format: document.getElementById("storyFormat").value, target_wpm: Number(wpmInput.value) || 250 }),
      });
      if (!r.ok) {
        promptOutput.value = "";
        promptWrap.classList.add("hidden");
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

  function repairPastedJson(raw) {
    try {
      return { ok: true, parsed: JSON.parse(raw), repairs: [] };
    } catch {
      // Deterministic cleanup
    }
    const repairs = [];
    let candidate = String(raw || "").trim();
    const fenceMatch = candidate.match(/```(?:json|JSON)?\s*\r?\n?([\s\S]*?)\r?\n?```/);
    if (fenceMatch && fenceMatch[1] && fenceMatch[1].includes("{")) {
      candidate = fenceMatch[1].trim();
      repairs.push("stripped markdown code fence");
    }
    const firstBrace = candidate.indexOf("{");
    const lastBrace = candidate.lastIndexOf("}");
    if (firstBrace !== -1 && lastBrace > firstBrace) {
      if (firstBrace > 0 || lastBrace < candidate.length - 1) {
        candidate = candidate.slice(firstBrace, lastBrace + 1);
        repairs.push("removed surrounding non-JSON text");
      }
    }
    let unescapedChanged = false;
    candidate = candidate.replace(/(\\+)([\[\]_*])/g, (full, slashes, ch) => {
      if (slashes.length % 2 === 1) {
        unescapedChanged = true;
        return slashes.slice(0, -1) + ch;
      }
      return full;
    });
    if (unescapedChanged) {
      repairs.push("removed stray markdown escapes (\\[, \\], \\_, \\*)");
    }
    try {
      const parsed = JSON.parse(candidate);
      return { ok: true, parsed, repairs };
    } catch (e) {
      let contextHint = "";
      const posMatch = String(e.message || "").match(/position\s+(\d+)/i);
      if (posMatch) {
        const pos = Number(posMatch[1]);
        const start = Math.max(0, pos - 24);
        const end = Math.min(candidate.length, pos + 24);
        contextHint = ` Near: ${candidate.slice(start, end)}`;
      }
      return { ok: false, error: `${e.message}${contextHint}` };
    }
  }

  async function runValidation() {
    jsonStatus.innerHTML = "";
    const raw = jsonInput.value.trim();
    if (!raw) {
      jsonStatus.innerHTML = '<div class="err">Paste a production JSON first.</div>';
      return null;
    }
    const rep = repairPastedJson(raw);
    if (!rep.ok) {
      jsonStatus.innerHTML = `<div class="err">JSON syntax error (even after auto-repair): ${escapeHtml(rep.error)}</div>`;
      return null;
    }
    const r = await api("/api/validate", {
      method: "POST",
      body: JSON.stringify({ production_json: rep.parsed }),
    });
    if (!r.ok || !r.data.valid) {
      const errs = (r.data.errors || ["Validation failed"]).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
      jsonStatus.innerHTML = `<div class="err"><strong>Validation errors:</strong><ul>${errs}</ul></div>`;
      return null;
    }
    const normalized = r.data.production_json || rep.parsed;
    const allNotices = [...rep.repairs, ...(r.data.notices || [])];
    const noticeHtml = allNotices.length
      ? `<div class="muted" style="margin-top:6px;"><strong>Note:</strong> ${allNotices.map((n) => escapeHtml(n)).join("; ")}.</div>`
      : "";
    const totalWords = (normalized.parts || []).reduce((acc, p) => {
      return acc + (p.lines || []).reduce((la, l) => la + String(l.text || "").split(/\s+/).filter(Boolean).length, 0);
    }, 0);
    const wpm = Number(wpmInput.value) || 250;
    const estSec = Math.round((totalWords / wpm) * 60);
    jsonStatus.innerHTML = `<div class="ok">Valid v${escapeHtml(normalized.schema_version || 3)} (${escapeHtml(normalized.format)}): "${escapeHtml(normalized.series_title)}" &mdash; ${normalized.parts.length} part(s), ${totalWords} words (~${estSec}s at ${wpm} WPM).${noticeHtml}</div>`;
    return normalized;
  }

  validateBtn.addEventListener("click", runValidation);

  // --- Render & Publishing Controls ---
  bgSelect.addEventListener("change", () => {
    bgOverridden = true;
    manualBgWrap.classList.toggle("hidden", bgSelect.value !== "manual");
  });

  document.querySelectorAll('input[name="publishMode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      const isSched = radio.value === "schedule" && radio.checked;
      scheduleWrap.classList.toggle("hidden", !isSched);
      if (isSched && !scheduleDtInput.value) {
        scheduleDtInput.value = getLagosInputDefault();
      }
    });
  });

  renderBtn.addEventListener("click", async () => {
    hideEl(renderErr);
    const parsed = await runValidation();
    if (!parsed) return;

    const pubMode = document.querySelector('input[name="publishMode"]:checked')?.value || "immediate";
    let scheduledForUtc = null;
    if (pubMode === "schedule") {
      scheduledForUtc = lagosLocalToUtcIso(scheduleDtInput.value);
      if (!scheduledForUtc) {
        showErr(renderErr, "Choose a valid schedule date & time.");
        return;
      }
    }

    const bgChoice = bgSelect.value;
    const bgMode = bgChoice === "sequential" || bgChoice === "manual" ? bgChoice : "specific";
    const bgClip = bgMode === "specific" ? bgChoice : null;

    renderBtn.disabled = true;
    try {
      const r = await api("/api/render", {
        method: "POST",
        body: JSON.stringify({
          production_json: parsed,
          voice_id: selectedVoiceId,
          voice_speed: speedInput.value.trim() || "+37%",
          target_wpm: Number(wpmInput.value) || 250,
          background_mode: bgMode,
          background_clip: bgClip,
          background_index: Number(bgIndexInput.value) || 0,
          publish_mode: pubMode,
          scheduled_for_utc: scheduledForUtc,
          timezone: "Africa/Lagos",
        }),
      });
      if (!r.ok) {
        showErr(renderErr, r.data.error || "Render request failed");
        return;
      }
      if (r.data.job?.task_id) openLogs.add(r.data.job.task_id);
      await loadJobs();
    } finally {
      renderBtn.disabled = false;
    }
  });

  // --- SF-004 / SF-005 / SF-009: Task Queue + Incremental Live Logs ---
  async function syncLogsForTask(taskId) {
    const existing = logCache.get(taskId) || { lastId: 0, entries: [] };
    const r = await api(`/api/task-logs?task_id=${encodeURIComponent(taskId)}&since_id=${existing.lastId}`);
    if (!r.ok || !Array.isArray(r.data.logs)) return existing.entries;
    if (r.data.logs.length > 0) {
      existing.entries.push(...r.data.logs);
      existing.lastId = r.data.logs[r.data.logs.length - 1].id;
      logCache.set(taskId, existing);
    }
    return existing.entries;
  }

  function renderLogEntriesHtml(taskId) {
    const entries = logCache.get(taskId)?.entries || [];
    if (!entries.length) return '<div class="log-box"><span class="muted" style="color:#a1a1aa;">No log lines yet.</span></div>';
    const lines = entries
      .map((l) => {
        const cls = l.level === "error" ? "log-line-error" : l.level === "warn" ? "log-line-warn" : "";
        const timeStr = (l.ts || "").slice(11, 19);
        const stStr = l.stage ? `[${l.stage}] ` : "";
        return `<div class="${cls}">${escapeHtml(timeStr)} ${escapeHtml(stStr)}${escapeHtml(l.message)}</div>`;
      })
      .join("");
    return `<div class="log-box" data-log-task="${escapeHtml(taskId)}">${lines}</div>`;
  }

  function renderTaskCard(j) {
    const isActive = ["queued", "running", "publishing", "uploading", "processing", "queued_publish", "scheduled"].includes(j.status);
    if (isActive) openLogs.add(j.task_id);
    const logsOpen = openLogs.has(j.task_id);
    const pct = Math.max(0, Math.min(100, Math.round(Number(j.progress_pct || 0))));

    let badgeCls = "badge";
    if (j.status === "done" || j.status === "published") badgeCls = "badge ok";
    else if (j.status === "failed" || j.status === "publish_failed" || j.status === "delete_failed") badgeCls = "badge err";
    else if (["waiting_for_keys", "publish_failed_partial", "publish_skipped", "publish_unconfirmed", "cleanup_pending", "deleting"].includes(j.status)) badgeCls = "badge warn";

    const modeBits = [];
    if (j.kind === "render") {
      modeBits.push(`Publish: ${j.publish_mode || "immediate"}`);
      if (j.scheduled_for_utc) modeBits.push(`Target: ${fmtLagosTime(j.scheduled_for_utc)}`);
      if (j.dry_run) modeBits.push("DRY RUN");
      if (j.mock_mode) modeBits.push("MOCK");
    }

    const targetsByPart = new Map();
    for (const t of j.publish_targets || []) {
      const arr = targetsByPart.get(t.part_number) || [];
      arr.push(t);
      targetsByPart.set(t.part_number, arr);
    }

    const partsHtml = (j.parts || [])
      .map((p) => {
        const dl = `/api/download?job_id=${encodeURIComponent(j.task_id)}&part=${p.part_number}`;
        const partTargets = targetsByPart.get(p.part_number) || [];
        const targetsHtml = partTargets.length
          ? `
            <table>
              <thead><tr><th>Platform</th><th>Account</th><th>Status</th><th>Details / Link</th></tr></thead>
              <tbody>
                ${partTargets
                  .map((t) => {
                    let sBadge = "badge";
                    if (t.status === "published") sBadge = "badge ok";
                    else if (t.status === "failed") sBadge = "badge err";
                    else if (t.status === "held" || t.status === "skipped") sBadge = "badge warn";
                    const details = t.platform_post_url
                      ? `<a href="${escapeHtml(t.platform_post_url)}" target="_blank" rel="noopener">${escapeHtml(t.platform_post_url)}</a>`
                      : t.error
                      ? `<span style="color:#b91c1c;">${escapeHtml(t.error)}</span>`
                      : t.next_eligible_at
                      ? `Eligible: ${escapeHtml(fmtLagosTime(t.next_eligible_at))}`
                      : "";
                    const dryHtml = t.payload_preview
                      ? `<details style="margin-top:4px;"><summary class="muted" style="cursor:pointer;">Dry-run payload</summary><pre>${escapeHtml(t.payload_preview)}</pre></details>`
                      : "";
                    return `
                      <tr>
                        <td><strong>${escapeHtml(t.platform)}</strong></td>
                        <td>${escapeHtml(t.account_name)}</td>
                        <td><span class="${sBadge}">${escapeHtml(t.status)}</span></td>
                        <td>${details}${dryHtml}</td>
                      </tr>
                    `;
                  })
                  .join("")}
              </tbody>
            </table>
          `
          : "";

        const canDownload = p.render_status === "done" && p.filename && j.status !== "published";
        const attrText = p.attribution ? `<details style="margin-top:4px;"><summary class="muted" style="cursor:pointer;">Attribution</summary><pre>${escapeHtml(p.attribution)}</pre></details>` : "";
        return `
          <div style="margin-top:10px; padding-top:10px; border-top:1px solid #e4e4e7;">
            <div class="row" style="justify-content:space-between; margin-top:0;">
              <div>
                <strong>Part ${p.part_number}:</strong> ${escapeHtml(p.title || "")}
                <span class="muted">(${Math.round(p.duration_sec || 0)}s${p.size_bytes ? `, ${fmtBytes(p.size_bytes)}` : ""}${p.background_clip ? `, bg: ${escapeHtml(p.background_clip)}` : ""})</span>
              </div>
              <div>
                <span class="badge">Render: ${escapeHtml(p.render_status)}</span>
                <span class="badge">Publish: ${escapeHtml(p.publish_status)}</span>
              </div>
            </div>
            ${p.planned_publish_at && p.publish_status !== "published" ? `<div class="muted">Planned publish: ${escapeHtml(fmtLagosTime(p.planned_publish_at))}</div>` : ""}
            ${p.published_at ? `<div class="muted">Live at: ${escapeHtml(fmtLagosTime(p.published_at))}</div>` : ""}
            ${canDownload ? `<div class="row"><a href="${dl}" download="${escapeHtml(p.filename)}"><button type="button" class="secondary">Download Part ${p.part_number} MP4</button></a></div>` : ""}
            ${targetsHtml}
            ${attrText}
          </div>
        `;
      })
      .join("");

    const hasFailedTarget = (j.publish_targets || []).some((t) => t.status === "failed");
    const retryBtn = hasFailedTarget
      ? `<button type="button" class="retry-pub-btn" data-id="${escapeHtml(j.task_id)}">Retry Failed Accounts</button>`
      : "";

    const isDeleting = deletingTasks.has(j.task_id) || j.status === "deleting";
    const deleteLabel = isDeleting ? "Deleting..." : j.status === "delete_failed" ? "Retry Delete" : "Delete";
    const deleteBtn = `<button type="button" class="danger delete-task-btn" data-id="${escapeHtml(j.task_id)}"${isDeleting ? " disabled" : ""}>${deleteLabel}</button>`;

    const errHtml = j.error ? `<div class="err">${escapeHtml(j.error)}</div>` : "";
    const waitHint =
      j.status === "waiting_for_keys"
        ? `<div class="warn">Rendered videos are ready. Add a Zernio API key in Settings to start publishing automatically.</div>`
        : "";

    return `
      <div class="card" style="margin-bottom:10px;">
        <div class="row" style="justify-content:space-between; margin-top:0;">
          <div>
            <strong>${escapeHtml(j.series_title || j.task_id)}</strong>
            <span class="${badgeCls}">${escapeHtml(j.status)}</span>
          </div>
          <div class="row" style="margin-top:0;">
            ${j.status === 'failed' && j.voice_id?.startsWith('vs-') ? '<button type="button" class="secondary retry-edge-btn" data-id="' + escapeHtml(j.task_id) + '">Re-run with Edge TTS</button>' : ''}
            ${retryBtn}
            <button type="button" class="secondary toggle-logs-btn" data-id="${escapeHtml(j.task_id)}">${logsOpen ? "Hide logs" : "Show logs"}</button>
            ${deleteBtn}
          </div>
        </div>
        <div class="muted" style="margin-top:4px;">
          Task: <span class="mono">${escapeHtml(j.task_id)}</span>
          ${j.voice_id ? ` | Voice: ${escapeHtml(j.voice_id)} (${escapeHtml(j.voice_speed || "")})` : ""}
          ${modeBits.length ? ` | ${escapeHtml(modeBits.join(" · "))}` : ""}
          | Created: ${escapeHtml(fmtLagosTime(j.created_at))}
        </div>
        <div style="margin-top:8px;">
          <div class="row" style="justify-content:space-between; margin-top:0; margin-bottom:2px;">
            <span class="muted">Stage: <strong>${escapeHtml(j.current_stage || j.status)}</strong></span>
            <span class="muted">${pct}%</span>
          </div>
          <progress value="${pct}" max="100"></progress>
        </div>
        ${errHtml}
        ${waitHint}
        ${logsOpen ? renderLogEntriesHtml(j.task_id) : ""}
        ${partsHtml}
      </div>
    `;
  }

  function buildDeleteConfirmHtml(task) {
    const title = task.series_title || task.task_id;
    const parts = task.parts || [];
    const targets = task.publish_targets || [];
    const isSeries = parts.length > 1;
    const st = String(task.status || "");

    const bullets = [];
    if (isSeries) {
      bullets.push(`This is a ${parts.length}-part series. Deleting it removes all ${parts.length} parts.`);
    }

    if (st === "queued" || st === "running") {
      bullets.push("The running/queued GitHub Actions workflow will be cancelled and live logging will stop.");
      bullets.push("Any rendered video or thumbnail files already uploaded for this task will be deleted.");
    } else if (st === "queued_publish" || st === "scheduled" || st === "waiting_for_keys") {
      bullets.push("It will be removed from the queue/schedule, including any not-yet-sent Part 2.");
      bullets.push("Rendered video and thumbnail files for this task will be deleted. Other tasks' cadence and series spacing will keep working.");
    } else if (
      ["publishing", "uploading", "processing", "published", "publish_failed_partial", "publish_skipped", "publish_unconfirmed", "cleanup_pending"].includes(st)
    ) {
      const liveTargets = targets.filter((t) => t.status === "published" && !task.dry_run);
      const pendingTargets = targets.filter((t) =>
        ["pending", "scheduled", "publishing", "uploading", "processing"].includes(t.status)
      );
      if (pendingTargets.length > 0) {
        bullets.push("Not-yet-live or scheduled Zernio posts (and any not-yet-sent Part 2) will be cancelled if supported by Zernio.");
      }
      if (liveTargets.length > 0) {
        const liveList = liveTargets
          .map((t) => `Part ${t.part_number} on ${t.platform} (${t.account_name})`)
          .join(", ");
        bullets.push(`Already live and CANNOT be taken back: ${liveList}.`);
      }
      bullets.push("The task record, logs, and rendered files in the renders Release will be removed.");
    } else if (st === "delete_failed") {
      bullets.push("A previous delete step failed. Retrying will clean up any remaining workflow runs, Zernio posts, render files, and task records.");
    } else {
      bullets.push("The task record, logs, and any rendered video or thumbnail files in the renders Release will be permanently removed.");
    }

    const listHtml = bullets.map((b) => `<li style="margin-bottom:6px;">${escapeHtml(b)}</li>`).join("");
    return `
      <p style="margin:0 0 8px;">Delete <strong>${escapeHtml(title)}</strong> (<span class="mono">${escapeHtml(task.task_id)}</span>, status: <strong>${escapeHtml(st)}</strong>)?</p>
      <ul style="margin:0 0 8px; padding-left:18px;">${listHtml}</ul>
      <p class="muted" style="margin:0;">This cannot be undone.</p>
    `;
  }

  function openDeleteConfirm(taskId) {
    const task = lastTasksById.get(taskId);
    if (!task || deletingTasks.has(taskId)) return;
    pendingDeleteTaskId = taskId;
    deleteConfirmTitle.textContent = task.status === "delete_failed" ? "Retry Task Deletion?" : "Delete Task?";
    deleteConfirmBody.innerHTML = buildDeleteConfirmHtml(task);
    deleteConfirmBtn.disabled = false;
    deleteConfirmBtn.textContent = task.status === "delete_failed" ? "Retry Delete" : "Delete permanently";
    deleteConfirmModal.classList.remove("hidden");
  }

  function closeDeleteConfirm() {
    pendingDeleteTaskId = null;
    deleteConfirmModal.classList.add("hidden");
  }

  deleteCancelBtn.addEventListener("click", closeDeleteConfirm);
  deleteConfirmModal.addEventListener("click", (e) => {
    if (e.target === deleteConfirmModal && !deleteConfirmBtn.disabled) {
      closeDeleteConfirm();
    }
  });

  deleteConfirmBtn.addEventListener("click", async () => {
    const taskId = pendingDeleteTaskId;
    if (!taskId || deletingTasks.has(taskId)) return;
    const task = lastTasksById.get(taskId);
    const title = task?.series_title || taskId;

    deletingTasks.add(taskId);
    deleteConfirmBtn.disabled = true;
    deleteConfirmBtn.textContent = "Deleting...";
    openLogs.delete(taskId);
    logCache.delete(taskId);

    try {
      const r = await api("/api/tasks/delete", {
        method: "POST",
        body: JSON.stringify({ task_id: taskId }),
      });
      closeDeleteConfirm();

      if (r.ok) {
        const d = r.data || {};
        const notes = [];
        if (d.already_deleted) {
          notes.push(`Task "${title}" was already deleted.`);
        } else {
          notes.push(`Deleted "${title}".`);
          if (Array.isArray(d.cancelled_runs) && d.cancelled_runs.length) {
            notes.push(`Cancelled workflow run(s): #${d.cancelled_runs.join(", #")}.`);
          }
          if (Array.isArray(d.cancelled_zernio_posts) && d.cancelled_zernio_posts.length) {
            notes.push(`Cancelled ${d.cancelled_zernio_posts.length} Zernio post(s).`);
          }
          if (Array.isArray(d.deleted_assets) && d.deleted_assets.length) {
            notes.push(`Removed ${d.deleted_assets.length} render file(s).`);
          }
        }
        const live = Array.isArray(d.already_live_posts) ? d.already_live_posts : [];
        if (live.length > 0) {
          const liveSummary = live
            .map((p) => `Part ${p.part_number} on ${p.platform} (${p.account_name})`)
            .join(", ");
          notes.push(`Already live (cannot be taken back): ${liveSummary}.`);
          taskDeleteNotice.className = "warn";
        } else {
          taskDeleteNotice.className = "ok";
        }
        taskDeleteNotice.textContent = notes.join(" ");
        taskDeleteNotice.classList.remove("hidden");
      } else {
        const failures = Array.isArray(r.data?.failures) && r.data.failures.length
          ? r.data.failures.join("; ")
          : r.data?.error || "Delete failed";
        taskDeleteNotice.className = "err";
        taskDeleteNotice.textContent = `Could not finish deleting "${title}": ${failures}. Tap "Retry Delete" on the task to try again.`;
        taskDeleteNotice.classList.remove("hidden");
      }
      await loadJobs();
    } finally {
      deletingTasks.delete(taskId);
      deleteConfirmBtn.disabled = false;
    }
  });

  function wireTaskButtons(container) {
    container.querySelectorAll(".toggle-logs-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.id;
        if (openLogs.has(id)) {
          openLogs.delete(id);
        } else {
          openLogs.add(id);
          await syncLogsForTask(id);
        }
        await loadJobs();
      });
    });
    container.querySelectorAll(".retry-pub-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        await api("/api/tasks/retry-publish", {
          method: "POST",
          body: JSON.stringify({ task_id: btn.dataset.id }),
        });
        await loadJobs();
      });
    });
    container.querySelectorAll('.retry-edge-btn').forEach(btn => btn.addEventListener('click', async () => {
      btn.disabled = true;
      const r = await api('/api/render', {method:'POST', body:JSON.stringify({retry_edge_task:btn.dataset.id})});
      if (!r.ok) { btn.textContent = r.data.error || 'Retry failed'; btn.disabled = false; return; }
      await loadJobs();
    }));
    container.querySelectorAll(".delete-task-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        openDeleteConfirm(btn.dataset.id);
      });
    });
    container.querySelectorAll(".log-box").forEach((box) => {
      box.scrollTop = box.scrollHeight;
    });
  }

  async function loadJobs() {
    const r = await api("/api/jobs");
    if (!r.ok) return;
    nextBgHint.textContent = `Next sequential background clip #${r.data.next_background_index ?? 0}`;
    const allTasks = r.data.tasks || r.data.jobs || [];
    lastTasksById.clear();
    for (const t of allTasks) lastTasksById.set(t.task_id, t);

    // Drop stale openLogs/logCache for tasks that no longer exist
    for (const id of Array.from(openLogs)) {
      if (!lastTasksById.has(id)) {
        openLogs.delete(id);
        logCache.delete(id);
      }
    }

    // Fetch incremental logs for all active or user-opened tasks (never for deleting tasks)
    const tasksToFetchLogs = allTasks.filter((t) => {
      if (deletingTasks.has(t.task_id) || t.status === "deleting") return false;
      const active = ["queued", "running", "publishing", "uploading", "processing", "queued_publish", "scheduled"].includes(t.status);
      return active || openLogs.has(t.task_id);
    });
    await Promise.all(tasksToFetchLogs.map((t) => syncLogsForTask(t.task_id)));

    const renderTasks = allTasks.filter((t) => t.kind !== "bg_import");
    const bgTasks = allTasks.filter((t) => t.kind === "bg_import");

    // Auto-refresh background library if a bg_import finished since last poll
    const doneId = bgTasks.find((t) => t.status === "done")?.task_id || "";
    const updatedLibrary = lastSeenBgImportDoneId !== null && doneId && doneId !== lastSeenBgImportDoneId;
    lastSeenBgImportDoneId = doneId;
    if (updatedLibrary) await loadBackgrounds();

    if (!renderTasks.length) {
      jobsList.innerHTML = '<div class="muted">No render tasks yet. (Previously published videos are automatically cleaned up once live on all accounts.)</div>';
    } else {
      jobsList.innerHTML = renderTasks.map(renderTaskCard).join("");
      wireTaskButtons(jobsList);
    }

    if (bgTasks.length) {
      bgImportTasksWrap.classList.remove("hidden");
      bgImportTasksWrap.innerHTML = `<h3>Recent Background Imports / Normalizations</h3>` + bgTasks.slice(0, 5).map(renderTaskCard).join("");
      wireTaskButtons(bgImportTasksWrap);
    } else {
      bgImportTasksWrap.classList.add("hidden");
      bgImportTasksWrap.innerHTML = "";
    }

    const hasActive = allTasks.some((t) =>
      ["queued", "running", "publishing", "uploading", "processing"].includes(t.status)
    );
    if (hasActive && !pollTimer) {
      pollTimer = setInterval(loadJobs, 3500);
    } else if (!hasActive && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  refreshJobsBtn.addEventListener("click", loadJobs);
  runTickBtn.addEventListener("click", async () => {
    runTickBtn.disabled = true;
    tickStatus.textContent = "Running publishing tick...";
    try {
      const r = await api("/api/tasks/publish-tick", { method: "POST" });
      if (r.ok) {
        tickStatus.textContent = `Checked ${r.data.checked} task(s), cleaned ${r.data.cleaned}.`;
      } else {
        tickStatus.textContent = r.data.error || "Tick failed";
      }
      await loadJobs();
    } finally {
      runTickBtn.disabled = false;
    }
  });

  checkAuth();
})();
