'use strict';

let _stats = null;
let _statusInterval = null;
let _selectedFileId = null;
let _dlHistory = [];
let _serverDownloads = [];
let _groups = [];
let _currentLinks = [];
let _currentFiles = [];
let _nameEditGroupId = null;
let _syncStatusCache = {};

const S = {
  activeGroupId: null,
  activeTab: 'files',
  sortBy: 'date', sortDir: 'desc',
  offset: 0, limit: 100,
  extChip: '', typeGroup: '',
  sizeMinMB: null, sizeMaxMB: null, sliderMax: 0,
  linkOffset: 0, linkLimit: 100,
  linkSortBy: 'date', linkSortDir: 'desc',
  dlQueue: {}, polls: {},
  selectedFiles: new Set(),
  colGroupIds: new Set(),
  fileIdsFilter: null,        // null or Set<int> — when set, restricts grid to these files
  fileIdsFilterLabel: null,   // human-readable label for the chip
  fileIdsFilterNotifId: null, // id of the notification that set the filter

  selectedLinks: new Set(),
  selectedGroups: new Set(),
  selectedDownloads: new Set(),
  hunterSelected: new Set(),
  hunterLimit: parseInt(localStorage.getItem('tf_hunter_limit') || '200', 10),
  hunterOffset: 0,
  hunterTotal: 0,
  dlSortBy: 'downloaded_at', dlSortDir: 'desc',
  showHidden: false,
  groupSort: 'count',
  groupSortDir: 'desc',
};

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  applySavedTheme();
  uiAuthBoot();
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      const el = document.getElementById('col-name');
      if (el) el.focus();
    }
    if (e.key === 'Escape') {
      const cn = document.getElementById('col-name');
      if (cn && document.activeElement === cn) { cn.value = ''; debouncedLoad(); }
    }
  });
  document.getElementById('table-wrap').addEventListener('mousemove', onCtxMove);
  document.getElementById('table-wrap').addEventListener('mouseleave', () => {
    document.getElementById('ctx-tip').style.display = 'none';
  });
});

// ── UI password gate (greeter) ───────────────────────────────────────────────
async function uiAuthBoot() {
  show('loading-screen');
  let r;
  try {
    const res = await fetch('/api/uiauth/check');
    r = await res.json();
  } catch (e) {
    r = { authenticated: false };
  }
  hide('loading-screen');
  if (r.authenticated) {
    checkAuth();   // fall through to the existing Telegram-auth check
  } else {
    show('ui-greeter');
    setTimeout(() => document.getElementById('ug-pass')?.focus(), 30);
  }
}

async function uiAuthLogin() {
  const pw = document.getElementById('ug-pass').value;
  const remember = !!document.getElementById('ug-remember').checked;
  const msg = document.getElementById('ug-msg');
  msg.textContent = '';
  try {
    await api('/api/uiauth/login', { method: 'POST', json: { password: pw, remember } });
  } catch (e) {
    msg.textContent = e.message || t('pw.wrongPass');
    return;
  }
  document.getElementById('ug-pass').value = '';
  hide('ui-greeter');
  checkAuth();
}

async function uiAuthLogout() {
  if (!confirm(t('pw.confirmLogout'))) return;
  try { await fetch('/api/uiauth/logout', { method: 'POST', credentials: 'same-origin' }); }
  catch (e) {}
  location.reload();
}

async function uiAuthChangePassword() {
  const cur = document.getElementById('ui-cur-pw').value;
  const nw  = document.getElementById('ui-new-pw').value;
  if (!nw) { showToast(t('pw.emptyNew'), 3000); return; }
  try {
    await api('/api/uiauth/change-password', {
      method: 'POST',
      json: { current_password: cur, new_password: nw },
    });
  } catch (e) {
    showToast(t('pw.changeFail') + ' ' + esc(e.message), 4000);
    return;
  }
  showToast(t('pw.changeOk'), 2500);
  setTimeout(() => location.reload(), 1500);
}

// Refresh the "Mevcut: varsayılan / özel" indicator on the password card
async function _refreshUiPwState() {
  const el = document.getElementById('ui-pw-state');
  if (!el) return;
  try {
    const r = await fetch('/api/uiauth/check').then(r => r.json());
    el.textContent = r.default_password ? t('pw.statDefault') : t('pw.statCustom');
  } catch (e) { /* leave as-is */ }
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function setTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  try { localStorage.setItem('theme', name); } catch(e){}
  document.querySelectorAll('.theme-card').forEach(c =>
    c.classList.toggle('active', c.dataset.theme === name)
  );
}

function applySavedTheme() {
  let saved = 'light';
  try { saved = localStorage.getItem('theme') || 'light'; } catch(e){}
  document.documentElement.setAttribute('data-theme', saved);
  document.querySelectorAll('.theme-card').forEach(c =>
    c.classList.toggle('active', c.dataset.theme === saved)
  );
}

function show(id) { document.getElementById(id).style.display = 'flex'; }
function hide(id) { document.getElementById(id).style.display = 'none'; }

const _LANG_LOCALE = { tr: 'tr-TR', en: 'en-US', de: 'de-DE', ru: 'ru-RU', zh: 'zh-CN' };
function _locale() { return _LANG_LOCALE[getLang()] || 'en-US'; }

// Kullanıcı greeter'da "Şimdi geç" dediyse, sonraki yenilemelerde aynı
// formu tekrar göstermek için bir localStorage flag'i kullanıyoruz.
// Kimlik bilgileri girilince temizleniyor.
function _credsSkipped() {
  try { return localStorage.getItem('tf_creds_skipped') === '1'; } catch (e) { return false; }
}
function _setCredsSkipped(v) {
  try {
    if (v) localStorage.setItem('tf_creds_skipped', '1');
    else   localStorage.removeItem('tf_creds_skipped');
  } catch (e) {}
}

async function checkAuth() {
  const r = await api('/api/auth/status');
  hide('loading-screen');
  if (r.authorized) { showApp(); return; }
  // Kullanıcı daha önce credentials adımını bilinçli olarak geçtiyse,
  // her yenilemede aynı engelle karşılaşmasın — doğrudan uygulamaya bırak.
  // İstedikleri zaman Ayarlar → Hesap üzerinden ekleyebilirler.
  if (_credsSkipped()) { showApp(); return; }
  startLoginForAccount(1, null);   // creds eksikse "creds" adımı,
                                   // varsa "phone" adımı gösterilir.
}

async function showApp() {
  show('app-shell');
  applySavedTheme();
  initColResize();
  await loadGroups();
  fetchStats();
  loadFiles();
  loadActiveNotifications();
  pollSync();
  setInterval(pollSync, 5000);
  setInterval(updateNextSyncDisplay, 1000);
  setInterval(loadActiveNotifications, 30000);
  // Açılışta bir kez, sonra 6 saatte bir GitHub'da yeni sürüm var mı bakar.
  setTimeout(checkForUpdate, 4000);
  setInterval(checkForUpdate, 6 * 3600 * 1000);
}

async function checkForUpdate() {
  try {
    const v = await api('/api/version');
    if (!v || !v.update_available || !v.latest || !v.latest.commit) return;
    // Kullanıcı bu sürüm için "Daha sonra" derse o commit'e kadar sus.
    const dismissed = localStorage.getItem('tf_update_dismissed_commit');
    if (dismissed === v.latest.commit) return;
    showUpdateBanner(v);
  } catch (e) { /* sessizce geç (offline / rate-limit) */ }
}

function showUpdateBanner(v) {
  const existing = document.getElementById('tf-update-banner');
  if (existing) existing.remove();
  const short = (s) => (s || '').substring(0, 7);
  const localShort  = short(v.local && v.local.commit) || '?';
  const latestShort = short(v.latest && v.latest.commit);
  const cmd = v.install_cmd || '';
  const el = document.createElement('div');
  el.id = 'tf-update-banner';
  el.innerHTML = `
    <div class="tfu-row">
      <div class="tfu-msg">
        <b>${esc(t('update.newVersion'))}</b>
        <span class="tfu-dim">${esc(t('update.installed'))} <code>${esc(localShort)}</code> · GitHub: <code>${esc(latestShort)}</code></span>
      </div>
      <div class="tfu-actions">
        <button class="tfu-btn" id="tfu-copy">${esc(t('update.copyBtn'))}</button>
        <button class="tfu-link" id="tfu-later">${esc(t('update.later'))}</button>
      </div>
    </div>
    <div class="tfu-cmd"><code>${esc(cmd)}</code></div>
  `;
  document.body.appendChild(el);
  document.getElementById('tfu-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(cmd); showToast(t('update.cmdCopied')); }
    catch { showToast(t('update.copyFail')); }
  });
  document.getElementById('tfu-later').addEventListener('click', () => {
    localStorage.setItem('tf_update_dismissed_commit', v.latest.commit);
    el.remove();
  });
}

async function fetchStats() {
  _stats = await api('/api/stats');
  const el24 = document.getElementById('ts-24h');
  const el7d = document.getElementById('ts-7d');
  const elAll = document.getElementById('ts-all');
  const sz = (n,b) => `${(n||0).toLocaleString()} <span class="ts-sz">${fmtSize(b||0)}</span>`;
  if (el24) el24.innerHTML = sz(_stats.recent_24h, _stats.recent_24h_size);
  if (el7d) el7d.innerHTML = sz(_stats.recent_7d,  _stats.recent_7d_size);
  if (elAll) elAll.innerHTML = sz(_stats.total_files, _stats.total_size);
}

function _initColResize(tableId, storeKey) {
  // Widths are stored as fractions of the table width so proportions survive
  // a viewport resize. Each table needs its own storage key so the two grids
  // don't share / overwrite each other.
  const table = document.getElementById(tableId);
  if (!table) return;
  const ths = [...table.querySelectorAll('thead tr:first-child th:not(.chk-cell)')];
  if (!ths.length) return;

  // Apply saved widths once on init
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storeKey) || 'null'); }
  catch (e) { saved = null; }
  if (saved) {
    ths.forEach((th, i) => {
      if (saved[i] != null) th.style.width = (saved[i] * 100).toFixed(3) + '%';
    });
  }

  const persistAndNormalize = () => {
    const tableW = table.clientWidth || 1;
    const w = {};
    ths.forEach((t, j) => { w[j] = t.offsetWidth / tableW; });
    try { localStorage.setItem(storeKey, JSON.stringify(w)); } catch (e) {}
    // Re-express every column as a percentage so window resizes keep the ratio
    ths.forEach((t, j) => { t.style.width = (w[j] * 100).toFixed(3) + '%'; });
  };

  ths.forEach((th) => {
    // Don't double-attach if init runs twice (e.g. tab swaps that re-render)
    if (th.querySelector(':scope > .col-resize-handle')) return;
    const h = document.createElement('div');
    h.className = 'col-resize-handle';
    th.appendChild(h);
    let x0, w0;
    h.addEventListener('mousedown', e => {
      x0 = e.clientX; w0 = th.offsetWidth;
      h.classList.add('active');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      const onMove = e => { th.style.width = Math.max(36, w0 + e.clientX - x0) + 'px'; };
      const onUp   = () => {
        h.classList.remove('active');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        persistAndNormalize();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      e.preventDefault(); e.stopPropagation();
    });
  });
}

function initColResize() {
  _initColResize('files-table', 'colWidthFrac_v4');
  _initColResize('links-table', 'linkColWidthFrac_v1');
}

// ── Auth ──────────────────────────────────────────────────────────────────────
let _loginAccountId = 1;       // which account the login wizard is currently targeting
let _loginReturnTo = null;     // null = come from boot screen; 'settings' = come from accounts panel

async function startLoginForAccount(accountId, returnTo) {
  _loginAccountId = accountId || 1;
  _loginReturnTo = returnTo || null;
  hide('app-shell');
  show('login-screen');
  loginMsg('');
  document.getElementById('inp-phone').value = '';
  document.getElementById('inp-code').value = '';
  document.getElementById('inp-pass').value = '';
  // Eğer bu hesap için API_ID/HASH henüz tanımlı değilse — örneğin kurulum
  // sırasında .env boş bırakılmışsa — telefon adımı yerine önce credentials
  // formu göster. Kullanıcı buradan girdiğinde backend account'u yaratır
  // veya günceller, sonra normal telefon akışına geçilir.
  try {
    const r = await api('/api/credentials?account_id=' + (_loginAccountId || 1));
    if (!r || !r.configured) {
      document.getElementById('inp-api-id').value = '';
      document.getElementById('inp-api-hash').value = '';
      showStep('creds');
      setTimeout(() => document.getElementById('inp-api-id')?.focus(), 30);
      return;
    }
  } catch (e) { /* credentials endpoint düştüyse normal akışa düş */ }
  showStep('phone');
  setTimeout(() => document.getElementById('inp-phone')?.focus(), 30);
}

async function authSaveCreds() {
  const idStr   = document.getElementById('inp-api-id').value.trim();
  const hashStr = document.getElementById('inp-api-hash').value.trim();
  const apiId = parseInt(idStr, 10);
  if (!apiId || !hashStr) { loginMsg(t('creds.apiRequired')); return; }
  try {
    await api('/api/credentials', { method: 'POST', json: {
      api_id: apiId, api_hash: hashStr, account_id: _loginAccountId || 1
    }});
  } catch (e) { loginMsg(t('creds.saveFail') + ' ' + e.message); return; }
  _setCredsSkipped(false);
  loginMsg('');
  showStep('phone');
  setTimeout(() => document.getElementById('inp-phone')?.focus(), 30);
}

// "Şimdi geç" — kullanıcı credentials adımını atlayıp uygulamaya doğrudan
// girmek isterse. Hesap henüz tanımlı olmadığı için Telegram bağlantısı
// gerektiren her özellik boş çalışır; kullanıcı Ayarlar → Hesap üzerinden
// API bilgilerini girip sonra giriş akışını oradan başlatır. localStorage
// flag'i sayesinde sonraki yenilemelerde aynı form tekrar açılmaz.
function authSkipCreds() {
  _setCredsSkipped(true);
  loginMsg('');
  hide('login-screen');
  show('app-shell');
  showApp().then(() => {
    try {
      switchTab('settings');
      switchSettingsTab('account');
    } catch (e) { /* tab switcher henüz hazır değilse sessizce geç */ }
    showToast(t('creds.needApiKey'), 6000);
  });
}

async function authSendCode() {
  const phone = document.getElementById('inp-phone').value.trim();
  if (!phone) return;
  try {
    await api('/api/auth/send-code', {method:'POST',json:{phone, account_id: _loginAccountId}});
    showStep('code');
  } catch(e) { loginMsg(e.message); }
}
async function authVerifyCode() {
  const phone = document.getElementById('inp-phone').value.trim();
  const code  = document.getElementById('inp-code').value.trim();
  try {
    const r = await api('/api/auth/verify-code', {method:'POST',json:{phone, code, account_id: _loginAccountId}});
    if (r.needs_2fa) { showStep('2fa'); return; }
    _afterLogin();
  } catch(e) { loginMsg(e.message); }
}
async function authVerifyPass() {
  const password = document.getElementById('inp-pass').value;
  try {
    await api('/api/auth/verify-password', {method:'POST',json:{password, account_id: _loginAccountId}});
    _afterLogin();
  } catch(e) { loginMsg(e.message); }
}

function _afterLogin() {
  hide('login-screen');
  if (_loginReturnTo === 'settings') {
    show('app-shell');
    switchTab('settings');
    switchSettingsTab('account');
    loadAccountsList();
  } else {
    showApp();
  }
}
function showStep(s) {
  ['creds','phone','code','2fa'].forEach(x =>
    document.getElementById('login-step-'+x).classList.toggle('active', x===s));
}
function loginMsg(m) { document.getElementById('login-msg').textContent = m; }

// ── Credentials ───────────────────────────────────────────────────────────────
async function loadCredentials() {
  try {
    const r = await api('/api/credentials');
    const idEl   = document.getElementById('creds-cur-id');
    const hashEl = document.getElementById('creds-cur-hash');
    const idInp  = document.getElementById('creds-api-id');
    const hashInp = document.getElementById('creds-api-hash');
    if (idEl)   idEl.textContent   = r.api_id ? r.api_id : t('creds.notDefined');
    if (hashEl) hashEl.textContent = r.api_hash_masked || '—';
    // Prefill the inputs only if the user hasn't already started editing
    if (idInp   && r.api_id   != null && document.activeElement !== idInp   && !idInp.value)   idInp.value   = r.api_id;
    if (hashInp && r.api_hash != null && document.activeElement !== hashInp && !hashInp.value) hashInp.value = r.api_hash;
  } catch (e) { /* ignore */ }
}

async function saveCredentials() {
  const idStr   = document.getElementById('creds-api-id').value.trim();
  const hashStr = document.getElementById('creds-api-hash').value.trim();
  const apiId = parseInt(idStr, 10);
  if (!apiId || !hashStr) {
    showToast(t('creds.apiRequired'));
    return;
  }
  if (!confirm(t('creds.saveConfirm'))) return;
  try {
    await api('/api/credentials', { method: 'POST', json: { api_id: apiId, api_hash: hashStr } });
  } catch (e) {
    showToast(t('creds.saveFail') + ' ' + esc(e.message));
    return;
  }
  _setCredsSkipped(false);
  document.getElementById('creds-api-id').value = '';
  document.getElementById('creds-api-hash').value = '';
  showToast(t('creds.saveOk'), 2500);
  setTimeout(() => location.reload(), 1200);
}

async function logoutAccount() {
  if (!confirm(t('creds.logoutConfirm'))) return;
  try {
    await api('/api/auth/logout', { method: 'POST' });
  } catch (e) {
    showToast(t('creds.logoutFail') + ' ' + esc(e.message));
    return;
  }
  showToast(t('creds.logoutOk'), 2500);
  setTimeout(() => location.reload(), 1200);
}

// ── Sync interval ─────────────────────────────────────────────────────────────
function _fmtInterval(sec) {
  if (sec >= 86400) return t('fmt.days',    { n: Math.round(sec / 86400) });
  if (sec >= 3600)  return t('fmt.hours',   { n: Math.round(sec / 3600) });
  return                       t('fmt.minutes', { n: Math.round(sec / 60) });
}

async function loadSyncInterval() {
  let r;
  try { r = await api('/api/settings'); }
  catch (e) { return; }
  const cur = document.getElementById('sync-int-cur');
  const next = document.getElementById('sync-int-next');
  if (cur)  cur.textContent = _fmtInterval(r.sync_interval_seconds);
  if (next) {
    next.textContent = r.next_sync_at
      ? new Date(r.next_sync_at * 1000).toLocaleString(getLang())
      : '—';
  }
  document.querySelectorAll('#sync-int-presets .sync-preset').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.secs, 10) === r.sync_interval_seconds);
    b.onclick = () => saveSyncInterval(parseInt(b.dataset.secs, 10));
  });
}

async function saveSyncInterval(secs) {
  try {
    await api('/api/settings', { method: 'PUT', json: { sync_interval_seconds: secs } });
    showToast(t('sync.intervalSaved', { interval: _fmtInterval(secs) }), 2500);
    loadSyncInterval();
    pollSync();
  } catch (e) {
    showToast(t('sync.intervalFail') + ' ' + esc(e.message), 4000);
  }
}

// ── Sync ──────────────────────────────────────────────────────────────────────
async function startSync() {
  const btn = document.getElementById('pg-sync-btn');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = t('sync.starting');
  try {
    await api('/api/sync/start', {method:'POST'});
  } catch(e) {
    console.error('Sync start error:', e);
    btn.disabled = false;
    btn.textContent = t('sync.scan');
    return;
  }
  pollSync();
  setTimeout(pollSync, 1500);
}

let _wasRunning = false;
async function pollSync() {
  try {
    const s = await api('/api/sync/status');
    _syncStatusCache = s;
    applySyncStatusToUI();
    if (s.running) {
      if (S.activeTab === 'files') loadFiles(true);
      else if (S.activeTab === 'links') loadLinks(true);
      _wasRunning = true;
    } else if (_wasRunning) {
      _wasRunning = false;
      // Sync just finished — refresh notifications
      loadActiveNotifications();
    }
  } catch(e) { /* silent */ }
  fetchStats();
}

function applySyncStatusToUI() {
  const s = _syncStatusCache;
  const syncEl = document.getElementById('pag-sync');
  const btnEl  = document.getElementById('pg-sync-btn');
  const grpEl  = document.getElementById('pag-current-group');
  if (!syncEl) return;

  if (s && s.running) {
    const done  = s.processed_groups || 0;
    const total = s.total_groups || '?';
    syncEl.innerHTML = `<span style="color:var(--accent)">🔄 ${done}/${total}</span>`;
    if (grpEl && s.current_group) {
      grpEl.textContent = '· ' + s.current_group.substring(0, 32);
      grpEl.style.display = '';
    } else if (grpEl) {
      grpEl.style.display = 'none';
    }
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = t('sync.scanning'); }
  } else {
    syncEl.textContent = t('sync.live');
    if (grpEl) grpEl.style.display = 'none';
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = t('sync.scan'); }
  }
  updateNextSyncDisplay();
}

function updateNextSyncDisplay() {
  const s = _syncStatusCache;
  const el = document.getElementById('pag-next');
  if (el && s && s.next_sync_at) {
    const diff = Math.max(0, s.next_sync_at - Date.now() / 1000);
    if (diff < 5) {
      el.textContent = '';
    } else {
      const h   = Math.floor(diff / 3600);
      const m   = Math.floor((diff % 3600) / 60);
      const sec = Math.floor(diff % 60);
      if (h > 0)      el.textContent = '· ' + t('sync.next', {dur: `${h}s ${m}dk`});
      else if (m > 0) el.textContent = '· ' + t('sync.next', {dur: `${m}dk ${sec}s`});
      else            el.textContent = '· ' + t('sync.next', {dur: `${sec}s`});
    }
  }
  updateLastSyncDisplay();
}

function _parseLastSync(v) {
  if (v == null) return null;
  if (typeof v === 'number') return v;
  let str = String(v);
  // backend uses datetime.utcnow().isoformat() which has no TZ — treat as UTC
  if (!/[zZ]$|[+-]\d\d:?\d\d$/.test(str)) str += 'Z';
  const ms = Date.parse(str);
  return isNaN(ms) ? null : ms / 1000;
}

function _fmtElapsed(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec}sn`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}dk ${sec % 60}sn`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}s ${m % 60}dk`;
  const d = Math.floor(h / 24);
  return `${d}g ${h % 24}s`;
}

function updateLastSyncDisplay() {
  const s = _syncStatusCache;
  const lastEl = document.getElementById('sb-last-sync');
  const elapsedEl = document.getElementById('sb-elapsed');
  if (!lastEl) return;
  const ts = _parseLastSync(s && s.last_sync_at);
  if (!ts) {
    lastEl.textContent = t('sync.lastSyncEmpty');
    if (elapsedEl) elapsedEl.textContent = '';
    return;
  }
  const date = new Date(ts * 1000);
  lastEl.textContent = t('sync.lastSync', {date: date.toLocaleString(_locale())});
  if (elapsedEl) {
    const elapsed = Date.now() / 1000 - ts;
    elapsedEl.textContent = t('sync.ago', {dur: _fmtElapsed(elapsed)});
  }
}

// ── Groups ────────────────────────────────────────────────────────────────────
let _visibleGroupIds = [];
let _lastSelectedGroupId = null;

function _loadGroupOverrides() {
  try { return JSON.parse(localStorage.getItem('groupOverrides') || '{}'); }
  catch (e) { return {}; }
}
function _saveGroupOverrides(o) {
  try { localStorage.setItem('groupOverrides', JSON.stringify(o)); } catch(e) {}
}
function _setGroupOverride(id, patch) {
  const o = _loadGroupOverrides();
  o[id] = { ...(o[id] || {}), ...patch };
  _saveGroupOverrides(o);
}
function _applyGroupOverrides(groups) {
  const o = _loadGroupOverrides();
  for (const g of groups) {
    const ov = o[g.id];
    if (!ov) continue;
    if (ov.display_name !== undefined && ov.display_name !== null) g.display_name = ov.display_name;
    if (ov.hidden !== undefined) g.hidden = !!ov.hidden;
  }
}

async function loadGroups() {
  _groups = await api('/api/groups');
  _applyGroupOverrides(_groups);
  renderSidebar();
  if (typeof cgfUpdateLabel === 'function') cgfUpdateLabel();
}

function setGroupSort(by) {
  if (S.groupSort === by) {
    S.groupSortDir = S.groupSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    S.groupSort = by;
    S.groupSortDir = by === 'count' ? 'desc' : 'asc';
  }
  renderSidebar();
}

function renderSidebar() {
  const q  = (document.getElementById('group-filter')?.value||'').toLowerCase();
  const el = document.getElementById('group-list');
  if (!el) return;

  let visible = _groups.filter(g => {
    if (S.showHidden) {
      if (!g.hidden) return false;
    } else {
      if (g.hidden) return false;
    }
    if (q && !(g.display_name||g.name).toLowerCase().includes(q)) return false;
    return true;
  });

  visible.sort((a, b) => {
    const v = S.groupSort === 'count'
      ? (a.file_count||0) - (b.file_count||0)
      : (a.display_name||a.name).localeCompare(b.display_name||b.name, 'tr');
    return S.groupSortDir === 'asc' ? v : -v;
  });

  ['name','count'].forEach(k => {
    const btn = document.getElementById('gsort-'+k);
    if (!btn) return;
    const isActive = S.groupSort === k;
    btn.classList.toggle('active', isActive);
    const arrow = isActive ? (S.groupSortDir==='asc' ? ' ↑' : ' ↓') : '';
    btn.textContent = (k==='name' ? t('groups.sortName') : t('groups.sortCount')) + arrow;
  });

  _visibleGroupIds = visible.map(g => g.id);

  el.innerHTML = visible.map(g => {
    const selCls     = S.selectedGroups.has(g.id) ? ' g-selected' : '';
    const hidCls     = g.hidden   ? ' g-hidden'  : '';
    const exclCls    = g.excluded ? ' excluded'  : '';
    const name       = g.display_name || g.name;
    const eyeCls     = g.hidden   ? ' ga-hidden-on'  : '';
    const followCls  = !g.excluded ? ' ga-follow-on' : '';
    const tgHref = tgGroupHref(g);
    // Per-cell actions are limited to genuinely per-group operations (rename
    // and open-in-Telegram). Visibility / follow-state / rescan / leave are
    // delegated to the existing bulk bar so cells stay legible.
    return `<div class="g-item${selCls}${hidCls}${exclCls}" onclick="selectGroup(${g.id},event)">
      <span class="g-name" title="${esc(name)}">${esc(name)}</span>
      <div class="g-acts">
        <button class="ga" onclick="openGroupNameEdit(event,${g.id})" title="${esc(t('groups.editName'))}">✏</button>
        <a class="ga ga-tg" href="${esc(tgHref)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="${esc(t('table.openTg'))}">↗</a>
      </div>
      <div class="g-meta">${(g.file_count||0).toLocaleString()} · ${fmtSize(g.total_size||0)}</div>
    </div>`;
  }).join('');

  const bulk = document.getElementById('sidebar-bulk');
  if (bulk) {
    if (S.selectedGroups.size > 0) {
      bulk.style.display = 'flex';
      document.getElementById('bulk-count').textContent = t('groups.bulkSelected', {n: S.selectedGroups.size});

      // Bulk hide/follow buttons act as toggles — flip the label so the user
      // always knows what the next click will actually do
      const ids = [...S.selectedGroups];
      const sel = ids.map(id => _groups.find(x => x.id === id)).filter(Boolean);
      const allHidden   = sel.length > 0 && sel.every(g => g.hidden);
      const allExcluded = sel.length > 0 && sel.every(g => g.excluded);
      const hideBtn = document.getElementById('bulk-hide-btn');
      const exclBtn = document.getElementById('bulk-excl-btn');
      if (hideBtn) hideBtn.textContent = t(allHidden   ? 'groups.bulkShow'  : 'groups.bulkHide');
      if (exclBtn) exclBtn.textContent = t(allExcluded ? 'groups.bulkTrack' : 'groups.bulkUntrack');
    } else {
      bulk.style.display = 'none';
    }
  }

  const hiddenCount = _groups.filter(g => g.hidden).length;
  const shBtn = document.getElementById('show-hidden-btn');
  if (shBtn) {
    shBtn.textContent = S.showHidden ? t('groups.showAll') : t('groups.showHiddenOnly', {n: hiddenCount});
    shBtn.classList.toggle('active', S.showHidden);
    shBtn.style.display = hiddenCount ? '' : 'none';
  }
}

function toggleShowHidden() { S.showHidden = !S.showHidden; renderSidebar(); }

function selectGroup(id, e) {
  if (e && e.target.closest('.ga')) return;

  // Shift+click: select the visible range between the last clicked cell and this one
  if (e && e.shiftKey && _lastSelectedGroupId != null && _visibleGroupIds.length) {
    // Clear native text selection that shift-click would otherwise leave behind
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.removeAllRanges) sel.removeAllRanges();
    const i = _visibleGroupIds.indexOf(_lastSelectedGroupId);
    const j = _visibleGroupIds.indexOf(id);
    if (i >= 0 && j >= 0) {
      const [a, b] = i <= j ? [i, j] : [j, i];
      for (let k = a; k <= b; k++) S.selectedGroups.add(_visibleGroupIds[k]);
      renderSidebar();
      return;
    }
  }

  if (S.selectedGroups.has(id)) S.selectedGroups.delete(id);
  else S.selectedGroups.add(id);
  _lastSelectedGroupId = id;
  renderSidebar();
}

async function toggleGroupHidden(e, id, currentHidden) {
  e.stopPropagation();
  const next = !currentHidden;
  _setGroupOverride(id, { hidden: next });
  const g = _groups.find(x => x.id === id);
  if (g) g.hidden = next;
  renderSidebar();
  api(`/api/groups/${id}`, {method:'PATCH', json:{hidden: next}}).catch(() => {});
}

async function toggleGroupExcluded(e, id, currentExcluded) {
  e.stopPropagation();
  const next = !currentExcluded;
  const g = _groups.find(x => x.id === id);
  if (g) g.excluded = next;
  renderSidebar();
  await api(`/api/groups/${id}`, {method:'PATCH', json:{excluded: next}});
}

function openGroupNameEdit(e, id) {
  e.stopPropagation();
  const g = _groups.find(x => x.id === id);
  if (!g) return;
  _nameEditGroupId = id;
  document.getElementById('gn-title').textContent = g.display_name || g.name;
  document.getElementById('gn-name').value = (g.display_name && g.display_name !== g.name) ? g.display_name : '';
  document.getElementById('gn-overlay').classList.add('open');
  setTimeout(() => document.getElementById('gn-name').focus(), 50);
}

function closeNameEdit() {
  document.getElementById('gn-overlay').classList.remove('open');
  _nameEditGroupId = null;
}

function gnOverlayClick(e) {
  if (e.target === document.getElementById('gn-overlay')) closeNameEdit();
}

async function saveNameEdit() {
  if (!_nameEditGroupId) return;
  const name = document.getElementById('gn-name').value.trim();
  const id = _nameEditGroupId;
  const g = _groups.find(x => x.id === id);
  if (g) g.display_name = name || g.name;
  _setGroupOverride(id, { display_name: name || null });
  closeNameEdit();
  renderSidebar();
  api(`/api/groups/${id}`, {method:'PATCH', json:{display_name: name || null}}).catch(() => {});
}

async function bulkToggleHide() {
  const ids = [...S.selectedGroups];
  if (!ids.length) return;
  // Flip when all selected are already hidden, otherwise unify to hidden=true
  const allHidden = ids.every(id => {
    const g = _groups.find(x => x.id === id);
    return g && g.hidden;
  });
  const next = !allHidden;
  await Promise.all(ids.map(id =>
    api(`/api/groups/${id}`, {method:'PATCH', json:{hidden: next}})
  ));
  await loadGroups();
}

async function bulkToggleExcluded() {
  const ids = [...S.selectedGroups];
  if (!ids.length) return;
  const allExcluded = ids.every(id => {
    const g = _groups.find(x => x.id === id);
    return g && g.excluded;
  });
  const next = !allExcluded;
  await Promise.all(ids.map(id =>
    api(`/api/groups/${id}`, {method:'PATCH', json:{excluded: next}})
  ));
  await loadGroups();
}

async function leaveGroup(e, id) {
  if (e) e.stopPropagation();
  const g = _groups.find(x => x.id === id);
  const name = g ? (g.display_name || g.name) : `#${id}`;
  if (!confirm(t('groups.leaveConfirm', { name }))) return;
  const purge = confirm(t('groups.purgeConfirm', { count: (g?.file_count||0).toLocaleString() }));
  try {
    await api(`/api/groups/${id}/leave?purge=${purge}`, { method: 'POST' });
    showToast(t('groups.leaveOk', { name: esc(name) }), 3000);
  } catch (err) {
    showToast(t('groups.leaveFail') + ' ' + esc(err.message), 5000);
    return;
  }
  S.selectedGroups.delete(id);
  await loadGroups();
}

async function rescanGroup(e, id) {
  if (e) e.stopPropagation();
  try {
    const r = await api(`/api/groups/${id}/rescan`, { method: 'POST' });
    showToast(r.queued
      ? t('groups.rescanStarted', { name: esc(r.name||id) })
      : t('groups.rescanQueued',  { name: esc(r.name||id) }),
      3000);
  } catch (err) {
    showToast(t('groups.rescanFail') + ' ' + esc(err.message), 4000);
  }
}

async function bulkRescanGroups() {
  const ids = [...S.selectedGroups];
  if (!ids.length) return;
  if (!confirm(t('groups.rescanConfirm', { n: ids.length }))) return;
  let failed = 0;
  for (const id of ids) {
    try { await api(`/api/groups/${id}/rescan`, { method: 'POST' }); }
    catch (err) { failed++; }
  }
  S.selectedGroups.clear();
  renderSidebar();
  if (failed) showToast(t('groups.rescanSome', { ok: ids.length - failed, fail: failed }), 4000);
  else        showToast(t('groups.rescanOk', { n: ids.length }), 3000);
}

async function bulkLeaveGroups() {
  const ids = [...S.selectedGroups];
  if (!ids.length) return;
  if (!confirm(t('groups.bulkLeaveConfirm', { n: ids.length }))) return;
  const purge = confirm(t('groups.bulkLeavePurge'));
  let ok = 0, failed = 0;
  for (const id of ids) {
    try {
      await api(`/api/groups/${id}/leave?purge=${purge}`, { method: 'POST' });
      ok++;
    } catch (e) {
      failed++;
    }
  }
  S.selectedGroups.clear();
  await loadGroups();
  if (failed) showToast(t('groups.bulkLeaveSome', { ok, fail: failed }), 4500);
  else showToast(t('groups.bulkLeaveOk', { ok }), 3000);
}

function bulkClearSelection() { S.selectedGroups.clear(); renderSidebar(); }

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchSettingsTab(name) {
  document.querySelectorAll('.sub-tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.stab === name)
  );
  document.querySelectorAll('.settings-tab-content').forEach(c =>
    c.classList.toggle('active', c.id === 'settings-tab-' + name)
  );
  if (name === 'watches') {
    loadWatches();
    loadAllNotifications();
  } else if (name === 'account') {
    loadAccountsList();
    _refreshUiPwState();
    loadTelemetrySettings();
  } else if (name === 'transfer') {
    loadTransferDestinations();
  }
}

function switchTab(tab) {
  S.activeTab = tab;
  ['files','links','settings','downloads','status','hunter'].forEach(t =>
    document.getElementById('tab-'+t)?.classList.toggle('active', t===tab));

  const isFiles     = tab === 'files';
  const isLinks     = tab === 'links';
  const isSettings  = tab === 'settings';
  const isDownloads = tab === 'downloads';
  const isStatus    = tab === 'status';
  const isHunter    = tab === 'hunter';
  const showTable   = isFiles || isLinks;

  document.getElementById('filter-bar').className      = isFiles ? 'filter-bar' : 'filter-bar hidden-bar';
  document.getElementById('link-filter-bar').className = isLinks ? 'link-filter-bar' : 'link-filter-bar hidden-bar';
  document.getElementById('table-wrap').style.display      = showTable   ? '' : 'none';
  document.getElementById('pagination').style.display      = showTable   ? '' : 'none';
  document.getElementById('status-panel').style.display    = isStatus    ? 'flex' : 'none';
  document.getElementById('settings-panel').style.display  = isSettings  ? 'flex' : 'none';
  document.getElementById('downloads-panel').style.display = isDownloads ? 'flex' : 'none';
  const hp = document.getElementById('hunter-panel');
  if (hp) hp.style.display = isHunter ? 'flex' : 'none';
  document.getElementById('files-table').style.display     = isFiles ? '' : 'none';
  document.getElementById('links-table').style.display     = isLinks ? '' : 'none';

  if (isFiles)         { stopStatusPoll(); stopHunterPoll(); loadFiles(); }
  else if (isLinks)    { stopStatusPoll(); stopHunterPoll(); loadLinks(); }
  else if (isSettings) { stopStatusPoll(); stopHunterPoll(); loadCredentials(); loadSyncInterval(); _refreshUiPwState(); }
  else if (isDownloads){ stopStatusPoll(); stopHunterPoll(); loadDownloadsList(); }
  else if (isStatus)   { stopHunterPoll(); startStatusPoll(); }
  else if (isHunter)   { stopStatusPoll(); startHunterPoll(); }

  updateBulkFileBtn();
  updateBulkLinkBtn();
  renderWatchBanner();
}

// ── Status tab ────────────────────────────────────────────────────────────────
function startStatusPoll() {
  loadStatus();
  _statusInterval = setInterval(loadStatus, 500);
}

function stopStatusPoll() {
  if (_statusInterval) { clearInterval(_statusInterval); _statusInterval = null; }
}

async function loadStatus() {
  try {
    const d = await api('/api/status');
    const el = document.getElementById('status-panel');
    if (!el || el.style.display === 'none') return;
    const scroll = el.scrollTop;
    renderStatus(d);
    el.scrollTop = scroll;
  } catch(e) { /* ignore while tab is switching */ }
}

const _TYPE_ICON  = {audio:'🎵',video:'🎬',image:'🖼',archive:'🗜',document:'📄',software:'💾',other:'📦'};
const _TYPE_COLOR = {audio:'#7c3aed',video:'#ef4444',image:'#059669',archive:'#f59e0b',document:'#2563eb',software:'#374151',other:'#9ca3af'};
const _TYPE_NAME  = {audio:'type.audio',video:'type.video',image:'type.image',archive:'type.archive',document:'type.document',software:'type.software',other:'type.other'};

function renderStatus(d) {
  const el = document.getElementById('status-panel');
  el.innerHTML =
    stCards(d) +
    stFileTypes(d) +
    `<div class="st-2col">${stGroups(d)}${stPlatforms(d)}</div>` +
    stPgTables(d) +
    `<div class="st-2col">${stSystem(d)}${stSync(d)}</div>` +
    stLogs(d);
}

function stCards(d) {
  const f = d.files || {};
  const dlPct  = f.total ? Math.round((f.downloaded||0)/f.total*100) : 0;
  const avgSize = f.total ? Math.round((f.total_size||0)/f.total) : 0;
  return `<div class="st-cards">
    <div class="st-card">
      <div class="st-lbl">${esc(t("status.totalFiles"))}</div>
      <div class="st-val">${(f.total||0).toLocaleString()}</div>
      <div class="st-sub">${esc(t("misc.todayCount",{d:f.recent_24h||0,w:f.recent_7d||0,m:f.recent_30d||0}))}</div>
    </div>
    <div class="st-card">
      <div class="st-lbl">${esc(t("status.totalSize"))}</div>
      <div class="st-val">${fmtSize(f.total_size||0)}</div>
      <div class="st-sub">${esc(t("misc.avgPerFile",{size:fmtSize(avgSize)}))}</div>
    </div>
    <div class="st-card">
      <div class="st-lbl">${esc(t("status.downloaded"))}</div>
      <div class="st-val">${(f.downloaded||0).toLocaleString()}</div>
      <div class="st-sub">${dlPct}% · ${fmtSize(f.downloaded_size||0)}</div>
    </div>
    <div class="st-card">
      <div class="st-lbl">${esc(t("status.links"))}</div>
      <div class="st-val">${((d.links||{}).total||0).toLocaleString()}</div>
      <div class="st-sub">${esc(t("misc.platforms",{n:((d.links||{}).by_platform||[]).length}))}</div>
    </div>
    <div class="st-card">
      <div class="st-lbl">${esc(t("status.database"))}</div>
      <div class="st-val">${(d.db||{}).size_pretty||'—'}</div>
      <div class="st-sub">${esc(t("misc.tables",{n:((d.db||{}).tables||[]).length}))}</div>
    </div>
  </div>`;
}

function stFileTypes(d) {
  const rows = ((d.files||{}).by_type||[]).map(tr => {
    const dlPct = tr.cnt ? Math.round(tr.dl_cnt/tr.cnt*100) : 0;
    const color = _TYPE_COLOR[tr.grp] || '#9ca3af';
    return `<tr>
      <td>${_TYPE_ICON[tr.grp]||'📦'} ${esc(t(_TYPE_NAME[tr.grp]||tr.grp))}</td>
      <td class="r">${tr.cnt.toLocaleString()}</td>
      <td class="r">${fmtSize(tr.total_sz)}</td>
      <td><div style="display:flex;align-items:center;gap:6px">
        <span style="min-width:44px;text-align:right">${tr.dl_cnt.toLocaleString()}</span>
        <div class="mini-bar" style="flex:1"><div class="mini-bar-fill" style="width:${dlPct}%;background:${color}"></div></div>
        <span style="font-size:.68rem;color:#9ca3af;width:30px">${dlPct}%</span>
      </div></td>
      <td class="r">${fmtSize(tr.dl_sz)}</td>
    </tr>`;
  }).join('');
  return `<div class="st-section">
    <h4><span class="live-dot"></span>${esc(t("status.fileTypes"))}</h4>
    <table class="st-tbl">
      <tr><th>${esc(t("status.colType"))}</th><th class="r">${esc(t("status.colCount"))}</th><th class="r">${esc(t("status.colSizeCol"))}</th><th>${esc(t("status.colDlRatio"))}</th><th class="r">${esc(t("status.colDlSizeCol"))}</th></tr>
      ${rows||'<tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:12px">${esc(t("status.noData"))}</td></tr>'}
    </table>
  </div>`;
}

function stGroups(d) {
  const g = d.groups || {};
  const total   = g.total  || 0;
  const synced  = g.synced || 0;
  const syncPct = total ? Math.round(synced/total*100) : 0;
  return `<div class="st-section">
    <h4>${esc(t("status.groups"))}</h4>
    <div class="bar-row">
      <span class="bar-lbl">${esc(t("status.synchronized"))}</span>
      <div class="st-bar"><div class="st-bar-fill" style="width:${syncPct}%"></div></div>
      <span class="bar-val">${synced} / ${total}</span>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px">
      ${[[t('status.allTotal'),total],[t('status.allSync'),synced],[t('status.allExcl'),g.excluded||0],[t('status.allHidden'),g.hidden||0]].map(([k,v])=>
        `<div class="kv-row" style="flex-direction:column;gap:0;border:none;padding:0">
          <span class="kv-key" style="font-size:.67rem">${k}</span>
          <span class="kv-val" style="font-size:1.1rem">${v}</span>
        </div>`).join('')}
    </div>
  </div>`;
}

function stPlatforms(d) {
  const plats = (d.links||{}).by_platform || [];
  const max = plats[0]?.cnt || 1;
  const rows = plats.slice(0,10).map(p =>
    `<div class="bar-row">
      <span class="bar-lbl">${esc(p.platform)}</span>
      <div class="st-bar"><div class="st-bar-fill" style="width:${Math.round(p.cnt/max*100)}%"></div></div>
      <span class="bar-val">${p.cnt.toLocaleString()}</span>
    </div>`).join('');
  return `<div class="st-section">
    <h4>${esc(t("status.linksByPlatform"))}</h4>
    ${rows||`<div style="color:#9ca3af;font-size:.78rem">${esc(t("status.notIndexed"))}</div>`}
  </div>`;
}

function stPgTables(d) {
  const tables = (d.db||{}).tables || [];
  const rows = tables.map(tr =>
    `<tr>
      <td><b>${tr.tablename}</b></td>
      <td class="r">${(tr.row_count||0).toLocaleString()}</td>
      <td class="r">${tr.size_pretty}</td>
      <td class="r">${tr.table_size_pretty}</td>
      <td class="r">${tr.index_size_pretty}</td>
    </tr>`).join('');
  return `<div class="st-section">
    <h4>PostgreSQL — DB: ${(d.db||{}).size_pretty||'—'}</h4>
    <table class="st-tbl">
      <tr><th>${esc(t("status.colTable"))}</th><th class="r">${esc(t("status.colRows"))}</th><th class="r">${esc(t("status.colTotal"))}</th><th class="r">${esc(t("status.colData"))}</th><th class="r">${esc(t("status.colIndex"))}</th></tr>
      ${rows||'<tr><td colspan="5" style="text-align:center;color:#9ca3af">—</td></tr>'}
    </table>
  </div>`;
}

function stSystem(d) {
  const sys = d.system || {};
  let bars = '';
  if (sys.cgroup_mem_used !== undefined) {
    const used  = sys.cgroup_mem_used;
    const limit = sys.cgroup_mem_limit;
    const pct   = limit ? Math.round(used/limit*100) : 0;
    const color = pct > 85 ? '#ef4444' : pct > 70 ? '#f59e0b' : '#2563eb';
    bars += `<div class="bar-row">
      <span class="bar-lbl">${esc(t("status.containerRam"))}</span>
      <div class="st-bar"><div class="st-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="bar-val">${fmtSize(used)}${limit?' / '+fmtSize(limit):''}</span>
    </div>`;
  } else if (sys.proc_rss_bytes) {
    bars += `<div class="bar-row">
      <span class="bar-lbl">${esc(t("status.processRss"))}</span>
      <div class="st-bar"><div class="st-bar-fill" style="width:40%"></div></div>
      <span class="bar-val">${fmtSize(sys.proc_rss_bytes)}</span>
    </div>`;
  }
  if (sys.disk) {
    const dk  = sys.disk;
    const pct = Math.round(dk.used/dk.total*100);
    const color = pct > 90 ? '#ef4444' : pct > 75 ? '#f59e0b' : '#2563eb';
    bars += `<div class="bar-row">
      <span class="bar-lbl">${esc(t("status.disk"))}</span>
      <div class="st-bar"><div class="st-bar-fill" style="width:${pct}%;background:${color}"></div></div>
      <span class="bar-val">${fmtSize(dk.used)} / ${fmtSize(dk.total)}</span>
    </div>`;
  }
  const load = sys.load ? sys.load.map(x=>x.toFixed(2)).join('  ') : null;
  return `<div class="st-section">
    <h4>${esc(t("status.system"))}</h4>
    ${bars||`<div style="color:#9ca3af;font-size:.78rem">${esc(t("status.noInfo"))}</div>`}
    <div style="margin-top:10px;display:flex;gap:16px;flex-wrap:wrap">
      <div class="kv-row" style="border:none;padding:0"><span class="kv-key">${esc(t("status.uptime"))}</span><span class="kv-val" style="margin-left:8px">${fmtUptime(sys.uptime||0)}</span></div>
      ${load?`<div class="kv-row" style="border:none;padding:0"><span class="kv-key">${esc(t("status.load"))}</span><span class="kv-val" style="margin-left:8px;font-family:monospace">${load}</span></div>`:''}
    </div>
  </div>`;
}

function stSync(d) {
  const s = d.sync || {};
  const lastAt   = s.last_sync_at ? new Date(s.last_sync_at).toLocaleString(_locale()) : '—';
  const nextAt   = s.next_sync_at && s.next_sync_at > 0 ? new Date(s.next_sync_at*1000).toLocaleString(_locale()) : '—';
  const statusTxt = s.running
    ? `🔄 ${s.processed_groups||0} / ${s.total_groups||0}`
    : t('status.ready');
  return `<div class="st-section">
    <h4>${esc(t("status.sync"))}</h4>
    <div style="font-size:.82rem;font-weight:600;color:${s.running?'#2563eb':'#16a34a'};margin-bottom:8px">${statusTxt}</div>
    ${s.running&&s.current_group?`<div style="font-size:.73rem;color:#6b7280;margin-bottom:8px">${esc(t("status.currentGroup"))}: <b style="color:#374151">${esc(s.current_group)}</b></div>`:''}
    <div class="kv-row"><span class="kv-key">${esc(t("status.lastSync"))}</span><span class="kv-val">${lastAt}</span></div>
    <div class="kv-row"><span class="kv-key">${esc(t("status.nextSync"))}</span><span class="kv-val">${nextAt}</span></div>
    <div class="kv-row"><span class="kv-key">${esc(t("status.newFilesSession"))}</span><span class="kv-val">${s.new_files||0}</span></div>
    <div class="kv-row"><span class="kv-key">${esc(t("status.newLinksSession"))}</span><span class="kv-val">${s.new_links||0}</span></div>
    ${s.error?`<div style="font-size:.73rem;color:#dc2626;margin-top:8px;word-break:break-word">⚠ ${esc(s.error)}</div>`:''}
  </div>`;
}

function stLogs(d) {
  const logs = [...(d.logs||[])].reverse();
  const rows = logs.map(l => {
    const ts = new Date(l.ts*1000).toLocaleTimeString();
    return `<div class="log-entry log-${l.level}">[${ts}] ${esc(l.msg)}</div>`;
  }).join('');
  return `<div class="st-section">
    <h4><span class="live-dot"></span>${esc(t('status.recentLogs'))}</h4>
    <div class="log-wrap">${rows||`<span style="color:#9ca3af">${esc(t('status.noLogs'))}</span>`}</div>
  </div>`;
}

function fmtUptime(s) {
  const d=Math.floor(s/86400), h=Math.floor((s%86400)/3600), m=Math.floor((s%3600)/60);
  if(d>0) return `${d}g ${h}s ${m}d`;
  if(h>0) return `${h}s ${m}d`;
  return `${Math.floor(s%60)?m+'d '+Math.floor(s%60)+'s':m+'d'}`;
}

// ── Type filter ───────────────────────────────────────────────────────────────
function setTypeFilter(group) {
  S.typeGroup = group;
  S.extChip = '';
  document.getElementById('ext-input').value = '';
  document.getElementById('col-ext').value = '';
  renderChips();
  S.offset = 0;
  document.querySelectorAll('.type-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.group===group));
  loadFiles();
}

// ── Size slider ───────────────────────────────────────────────────────────────
function initSizeSlider(maxBytes) {
  const maxMB = Math.ceil(maxBytes/1048576)||1;
  S.sliderMax = maxMB;
  S.sizeMinMB = null; S.sizeMaxMB = null;
  document.getElementById('sl-min').value = 0;
  document.getElementById('sl-max').value = 1000;
  document.getElementById('sl-min-lbl').textContent = '0 MB';
  document.getElementById('sl-max-lbl').textContent = fmtMB(maxMB);
  updateSliderFill();
}
function sliderToMB(v) { return Math.round((v/1000)**2*S.sliderMax); }
function onSizeSlider() {
  let lo=+document.getElementById('sl-min').value, hi=+document.getElementById('sl-max').value;
  if (lo>hi){const t=lo;lo=hi;hi=t;document.getElementById('sl-min').value=lo;document.getElementById('sl-max').value=hi;}
  S.sizeMinMB = lo===0?null:sliderToMB(lo);
  S.sizeMaxMB = hi===1000?null:sliderToMB(hi);
  document.getElementById('sl-min-lbl').textContent = fmtMB(sliderToMB(lo));
  document.getElementById('sl-max-lbl').textContent = hi===1000?fmtMB(S.sliderMax):fmtMB(sliderToMB(hi));
  updateSliderFill(); debouncedLoad();
}
function updateSliderFill() {
  const lo=+document.getElementById('sl-min').value, hi=+document.getElementById('sl-max').value;
  const f=document.getElementById('size-range-fill');
  f.style.left=(lo/10)+'%'; f.style.width=((hi-lo)/10)+'%';
}
function fmtMB(mb) { return mb>=1024?(mb/1024).toFixed(1)+' GB':mb+' MB'; }

// ── Context tooltip ───────────────────────────────────────────────────────────
function onCtxMove(e) {
  const td = e.target.closest('td[data-ctx]');
  const tip = document.getElementById('ctx-tip');
  if (!td||!td.dataset.ctx) { tip.style.display='none'; return; }
  tip.textContent = td.dataset.ctx;
  tip.style.display = 'block';
  const x=e.clientX+14, y=e.clientY+14;
  const bx=tip.offsetWidth, by=tip.offsetHeight;
  tip.style.left=(x+bx>window.innerWidth?x-bx-20:x)+'px';
  tip.style.top=(y+by>window.innerHeight?y-by-20:y)+'px';
}

// ── Files ─────────────────────────────────────────────────────────────────────
let _debounceTimer;
function debouncedLoad() {
  clearTimeout(_debounceTimer);
  _debounceTimer = setTimeout(() => {
    const extVal = document.getElementById('ext-input').value.trim() ||
                   document.getElementById('col-ext').value.trim();
    if (extVal && S.typeGroup) {
      S.typeGroup = '';
      document.querySelectorAll('.type-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.group===''));
    }
    S.offset = 0; loadFiles();
  }, 280);
}

async function loadFiles(silent = false) {
  const params = new URLSearchParams({
    q:         document.getElementById('col-name').value.trim(),
    ext:       S.extChip || document.getElementById('ext-input').value.trim() ||
               document.getElementById('col-ext').value.trim(),
    ext_group: S.typeGroup,
    sort_by:   S.sortBy, sort_dir: S.sortDir,
    limit:     S.limit,  offset:   S.offset,
  });
  if (S.activeGroupId!=null) params.set('group_id', S.activeGroupId);

  // Multi-select group filter (from #cgf-list checkboxes)
  if (S.colGroupIds && S.colGroupIds.size > 0) {
    params.set('group_ids', [...S.colGroupIds].join(','));
  }

  // Notification-driven file_ids restriction (set by showNotifMatches)
  if (S.fileIdsFilter && S.fileIdsFilter.size > 0) {
    params.set('file_ids', [...S.fileIdsFilter].join(','));
  }

  // Column-level size filter overrides the top filter bar
  const colSizeMin = document.getElementById('col-size-min').value.trim();
  const colSizeMax = document.getElementById('col-size-max').value.trim();

  const df = document.getElementById('date-from')?.value;
  const dt = document.getElementById('date-to')?.value;
  if (df) params.set('date_from', df);
  if (dt) params.set('date_to', dt);

  let smin = colSizeMin ? parseFloat(colSizeMin) : null;
  let smax = colSizeMax ? parseFloat(colSizeMax) : null;
  if (smin == null && S.sizeMinMB != null) smin = S.sizeMinMB;
  if (smax == null && S.sizeMaxMB != null) smax = S.sizeMaxMB;
  if (smin != null && !isNaN(smin)) params.set('size_min', Math.round(smin * 1048576));
  if (smax != null && !isNaN(smax)) params.set('size_max', Math.round(smax * 1048576));

  if (!silent) _paintGridLoading('files-body', 9);
  const data = await api('/api/files?'+params);

  if (S.sliderMax===0) {
    const stats = await api('/api/stats');
    initSizeSlider(stats.max_file_size||0);
  }

  renderFiles(data.files, '');
  renderPagination(data.total, S.limit, S.offset);
  const fc = document.getElementById('flt-count');
  if (fc) fc.textContent = t("filter.fileCount", {n: (data.total || 0).toLocaleString()});
}

function renderFiles(files, gFilter) {
  const tbody = document.getElementById('files-body');
  if (gFilter) files = files.filter(f=>(f.group_name||'').toLowerCase().includes(gFilter));
  _currentFiles = files;
  if (!files.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="no-data">${esc(t("table.noFiles"))}</td></tr>`;
    return;
  }
  tbody.innerHTML = files.map((f,i) => {
    const rowNum  = S.offset+i+1;
    const checked = S.selectedFiles.has(f.id) ? ' checked' : '';
    const ext     = (f.file_ext||'').toUpperCase();
    const color   = extColor(f.file_ext||'');
    const badge   = ext ? `<span class="ext-badge" style="${color}" onclick="filterByExt('${esc(f.file_ext)}')">${esc(ext)}</span>` : '—';
    // Strip emojis / formatting glue from anything that came out of a
    // Telegram message (channel display name, file name, message body)
    // so cells render as plain text regardless of how the source was decorated.
    const gName   = cleanText(f.group_name || '');
    const fName   = cleanText(f.file_name || '') || '—';
    const ctxRaw  = cleanText(f.context || '');
    const gLink   = `<span class="g-link" onclick="filterByGroup(${f.group_id})">${esc(gName)}</span>`;
    const tg      = tgLink(f);
    const selRow  = f.id === _selectedFileId ? ' class="row-selected"' : '';
    // Same file (name + size) re-posted across multiple messages collapses
    // into one row; surface the underlying count.
    const dupBadge = (f.appearances && f.appearances > 1)
      ? `<span class="link-dup-badge" title="${esc(t('table.appearances', { n: f.appearances }))}">×${f.appearances}</span>`
      : '';
    return `<tr${selRow} onclick="selectFileRow(event,${f.id})">
      <td class="chk-cell"><input type="checkbox" class="row-chk" data-fid="${f.id}"${checked}></td>
      <td class="num-cell">${rowNum}</td>
      <td>${badge}</td>
      <td title="${esc(fName)}"><div class="fname-cell"><span class="fname-trunc">${esc(fName)}</span>${tg}${dupBadge}</div></td>
      <td class="ctx-cell"${ctxRaw ? ` data-ctx="${esc(ctxRaw)}"` : ''} title="${esc(ctxRaw)}">${ctxRaw ? esc(ctxRaw.substring(0,50)) : '—'}</td>
      <td>${fmtSize(f.file_size)}</td>
      <td>${gLink}</td>
      <td>${fmtDate(f.date)}</td>
      <td>${dlState(f)}</td>
    </tr>`;
  }).join('');
  // Direct per-checkbox listeners — gives us a real DOM event with shiftKey.
  tbody.querySelectorAll('.row-chk').forEach(cb => {
    cb.addEventListener('click', _fileCbClick);
  });
  updateBulkFileBtn();
}

function selectFileRow(e, id) {
  if (e.target.tagName === 'INPUT' || e.target.closest('a, .dl-link, .dl-done, .dl-prog, .ext-badge, .g-link')) return;
  const was = _selectedFileId === id;
  _selectedFileId = was ? null : id;
  document.querySelectorAll('#files-body tr').forEach(r => r.classList.remove('row-selected'));
  if (!was) e.currentTarget.classList.add('row-selected');
}

// ── File selection & bulk download ───────────────────────────────────────────
let _lastFileToggleId = null;

function _filesVisibleRowIds() {
  return [...document.querySelectorAll('#files-body .row-chk')]
    .map(cb => parseInt(cb.getAttribute('data-fid'), 10))
    .filter(x => Number.isFinite(x));
}

function _fileCbClick(ev) {
  const cb = ev.currentTarget;
  ev.stopPropagation();   // don't toggle the row's "selected" highlight
  const id = parseInt(cb.getAttribute('data-fid'), 10);
  if (!Number.isFinite(id)) return;
  toggleFileSelect(id, cb.checked, ev);
}

function toggleFileSelect(id, checked, e) {
  // Shift+click extends across the visible range.
  if (e && e.shiftKey && _lastFileToggleId != null && _lastFileToggleId !== id) {
    const ids = _filesVisibleRowIds();
    const a = ids.indexOf(_lastFileToggleId);
    const b = ids.indexOf(id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a <= b ? [a, b] : [b, a];
      const sel = window.getSelection && window.getSelection();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
      const boxes = [...document.querySelectorAll('#files-body .row-chk')];
      for (let i = lo; i <= hi; i++) {
        const rid = ids[i];
        if (checked) S.selectedFiles.add(rid);
        else        S.selectedFiles.delete(rid);
        const box = boxes[i];
        if (box) box.checked = checked;
      }
      updateBulkFileBtn();
      _lastFileToggleId = id;
      return;
    }
  }
  if (checked) S.selectedFiles.add(id); else S.selectedFiles.delete(id);
  _lastFileToggleId = id;
  updateBulkFileBtn();
}

function toggleAllFiles(checked) {
  S.selectedFiles.clear();
  document.querySelectorAll('#files-body .row-chk').forEach(chk => {
    chk.checked = checked;
    if (checked) {
      const id = parseInt(chk.getAttribute('data-fid'), 10);
      if (Number.isFinite(id)) S.selectedFiles.add(id);
    }
  });
  updateBulkFileBtn();
}

function updateBulkFileBtn() {
  const btn = document.getElementById('bulk-dl-btn');
  if (!btn) return;
  if (S.activeTab !== 'files') { btn.style.display = 'none'; return; }
  const n = S.selectedFiles.size;
  btn.style.display = n>0 ? 'inline-flex' : 'none';
  let totalSize = 0;
  for (const f of _currentFiles) {
    if (S.selectedFiles.has(f.id)) totalSize += f.file_size || 0;
  }
  btn.textContent = totalSize > 0 ? t("bulk.dlSelectedSize",{n,size:fmtSize(totalSize)}) : t("bulk.dlSelected",{n});
}

async function bulkDownloadSelected() {
  const fileIds = [...S.selectedFiles];
  const dests = await _getEnabledDests();
  let destIds = [];
  if (dests.length > 0) {
    const result = await _showDlDestModal(dests, fileIds);
    if (result === null) return; // cancelled
    destIds = result;
  }
  for (const fileId of fileIds) {
    await _doDownload(fileId, destIds);
  }
  S.selectedFiles.clear();
  updateBulkFileBtn();
  document.querySelectorAll('.row-chk').forEach(c => c.checked=false);
}

// ── Download state ────────────────────────────────────────────────────────────
function dlState(f) {
  if (f.local_path)  return `<span class="dl-done">${esc(t("table.dlDone"))}</span>`;
  if (f.downloading) return `<span class="dl-prog">${Math.round(f.download_progress*100)}%</span>`;
  if (S.dlQueue[f.id]!==undefined) return `<span class="dl-prog">${S.dlQueue[f.id]}%</span>`;
  return `<span class="dl-link" onclick="triggerDownload(${f.id})">${esc(t("table.dlLink"))}</span>`;
}

async function _doDownload(fileId, destinationIds) {
  const r = await api(`/api/files/${fileId}/download`, {
    method: 'POST',
    json: { destination_ids: destinationIds || [] },
  });
  if (r.status === 'already_downloaded') { loadFiles(); return; }
  if (r.status === 'transfer_started') {
    showToast(t('dl.transferStarted'));
    loadFiles();
    return;
  }

  if (!_dlHistory.find(e => e.id === fileId)) {
    _dlHistory.push({ id: fileId, name: '', size: 0, group: '', pct: 0, status: 'queued', startedAt: Date.now() });
    if (S.activeTab !== 'downloads') {
      showToast(t('dl.downloadStarted', { link: `<a onclick="switchTab('downloads')" style="cursor:pointer;text-decoration:underline">${t('dl.goToDownloads')}</a>` }));
    }
  }

  S.dlQueue[fileId] = 0;
  if (S.activeTab === 'downloads') renderDownloadsTab();
  pollDownload(fileId);
}

// _dlDestPending: { fileIds: [], resolve: fn }
let _dlDestPending = null;

async function triggerDownload(fileId) {
  const dests = await _getEnabledDests();
  if (dests.length > 0) {
    const destIds = await _showDlDestModal(dests, [fileId]);
    if (destIds === null) return; // cancelled
    await _doDownload(fileId, destIds);
  } else {
    await _doDownload(fileId, []);
  }
}

async function _getEnabledDests() {
  try {
    const dests = await api('/api/transfer-destinations');
    return (dests || []).filter(d => d.enabled);
  } catch { return []; }
}

function _showDlDestModal(dests, fileIds) {
  return new Promise(resolve => {
    _dlDestPending = { fileIds, resolve };
    const wrap = document.getElementById('ddm-options');
    wrap.innerHTML = '';
    dests.forEach(d => {
      const desc = _destPathLabel(d);
      const row = document.createElement('div');
      row.className = 'ddm-option selected';
      row.dataset.id = d.id;
      row.innerHTML = `
        <input type="checkbox" checked onchange="this.closest('.ddm-option').classList.toggle('selected',this.checked)">
        <span class="td-badge ${d.type}">${_typeLabelShort(d.type)}</span>
        <span class="ddm-opt-name">${esc(d.name)}</span>
        <span class="ddm-opt-path">${esc(desc)}</span>`;
      row.querySelector('input').addEventListener('change', () => {});
      wrap.appendChild(row);
    });
    document.getElementById('dl-dest-overlay').classList.add('open');
  });
}

function closeDlDestModal() {
  document.getElementById('dl-dest-overlay').classList.remove('open');
  if (_dlDestPending) { _dlDestPending.resolve(null); _dlDestPending = null; }
}

function confirmDlDestModal(withTransfer) {
  document.getElementById('dl-dest-overlay').classList.remove('open');
  if (!_dlDestPending) return;
  let destIds = [];
  if (withTransfer) {
    document.querySelectorAll('#ddm-options .ddm-option').forEach(row => {
      if (row.querySelector('input').checked) destIds.push(parseInt(row.dataset.id));
    });
  }
  const pending = _dlDestPending;
  _dlDestPending = null;
  pending.resolve(destIds);
}

function showToast(html, ms = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = html;
  container.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.add('fade');
    setTimeout(() => el.remove(), 300);
  }, ms);
}

function _updateFileDlCell(fileId, fakef) {
  const cb = document.querySelector(`#files-body .row-chk[data-fid="${fileId}"]`);
  if (!cb) return;
  const dlTd = cb.closest('tr')?.children[8];
  if (dlTd) dlTd.innerHTML = dlState(fakef);
}

function pollDownload(fileId) {
  if (S.polls[fileId]) return;
  S.polls[fileId] = setInterval(async () => {
    const f = await api(`/api/files/${fileId}`);
    const entry = _dlHistory.find(e => e.id === fileId);
    if (entry && !entry.name) {
      entry.name  = f.file_name  || '';
      entry.size  = f.file_size  || 0;
      entry.group = f.group_name || '';
    }
    if (f.local_path) {
      delete S.dlQueue[fileId];
      clearInterval(S.polls[fileId]); delete S.polls[fileId];
      if (entry) { entry.pct = 100; entry.status = 'done'; }
      if (S.activeTab === 'downloads') loadDownloadsList();
      _updateFileDlCell(fileId, { local_path: f.local_path });
    } else if (f.downloading) {
      const pct = Math.round(f.download_progress*100);
      S.dlQueue[fileId] = pct;
      if (entry) { entry.pct = pct; entry.status = 'downloading'; }
      if (S.activeTab === 'downloads') renderDownloadsTab();
      _updateFileDlCell(fileId, { downloading: true, download_progress: f.download_progress });
    }
  }, 2000);
}

async function loadDownloadsList() {
  _paintGridLoading('dl-hist-tbody', 8);
  try {
    [_serverDownloads] = await Promise.all([
      api('/api/downloads'),
      api('/api/downloads/active').then(active => {
        for (const f of active) {
          if (!_dlHistory.find(e => e.id === f.id)) {
            _dlHistory.push({
              id: f.id,
              name: f.file_name || '',
              size: f.file_size || 0,
              group: f.group_name || '',
              pct: Math.round((f.download_progress || 0) * 100),
              status: 'downloading',
              startedAt: Date.now(),
            });
            S.dlQueue[f.id] = Math.round((f.download_progress || 0) * 100);
            pollDownload(f.id);
          }
        }
      }).catch(() => {}),
    ]);
  } catch (e) {
    _serverDownloads = [];
  }
  renderDownloadsTab();
}

function sortDownloads(col) {
  if (S.dlSortBy === col) {
    S.dlSortDir = S.dlSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    S.dlSortBy = col;
    S.dlSortDir = (col === 'name' || col === 'group' || col === 'status') ? 'asc' : 'desc';
  }
  renderDownloadsTab();
}

function _updateDlSortArrows() {
  ['name','size','group','downloaded_at','progress','status'].forEach(c => {
    const el = document.getElementById('dl-arr-' + c);
    if (!el) return;
    el.textContent = S.dlSortBy === c ? (S.dlSortDir === 'asc' ? '▲' : '▼') : '▲▼';
    el.classList.toggle('active', S.dlSortBy === c);
  });
}

function renderDownloadsTab() {
  const tbody = document.getElementById('dl-hist-tbody');
  const empty = document.getElementById('dl-hist-empty');
  const count = document.getElementById('dl-hist-count');
  if (!tbody) return;

  // In-flight items live in _dlHistory; completed items are loaded from /api/downloads
  const inFlight = _dlHistory.filter(e => e.status === 'queued' || e.status === 'downloading')
    .map(e => ({...e, downloaded_at: null}));

  const completedRows = _serverDownloads.map(d => ({
    id: d.id,
    name: d.file_name || t('common.fileId', { n: d.id }),
    size: d.file_size || 0,
    group: d.group_name || '',
    pct: 100,
    status: 'done',
    downloaded_at: d.downloaded_at || null,
  }));

  let all = [...inFlight, ...completedRows];

  // Apply name/group filter from filter row
  const fName = (document.getElementById('dl-col-name')?.value || '').trim().toLowerCase();
  const fGroup = (document.getElementById('dl-col-group')?.value || '').trim().toLowerCase();
  if (fName)  all = all.filter(e => (e.name||'').toLowerCase().includes(fName));
  if (fGroup) all = all.filter(e => (e.group||'').toLowerCase().includes(fGroup));

  // Apply sort
  const dir = S.dlSortDir === 'asc' ? 1 : -1;
  const cmp = (a, b) => {
    let va, vb;
    switch (S.dlSortBy) {
      case 'name':  va=(a.name||'').toLowerCase(); vb=(b.name||'').toLowerCase(); break;
      case 'size':  va=a.size||0; vb=b.size||0; break;
      case 'group': va=(a.group||'').toLowerCase(); vb=(b.group||'').toLowerCase(); break;
      case 'progress': va=a.pct||0; vb=b.pct||0; break;
      case 'status':va=a.status||''; vb=b.status||''; break;
      case 'downloaded_at':
      default: va=a.downloaded_at?Date.parse(a.downloaded_at):0;
               vb=b.downloaded_at?Date.parse(b.downloaded_at):0; break;
    }
    if (va < vb) return -1*dir;
    if (va > vb) return  1*dir;
    return 0;
  };
  all.sort(cmp);
  // Always promote in-flight downloads (queued + downloading) to the top,
  // regardless of the user's current sort column.
  const _active = all.filter(e => e.status === 'queued' || e.status === 'downloading');
  const _done   = all.filter(e => e.status === 'done');
  all = _active.concat(_done);
  _updateDlSortArrows();

  const notice = document.getElementById('dl-space-notice');
  const completedSize = _serverDownloads.reduce((s, d) => s + (d.file_size || 0), 0);
  if (notice) notice.style.display = completedSize > 0 ? '' : 'none';

  if (!all.length) {
    tbody.innerHTML = '';
    empty.style.display = '';
    if (count) count.textContent = '';
    return;
  }
  empty.style.display = 'none';
  if (count) {
    count.textContent = completedSize > 0
      ? `(${all.length}) · ${fmtSize(completedSize)}`
      : `(${all.length})`;
  }

  // Drop selections for ids that are no longer present at all
  const knownIds = new Set(all.map(r => r.id));
  for (const id of [...S.selectedDownloads]) {
    if (!knownIds.has(id)) S.selectedDownloads.delete(id);
  }

  tbody.innerHTML = all.map(e => {
    const stCls = e.status === 'done' ? 'dl-st-done' : e.status === 'downloading' ? 'dl-st-active' : 'dl-st-queued';
    const stTxt = e.status === 'done' ? t('downloads.completed') : e.status === 'downloading' ? t('downloads.downloading') : t('downloads.queued');
    const progHtml = e.status !== 'done'
      ? `<div style="display:flex;align-items:center;gap:6px">
           <div class="dl-bar" style="flex:1;width:auto"><div class="dl-bar-fill" style="width:${e.pct}%"></div></div>
           <span style="font-size:.71rem;color:var(--text-3);width:32px;text-align:right">${e.pct}%</span>
         </div>`
      : '—';
    const isDone = e.status === 'done';
    const checked = S.selectedDownloads.has(e.id) ? ' checked' : '';
    const chkCell = `<input type="checkbox" class="dl-row-chk" data-status="${e.status}"${checked} onchange="toggleDownloadSelect(${e.id},this.checked)">`;
    const actions = isDone
      ? `<button class="dl-act dl-act-dl" onclick="downloadBlob(${e.id})" title="${esc(t('dl.downloadTitle'))}">⬇</button>
         <button class="dl-act dl-act-del" onclick="deleteLocalFile(${e.id})" title="${esc(t('dl.deleteTitle'))}">🗑</button>`
      : `<button class="dl-act dl-act-del" onclick="cancelDownload(${e.id})" title="${esc(t('dl.cancelTitle'))}">✕</button>`;
    return `<tr>
      <td class="chk-cell">${chkCell}</td>
      <td title="${esc(e.name||'')}">${esc(e.name || t('common.fileId', { n: e.id }))}</td>
      <td>${fmtSize(e.size)}</td>
      <td>${esc(e.group||'')}</td>
      <td>${e.downloaded_at ? fmtDate(e.downloaded_at) : '—'}</td>
      <td>${progHtml}</td>
      <td><span class="${stCls}">${stTxt}</span></td>
      <td>${actions}</td>
    </tr>`;
  }).join('');

  updateDownloadBulkBar();
}

function _selectedDownloadIdsByStatus() {
  // Walk the currently rendered checkboxes since their data-status attribute
  // is the source of truth for whether each selected row is in-flight or done
  const done = [], inflight = [];
  document.querySelectorAll('.dl-row-chk').forEach(chk => {
    if (!chk.checked) return;
    const id = parseInt(chk.getAttribute('onchange').match(/\d+/)[0], 10);
    if (chk.dataset.status === 'done') done.push(id);
    else inflight.push(id);
  });
  return { done, inflight };
}

function toggleDownloadSelect(id, checked) {
  if (checked) S.selectedDownloads.add(id); else S.selectedDownloads.delete(id);
  updateDownloadBulkBar();
}

function toggleAllDownloads(checked) {
  S.selectedDownloads.clear();
  if (checked) {
    document.querySelectorAll('.dl-row-chk').forEach(chk => {
      const id = parseInt(chk.getAttribute('onchange').match(/\d+/)[0], 10);
      S.selectedDownloads.add(id);
    });
  }
  document.querySelectorAll('.dl-row-chk').forEach(c => { c.checked = checked; });
  updateDownloadBulkBar();
}

function updateDownloadBulkBar() {
  const bar = document.getElementById('dl-bulk-actions');
  const cnt = document.getElementById('dl-bulk-count');
  if (!bar) return;
  const n = S.selectedDownloads.size;
  bar.style.display = n > 0 ? 'inline-flex' : 'none';
  if (cnt) cnt.textContent = t('dl.filesSelected', { n });
  const { done, inflight } = _selectedDownloadIdsByStatus();
  const dlBtn = document.getElementById('dl-bulk-dl');
  const delBtn = document.getElementById('dl-bulk-del');
  const cancelBtn = document.getElementById('dl-bulk-cancel');
  if (dlBtn)     dlBtn.style.display     = done.length     ? '' : 'none';
  if (delBtn)    delBtn.style.display    = done.length     ? '' : 'none';
  if (cancelBtn) cancelBtn.style.display = inflight.length ? '' : 'none';
  const all = document.getElementById('dl-select-all');
  const totalRows = document.querySelectorAll('.dl-row-chk').length;
  if (all) all.checked = totalRows > 0 && n === totalRows;
}

function clearDownloadSelection() {
  S.selectedDownloads.clear();
  document.querySelectorAll('.dl-row-chk').forEach(c => { c.checked = false; });
  const all = document.getElementById('dl-select-all');
  if (all) all.checked = false;
  updateDownloadBulkBar();
}

function bulkDownloadDownloaded() {
  const { done } = _selectedDownloadIdsByStatus();
  if (!done.length) return;
  done.forEach((id, i) => setTimeout(() => downloadBlob(id), i * 200));
  showToast(t('dl.downloading', { n: done.length }), 2500);
}

async function bulkDeleteDownloaded() {
  const { done } = _selectedDownloadIdsByStatus();
  if (!done.length) return;
  if (!confirm(t('dl.deleteConfirmBulk', { n: done.length }))) return;
  let failed = 0;
  for (const id of done) {
    try { await api(`/api/files/${id}/local`, { method: 'DELETE' }); }
    catch (e) { failed++; }
  }
  done.forEach(id => S.selectedDownloads.delete(id));
  await loadDownloadsList();
  loadFiles();
  if (failed) showToast(t('dl.deletedSome', { ok: done.length - failed, fail: failed }), 4000);
  else showToast(t('dl.deletedBulk', { n: done.length }), 2500);
}

async function cancelDownload(fileId) {
  try {
    await api(`/api/files/${fileId}/cancel`, { method: 'POST' });
  } catch (e) {
    showToast(t('dl.cancelFail') + ' ' + esc(e.message), 4000);
    return;
  }
  // Remove the in-flight entry from the local history; the next poll for that
  // file (if it was running) will see downloading=false and stop on its own.
  _dlHistory = _dlHistory.filter(en => en.id !== fileId);
  delete S.dlQueue[fileId];
  if (S.polls[fileId]) { clearInterval(S.polls[fileId]); delete S.polls[fileId]; }
  S.selectedDownloads.delete(fileId);
  if (S.activeTab === 'downloads') renderDownloadsTab();
  loadFiles();
  showToast(t('dl.cancelOk'), 2500);
}

async function bulkCancelDownloads() {
  const { inflight } = _selectedDownloadIdsByStatus();
  if (!inflight.length) return;
  if (!confirm(t('dl.cancelConfirmBulk', { n: inflight.length }))) return;
  let failed = 0;
  for (const id of inflight) {
    try { await api(`/api/files/${id}/cancel`, { method: 'POST' }); }
    catch (e) { failed++; }
    _dlHistory = _dlHistory.filter(en => en.id !== id);
    delete S.dlQueue[id];
    if (S.polls[id]) { clearInterval(S.polls[id]); delete S.polls[id]; }
    S.selectedDownloads.delete(id);
  }
  if (S.activeTab === 'downloads') renderDownloadsTab();
  loadFiles();
  if (failed) showToast(t('dl.cancelSome', { ok: inflight.length - failed, fail: failed }), 4000);
  else        showToast(t('dl.cancelBulkOk', { n: inflight.length }), 2500);
}

function downloadBlob(fileId) {
  // Trigger browser download by navigating to the streaming endpoint
  const a = document.createElement('a');
  a.href = `/api/files/${fileId}/blob`;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function deleteLocalFile(fileId) {
  if (!confirm(t('dl.deleteFileConfirm'))) return;
  try {
    await api(`/api/files/${fileId}/local`, { method: 'DELETE' });
  } catch (e) {
    showToast(t('dl.deleteFileFail') + ' ' + esc(e.message));
    return;
  }
  _dlHistory = _dlHistory.filter(e => e.id !== fileId);
  await loadDownloadsList();
  loadFiles();
}

// ── Links ─────────────────────────────────────────────────────────────────────
let _debounceLinksTimer;
function debouncedLoadLinks() {
  clearTimeout(_debounceLinksTimer);
  _debounceLinksTimer = setTimeout(loadLinks, 280);
}

async function loadLinks(silent = false) {
  const v  = (id) => (document.getElementById(id)?.value || '').trim();
  const p  = new URLSearchParams({
    q:        v('link-search'),
    platform: v('lcol-platform'),
    sort_by:  S.linkSortBy,
    sort_dir: S.linkSortDir,
    limit:    S.linkLimit,
    offset:   S.linkOffset,
  });
  if (S.activeGroupId != null) p.set('group_id', S.activeGroupId);
  // Per-column filters (sent only when non-empty so the API treats them as absent)
  const urlF  = v('lcol-url');         if (urlF)  p.set('url_filter', urlF);
  const ctxF  = v('lcol-context');     if (ctxF)  p.set('context_filter', ctxF);
  const grpF  = v('lcol-group');       if (grpF)  p.set('group_filter', grpF);
  const fnameF = v('lcol-files-name'); if (fnameF) p.set('file_name_filter', fnameF);
  const dfrom = v('lcol-date-from');   if (dfrom) p.set('date_from', dfrom);
  const dto   = v('lcol-date-to');     if (dto)   p.set('date_to',   dto);

  if (!silent) _paintGridLoading('links-body', 8);
  const data = await api('/api/links?' + p);
  renderLinks(data.links);
  const lc = document.getElementById('link-flt-count');
  if (lc) lc.textContent = t("filter.linkCount", {n: (data.total || 0).toLocaleString()});
  _updateLinkSortArrows();
  renderPagination(data.total, S.linkLimit, S.linkOffset);
}

function linkSortBy(col) {
  S.linkSortDir = (S.linkSortBy === col) ? (S.linkSortDir === 'asc' ? 'desc' : 'asc') : 'asc';
  S.linkSortBy  = col;
  S.linkOffset  = 0;
  loadLinks();
}

function _updateLinkSortArrows() {
  ['url','platform','files','group','date','context'].forEach(c => {
    const el = document.getElementById('larr-' + c);
    if (!el) return;
    el.textContent = (S.linkSortBy === c) ? (S.linkSortDir === 'asc' ? '▲' : '▼') : '▲▼';
    el.classList.toggle('active', S.linkSortBy === c);
  });
}

function cleanUrl(raw) {
  if (!raw) return '';
  // Strip zero-width and soft hyphen characters that often pollute Telegram URLs
  let s = String(raw).replace(/[​-‍﻿­]/g, '').trim();
  // Find the first http(s):// occurrence and take from there
  const m = s.match(/https?:\/\/[^\s<>"'` ]+/i);
  if (m) {
    // Trim trailing punctuation accidentally appended to the URL
    return m[0].replace(/[\s.,;:!?)\]}>'"`]+$/, '');
  }
  return s;
}

function _linkFilesCell(l) {
  // Three states from the prober:
  //   probed_at IS NULL              → not yet visited (queued)
  //   available IS NOT NULL && false → confirmed dead (filtered out by API)
  //   available is null + probed_at  → unsupported provider
  //   files_json non-empty           → actual file list
  if (!l.probed_at) {
    return `<span class="link-files-pending" title="${esc(t('links.notScanned'))}">…</span>`;
  }
  let files = l.files_json;
  if (typeof files === 'string') {
    try { files = JSON.parse(files); } catch(e) { files = []; }
  }
  if (!Array.isArray(files) || files.length === 0) {
    if (l.available === false) {
      return `<span class="link-files-dead" title="${esc(t('links.noAccess'))}">${esc(t('links.noAccessText'))}</span>`;
    }
    return `<span class="link-files-unknown" title="${esc(t('links.unsupported'))}">—</span>`;
  }
  const totalSz = +(l.file_size_total || 0);
  const sizeStr = totalSz > 0 ? ' · ' + fmtSize(totalSz) : '';
  // Tooltip: up to 20 file lines as a stacked list. Newlines render as
  // separate lines inside the native title attribute.
  const lines = files.slice(0, 20).map(f => {
    const sz = f.size ? ' (' + fmtSize(f.size) + ')' : '';
    return (cleanText(f.name) || '?') + sz;
  });
  if (files.length > 20) lines.push(`+${files.length - 20} daha`);
  const tip = lines.join('\n');
  const headline = cleanText(files[0]?.name || '');
  const visible = files.length === 1
    ? `${esc(headline.substring(0, 28))}`
    : `${files.length} dosya${sizeStr}`;
  return `<span class="link-files-ok" title="${esc(tip)}">${visible}</span>`;
}

function renderLinks(links) {
  // Normalize URLs once so display, copy, and external open all use the same cleaned form
  links.forEach(l => { l.url = cleanUrl(l.url); });
  _currentLinks = links;
  const tbody = document.getElementById('links-body');
  if (!links.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="no-data">${esc(t("table.noLinks"))}</td></tr>`;
    return;
  }
  tbody.innerHTML = links.map((l, i) => {
    const rowNum  = S.linkOffset + i + 1;
    const checked = S.selectedLinks.has(l.id) ? ' checked' : '';
    const shortUrl = l.url.replace(/^https?:\/\//,'').substring(0,55);
    // Same URL re-posted across multiple messages collapses into one row;
    // surface the underlying count so the user knows it's not a single shot.
    const dupBadge = (l.appearances && l.appearances > 1)
      ? ` <span class="link-dup-badge" title="${esc(t('table.appearances', { n: l.appearances }))}">×${l.appearances}</span>`
      : '';
    // URL'ler zaten ASCII; group_name ve context Telegram'dan geldiği için
    // emoji/biçim temizliği uygulanır.
    const gName  = cleanText(l.group_name || '');
    const ctxRaw = cleanText(l.context || '');
    return `<tr>
      <td class="chk-cell"><input type="checkbox" class="link-chk" data-lid="${l.id}"${checked}></td>
      <td class="num-cell">${rowNum}</td>
      <td title="${esc(l.url)}"><a href="${esc(l.url)}" target="_blank" rel="noopener" style="color:#2563eb">${esc(shortUrl)}</a>${dupBadge}</td>
      <td>${platBadge(l.platform)}</td>
      <td class="link-files-cell">${_linkFilesCell(l)}</td>
      <td>${esc(gName)}</td>
      <td>${fmtDate(l.date)}</td>
      <td class="ctx-cell" title="${esc(ctxRaw)}">${esc(ctxRaw.substring(0,40))}</td>
    </tr>`;
  }).join('');
  // Direct per-checkbox click listeners — gives us a real DOM event with shiftKey.
  document.querySelectorAll('#links-body .link-chk').forEach(cb => {
    cb.addEventListener('click', _linkCbClick);
  });
  updateBulkLinkBtn();
}

let _lastLinkToggleId = null;

function _linksVisibleRowIds() {
  return [...document.querySelectorAll('#links-body .link-chk')]
    .map(cb => parseInt(cb.getAttribute('data-lid'), 10))
    .filter(x => Number.isFinite(x));
}

function _linkCbClick(ev) {
  const cb = ev.currentTarget;
  ev.stopPropagation();
  const id = parseInt(cb.getAttribute('data-lid'), 10);
  if (!Number.isFinite(id)) return;
  toggleLinkSelect(id, cb.checked, ev);
}

function toggleLinkSelect(id, checked, e) {
  // Shift+click extends across the visible range.
  if (e && e.shiftKey && _lastLinkToggleId != null && _lastLinkToggleId !== id) {
    const ids = _linksVisibleRowIds();
    const a = ids.indexOf(_lastLinkToggleId);
    const b = ids.indexOf(id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a <= b ? [a, b] : [b, a];
      const sel = window.getSelection && window.getSelection();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
      const boxes = [...document.querySelectorAll('#links-body .link-chk')];
      for (let i = lo; i <= hi; i++) {
        const rid = ids[i];
        if (checked) S.selectedLinks.add(rid);
        else        S.selectedLinks.delete(rid);
        const box = boxes[i];
        if (box) box.checked = checked;
      }
      updateBulkLinkBtn();
      _lastLinkToggleId = id;
      return;
    }
  }
  if (checked) S.selectedLinks.add(id); else S.selectedLinks.delete(id);
  _lastLinkToggleId = id;
  updateBulkLinkBtn();
}

function toggleAllLinks(checked) {
  S.selectedLinks.clear();
  if (checked) {
    _currentLinks.forEach(l => S.selectedLinks.add(l.id));
  }
  document.querySelectorAll('.link-chk').forEach(chk => { chk.checked = checked; });
  updateBulkLinkBtn();
}

function updateBulkLinkBtn() {
  const btn = document.getElementById('bulk-copy-btn');
  if (!btn) return;
  if (S.activeTab !== 'links') { btn.style.display = 'none'; return; }
  const n = _currentLinks.filter(l => S.selectedLinks.has(l.id)).length;
  btn.style.display = n > 0 ? 'inline-flex' : 'none';
  const cnt = document.getElementById('bulk-copy-count');
  if (cnt) cnt.textContent = n;
}

async function copyLinksToClipboard() {
  const selected = _currentLinks.filter(l => S.selectedLinks.has(l.id));
  const text = selected.map(l => l.url).join('\n');
  try {
    await navigator.clipboard.writeText(text);
    const btn = document.getElementById('bulk-copy-btn');
    if (btn) {
      const orig = btn.innerHTML;
      btn.textContent = t('bulk.copied', {n: selected.length});
      setTimeout(() => { btn.innerHTML = orig; }, 1800);
    }
  } catch(e) {
    alert(t('bulk.copyFail') + ': ' + e.message);
  }
}

function platBadge(p) {
  const cls={'Google Drive':'plat-gdrive','Mega':'plat-mega','MediaFire':'plat-mediafire',
    'OneDrive':'plat-onedrive','Dropbox':'plat-dropbox','YouTube':'plat-youtube','GitHub':'plat-github'}[p]||'plat-other';
  return `<span class="plat-badge ${cls}">${esc(p||'—')}</span>`;
}

function setLinkLimit(v) { S.linkLimit=parseInt(v); S.linkOffset=0; loadLinks(); }

// ── Sort / filter ─────────────────────────────────────────────────────────────
function sortBy(col) {
  S.sortDir = S.sortBy===col?(S.sortDir==='asc'?'desc':'asc'):'desc';
  S.sortBy=col; S.offset=0;
  updateSortArrows(); loadFiles();
}
function updateSortArrows() {
  ['name','group','size','date'].forEach(c => {
    const el=document.getElementById('arr-'+c);
    if (!el) return;
    el.textContent = S.sortBy===c?(S.sortDir==='asc'?'▲':'▼'):'▲▼';
    el.classList.toggle('active', S.sortBy===c);
  });
}

function filterByExt(ext) {
  S.extChip=ext; S.typeGroup=''; S.offset=0;
  document.querySelectorAll('.type-btn').forEach(b=>b.classList.toggle('active',b.dataset.group===''));
  renderChips(); loadFiles();
}
function filterByGroup(id) { S.activeGroupId=id; S.offset=0; renderChips(); loadFiles(); }
function clearGroupFilter() { S.activeGroupId=null; S.offset=0; renderChips(); loadFiles(); }
function renderChips() {
  const chips=[];
  if (S.extChip) chips.push(`<span class="chip" onclick="filterByExt('')">${esc(S.extChip.toUpperCase())}</span>`);
  if (S.activeGroupId!=null) {
    const g = _groups.find(x=>x.id===S.activeGroupId);
    const name = g ? (g.display_name||g.name) : `#${S.activeGroupId}`;
    chips.push(`<span class="chip" onclick="clearGroupFilter()" title="${esc(t('filter.removeGroup'))}">📁 ${esc(name)}</span>`);
  }
  if (S.fileIdsFilter && S.fileIdsFilter.size > 0) {
    const lbl = S.fileIdsFilterLabel ? `🔔 ${esc(S.fileIdsFilterLabel)} (${S.fileIdsFilter.size})` : `🔔 ${esc(t('accounts.fileCount', { n: S.fileIdsFilter.size }))}`;
    chips.push(`<span class="chip" onclick="clearFileIdsFilter()" title="${esc(t('filter.removeNotif'))}">${lbl}</span>`);
  }
  document.getElementById('chip-list').innerHTML=chips.join('');
}

// ── Pagination ────────────────────────────────────────────────────────────────
function gotoPage(pg) {
  if (S.activeTab==='files') { S.offset=pg*S.limit; loadFiles(); }
  else                        { S.linkOffset=pg*S.linkLimit; loadLinks(); }
}

function setPagLimit(v) {
  if (S.activeTab==='files') { S.limit=parseInt(v); S.offset=0; loadFiles(); }
  else { S.linkLimit=parseInt(v); S.linkOffset=0; loadLinks(); }
}

function renderPagination(total, limit, offset) {
  const totalPages = Math.max(1, Math.ceil(total/limit));
  const current    = Math.floor(offset/limit);
  const limitVal   = S.activeTab==='files'?S.limit:S.linkLimit;
  const el         = document.getElementById('pagination');

  let html = '';
  if (S.activeTab === 'files') {
    html += `<button id="bulk-dl-btn" onclick="bulkDownloadSelected()"></button>`;
  } else if (S.activeTab === 'links') {
    html += `<button id="bulk-copy-btn" onclick="copyLinksToClipboard()"><span class="i18n-bcopy">📋 Panoya Kopyala (</span><span id="bulk-copy-count">0</span>)</button>`;
  }
  html += `<select id="pag-limit" onchange="setPagLimit(this.value)" style="margin-left:auto">
    <option value="100"${limitVal==100?' selected':''}>100</option>
    <option value="500"${limitVal==500?' selected':''}>500</option>
    <option value="1000"${limitVal==1000?' selected':''}>1000</option>
  </select>`;
  html += `<button class="pg-btn" onclick="gotoPage(${current-1})" ${current===0?'disabled':''}>‹</button>`;
  buildPageList(current, totalPages).forEach(p => {
    if (p==='…') { html+=`<span class="pg-ellipsis">…</span>`; return; }
    html+=`<button class="pg-btn${p===current?' active':''}" onclick="gotoPage(${p})">${p+1}</button>`;
  });
  html += `<button class="pg-btn" onclick="gotoPage(${current+1})" ${current>=totalPages-1?'disabled':''}>›</button>`;
  el.innerHTML = html;
  applySyncStatusToUI();
  updateBulkFileBtn();
  updateBulkLinkBtn();
}

function buildPageList(cur, total) {
  if (total<=9) return Array.from({length:total},(_,i)=>i);
  const set=new Set([0,total-1,cur,cur-1,cur+1,cur-2,cur+2].filter(p=>p>=0&&p<total));
  const sorted=[...set].sort((a,b)=>a-b);
  const result=[];
  sorted.forEach((p,i)=>{if(i>0&&p-sorted[i-1]>1)result.push('…');result.push(p);});
  return result;
}

// ── Telegram link ─────────────────────────────────────────────────────────────
function tgLink(f) {
  if (!f.message_id) return '';
  const gid=String(f.group_id);
  const href=f.group_username
    ?`https://t.me/${f.group_username}/${f.message_id}`
    :`https://t.me/c/${gid.startsWith('-100')?gid.slice(4):gid.replace('-','')}/${f.message_id}`;
  return `<a href="${href}" target="_blank" rel="noopener" title="${esc(t('table.openTg'))}" style="color:#9ca3af;font-size:.82em;margin-left:3px">↗</a>`;
}

function tgGroupHref(g) {
  if (g && g.username) return `https://t.me/${g.username}`;
  const gid = String(g && g.id != null ? g.id : '');
  const slug = gid.startsWith('-100') ? gid.slice(4) : gid.replace(/^-/, '');
  return `https://t.me/c/${slug}`;
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// Strip emoji + decorative codepoints from text that came from Telegram
// message bodies / channel names / file names so grid cells render as
// plain text. Removes:
//   • Extended_Pictographic (emoji, symbols, dingbats, transport, etc.)
//   • Regional Indicator pairs (flag emoji)
//   • Skin-tone modifiers (U+1F3FB–U+1F3FF)
//   • Variation selectors + ZWJ + bidi/format controls (invisible glue)
// Then collapses any whitespace runs the removals left behind. Used by
// the Files and Links grids; the underlying DB values stay intact.
function cleanText(s) {
  if (s == null || s === '') return '';
  return String(s)
    .replace(/\p{Extended_Pictographic}/gu, '')
    .replace(/[\u{1F1E6}-\u{1F1FF}]/gu, '')
    .replace(/[\u{1F3FB}-\u{1F3FF}]/gu, '')
    .replace(/[​-‏‪-‮⁠-⁤︀-️]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

// Show a single-row spinner inside any tbody while its data fetch is in flight.
// Replaced by the next innerHTML write in the corresponding render*() function.
function _paintGridLoading(tbodyId, colspan) {
  const tb = document.getElementById(tbodyId);
  if (!tb) return;
  tb.innerHTML = `<tr class="grid-loading-row"><td colspan="${colspan}">
    <span class="gl-inner"><span class="hd-spinner"></span> ${esc(t('common.loadingData'))}</span>
  </td></tr>`;
}
function fmtSize(b){if(!b)return'—';if(b>=1073741824)return(b/1073741824).toFixed(1)+' GB';if(b>=1048576)return(b/1048576).toFixed(1)+' MB';if(b>=1024)return(b/1024).toFixed(0)+' KB';return b+' B';}
function fmtDate(s){
  if(!s) return '—';
  const d = new Date(s);
  if(isNaN(d)) return s.substring(0,10);
  const pad = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const EXT_COLORS={
  zip:'#f59e0b,#fff8e7',rar:'#ef4444,#fff0f0','7z':'#8b5cf6,#f5f0ff',
  tar:'#6b7280,#f3f4f6',gz:'#6b7280,#f3f4f6',bz2:'#6b7280,#f3f4f6',xz:'#6b7280,#f3f4f6',
  pdf:'#dc2626,#fff0f0',doc:'#2563eb,#eff6ff',docx:'#2563eb,#eff6ff',
  xls:'#16a34a,#f0fdf4',xlsx:'#16a34a,#f0fdf4',ppt:'#ea580c,#fff7ed',pptx:'#ea580c,#fff7ed',
  mp4:'#7c3aed,#f5f3ff',mkv:'#7c3aed,#f5f3ff',avi:'#7c3aed,#f5f3ff',mov:'#7c3aed,#f5f3ff',
  mp3:'#0891b2,#ecfeff',flac:'#0891b2,#ecfeff',wav:'#0891b2,#ecfeff',aac:'#0891b2,#ecfeff',
  jpg:'#059669,#ecfdf5',jpeg:'#059669,#ecfdf5',png:'#059669,#ecfdf5',gif:'#d97706,#fffbeb',
  iso:'#374151,#f9fafb',exe:'#b45309,#fefce8',apk:'#15803d,#f0fdf4',dmg:'#374151,#f9fafb',
  epub:'#7c3aed,#f5f0ff',txt:'#374151,#f9fafb',torrent:'#9333ea,#faf5ff',
};
function extColor(ext){const v=EXT_COLORS[(ext||'').toLowerCase()]||'#6b7280,#f3f4f6';const[fg,bg]=v.split(',');return`color:${fg};background:${bg}`;}

async function api(url,opts={}){
  const init={method:opts.method||'GET',headers:{}};
  if(opts.json){init.headers['Content-Type']='application/json';init.body=JSON.stringify(opts.json);}
  const res=await fetch(url,init);
  // UI session expired / never present → drop the user back at the greeter.
  // The login endpoint also returns 401 on bad password; we don't bounce
  // there because the greeter is already visible and will show the error.
  if (res.status === 401 && !url.startsWith('/api/uiauth/')) {
    try { hide('app-shell'); } catch (e) {}
    try { show('ui-greeter'); setTimeout(() => document.getElementById('ug-pass')?.focus(), 30); } catch (e) {}
    throw new Error('UI session expired');
  }
  if(!res.ok){const err=await res.json().catch(()=>({detail:res.statusText}));throw new Error(err.detail||res.statusText);}
  return res.json();
}

updateSortArrows();
document.querySelector('.type-btn[data-group=""]').classList.add('active');


// ── Watch terms & notifications ──────────────────────────────────────────────
let _watches = [];
let _activeNotifications = [];
let _allNotifications = [];

async function loadWatches() {
  try {
    _watches = await api('/api/watches');
  } catch (e) { _watches = []; }
  renderWatches();
}

let _watchSortBy  = 'created_at';
let _watchSortDir = 'desc';

function watchSort(col) {
  if (_watchSortBy === col) _watchSortDir = _watchSortDir === 'asc' ? 'desc' : 'asc';
  else { _watchSortBy = col; _watchSortDir = (col === 'keywords') ? 'asc' : 'desc'; }
  renderWatches();
}

function _wgArrow(col) {
  return _watchSortBy === col ? (_watchSortDir === 'asc' ? '▲' : '▼') : '▲▼';
}

function renderWatches() {
  const el = document.getElementById('watches-list');
  if (!el) return;
  if (!_watches.length) {
    el.innerHTML = `<div style="font-size:.78rem;color:var(--text-4);padding:14px 4px;text-align:center">${esc(t("watch.empty"))}</div>`;
    return;
  }

  // Sort
  const dir = _watchSortDir === 'asc' ? 1 : -1;
  const rows = [..._watches].sort((a, b) => {
    let va, vb;
    switch (_watchSortBy) {
      case 'keywords':         va=(a.keywords||'').toLowerCase(); vb=(b.keywords||'').toLowerCase(); break;
      case 'matches':          va=a.active_match_count||0; vb=b.active_match_count||0; break;
      case 'last_match':       va=a.active_last_match_at?Date.parse(a.active_last_match_at):0; vb=b.active_last_match_at?Date.parse(b.active_last_match_at):0; break;
      default:                 va=a.created_at?Date.parse(a.created_at):0; vb=b.created_at?Date.parse(b.created_at):0; break;
    }
    if (va < vb) return -1*dir;
    if (va > vb) return  1*dir;
    return 0;
  });

  el.innerHTML = `
    <table class="wg-table">
      <thead><tr>
        <th class="wg-sortable" onclick="watchSort('keywords')">${esc(t('watch.colKeywords'))} <span class="sort-arrow">${_wgArrow('keywords')}</span></th>
        <th class="wg-sortable" onclick="watchSort('matches')" style="width:100px">${esc(t('watch.colMatches'))} <span class="sort-arrow">${_wgArrow('matches')}</span></th>
        <th class="wg-sortable" onclick="watchSort('created_at')" style="width:152px">${esc(t('watch.colCreated'))} <span class="sort-arrow">${_wgArrow('created_at')}</span></th>
        <th class="wg-sortable" onclick="watchSort('last_match')" style="width:152px">${esc(t('watch.colLastMatch'))} <span class="sort-arrow">${_wgArrow('last_match')}</span></th>
        <th style="width:185px">${esc(t('hg.actions'))}</th>
      </tr></thead>
      <tbody>${rows.map(w => {
        const created = w.created_at ? fmtDate(w.created_at).substring(0,16) : '—';
        const last    = w.active_last_match_at ? fmtDate(w.active_last_match_at).substring(0,16) : '—';
        const matchN  = w.active_match_count || 0;
        const cntCls  = matchN > 0 ? 'wg-count' : 'wg-count wg-count-zero';
        const showBtn = matchN > 0
          ? `<button class="wg-btn wg-btn-show" onclick="showWatchMatches(${w.id})">${esc(t('watch.view'))}</button>
             <button class="wg-btn" onclick="dismissWatchNotif(${w.active_notification_id})">${esc(t('watch.dismiss'))}</button>`
          : `<span style="color:var(--text-4);font-size:.7rem">—</span>`;
        return `<tr>
          <td><code class="wg-kw">${esc(w.keywords)}</code></td>
          <td><span class="${cntCls}">${matchN}</span></td>
          <td class="wg-time">${esc(created)}</td>
          <td class="wg-time">${esc(last)}</td>
          <td><div class="wg-acts">${showBtn}<button class="wg-btn wg-btn-del" onclick="deleteWatch(${w.id})" title="${esc(t('watch.deleteTip'))}">🗑</button></div></td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

async function addWatch() {
  const inp = document.getElementById('watch-input');
  const kw  = (inp.value || '').trim();
  if (!kw) return;
  try {
    await api('/api/watches', { method:'POST', json:{ keywords: kw } });
    inp.value = '';
    await loadWatches();
    await loadActiveNotifications();
  } catch (e) {
    alert(t('watch.cantAdd') + ': ' + e.message);
  }
}

async function deleteWatch(id) {
  if (!confirm(t('watch.confirmDelete'))) return;
  await api(`/api/watches/${id}`, { method:'DELETE' });
  await loadWatches();
  await loadActiveNotifications();
}

async function loadActiveNotifications() {
  try {
    _activeNotifications = await api('/api/notifications?active_only=true');
  } catch (e) { _activeNotifications = []; }
  renderWatchBanner();
  updateSettingsWatchBadge();
}

async function loadAllNotifications() {
  try {
    _allNotifications = await api('/api/notifications?active_only=false');
  } catch (e) { _allNotifications = []; }
  renderNotificationLog();
}

function renderNotificationLog() {
  const el = document.getElementById('notif-log');
  if (!el) return;
  if (!_allNotifications.length) {
    el.innerHTML = `<div style="font-size:.78rem;color:var(--text-4);padding:8px 4px">${esc(t("watch.notifEmpty"))}</div>`;
    return;
  }
  el.innerHTML = _allNotifications.map(n => {
    const time = new Date(n.last_match_at).toLocaleString(_locale());
    const isDismissed = !!n.dismissed_at;
    const groups = (n.group_names || []);
    const groupsHtml = groups.length
      ? `<span class="nl-groups" title="${esc(groups.join(', '))}">${groups.slice(0,4).map(g=>`<span class="nl-grp-pill">${esc(g)}</span>`).join('')}${groups.length>4?` <span style="color:var(--text-4)">+${groups.length-4}</span>`:''}</span>`
      : '';
    return `<div class="notif-row${isDismissed?' dismissed':''}">
      <span class="nl-time">${esc(time)}</span>
      <span class="nl-kw"><code>${esc(n.keywords)}</code></span>
      <span class="nl-cnt">${n.match_count} ${esc(t("watch.newFiles"))}</span>
      ${groupsHtml}
      <span class="nl-actions">
        ${!isDismissed ? `<button class="wr-btn" onclick="showNotifMatches(${n.id})">${esc(t("watch.view"))}</button>
         <button class="wr-btn" onclick="dismissNotifAndReload(${n.id})">${esc(t("watch.dismiss"))}</button>` : `<span style="color:var(--text-4);font-size:.7rem">${esc(t("common.dismissed"))}</span>`}
      </span>
    </div>`;
  }).join('');
}

function renderWatchBanner() {
  const el = document.getElementById('watch-banner');
  if (!el) return;
  const onFiles = S.activeTab === 'files';
  if (!onFiles || !_activeNotifications.length) {
    el.style.display = 'none';
    return;
  }
  const total = _activeNotifications.reduce((s,n) => s + (n.match_count||0), 0);
  el.style.display = 'flex';
  const items = _activeNotifications.map(n => {
    const groups = (n.group_names || []);
    const grpTip = groups.length ? ` title="${esc(groups.join(', '))}"` : '';
    const grpInline = groups.length
      ? `<span class="wb-grp">${esc(groups.slice(0,2).join(', '))}${groups.length>2?` +${groups.length-2}`:''}</span>`
      : '';
    return `<span class="wb-item"${grpTip}>
       <code>${esc(n.keywords)}</code>
       <span class="wb-cnt">${n.match_count}</span>
       ${grpInline}
       <span class="wb-show" onclick="showNotifMatches(${n.id})">${esc(t("watch.show"))}</span>
       <span class="wb-x" onclick="dismissNotifAndReload(${n.id})" title="${esc(t("watch.dismiss"))}">×</span>
     </span>`;
  }).join('');
  el.innerHTML = `
    <span class="wb-bell">🔔</span>
    <span class="wb-title">${esc(t("watch.newMatches",{n:total}))}</span>
    <span class="wb-list">${items}</span>
  `;
}

function updateSettingsWatchBadge() {
  const badge = document.getElementById('settings-watch-count');
  if (!badge) return;
  const n = _activeNotifications.length;
  if (n > 0) {
    badge.textContent = n;
    badge.style.display = 'inline-flex';
  } else {
    badge.style.display = 'none';
  }
}

function _findNotif(id) { return _activeNotifications.find(n => n.id === id) || _allNotifications.find(n => n.id === id); }

function showNotifMatches(notifId) {
  const n = _findNotif(notifId);
  if (!n) return;
  // Filter the files grid to ONLY the file_ids that triggered this notification.
  // ÖNCE her potansiyel olarak çelişen filtreyi temizle — yoksa kullanıcının
  // önceki seçtiği ext/group/size/date filtresi file_ids ile AND'lenip 0
  // sonuç döndürebiliyor ve grid boş geliyor.
  S.typeGroup = '';
  S.extChip = '';
  S.activeGroupId = null;
  S.colGroupIds.clear();
  S.sizeMinMB = null;
  S.sizeMaxMB = null;
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  setVal('col-name', '');
  setVal('ext-input', '');
  setVal('col-ext', '');
  setVal('col-size-min', '');
  setVal('col-size-max', '');
  setVal('date-from', '');
  setVal('date-to', '');
  // Size slider'ı sıfırla
  const sMin = document.getElementById('sl-min'); if (sMin) sMin.value = 0;
  const sMax = document.getElementById('sl-max'); if (sMax) sMax.value = 1000;
  if (typeof updateSliderFill === 'function') updateSliderFill();
  // "Tüm Tipler" butonunu aktif yap, diğerlerini pasif
  document.querySelectorAll('.type-btn').forEach(b =>
    b.classList.toggle('active', (b.dataset.group || '') === ''));
  if (typeof cgfUpdateLabel === 'function') cgfUpdateLabel();
  // Sonra notification filtresini uygula
  S.fileIdsFilter = new Set((n.file_ids || []).map(x => parseInt(x, 10)));
  S.fileIdsFilterLabel = n.keywords;
  S.fileIdsFilterNotifId = notifId;
  S.offset = 0;
  renderChips();
  switchTab('files');
}

function clearFileIdsFilter() {
  S.fileIdsFilter = null;
  S.fileIdsFilterLabel = null;
  S.fileIdsFilterNotifId = null;
  S.offset = 0;
  renderChips();
  loadFiles();
}

function showWatchMatches(watchId) {
  const w = _watches.find(x => x.id === watchId);
  if (!w) return;
  const cn = document.getElementById('col-name');
  if (cn) cn.value = w.keywords;
  S.offset = 0;
  switchTab('files');
  loadFiles();
}

async function dismissWatchNotif(notifId) {
  if (!notifId) return;
  await api(`/api/notifications/${notifId}/dismiss`, { method:'POST' });
  await loadWatches();
  await loadActiveNotifications();
  if (document.getElementById('settings-tab-watches')?.classList.contains('active')) {
    loadAllNotifications();
  }
}

async function dismissNotifAndReload(notifId) {
  await dismissWatchNotif(notifId);
  if (S.fileIdsFilterNotifId === notifId) clearFileIdsFilter();
}


// ── Accounts management ───────────────────────────────────────────────────────
let _accounts = [];

async function loadAccountsList() {
  try {
    _accounts = await api('/api/accounts');
  } catch (e) {
    _accounts = [];
  }
  renderAccountsList();
}

function renderAccountsList() {
  const el = document.getElementById('accounts-list');
  if (!el) return;
  if (!_accounts.length) {
    el.innerHTML = `<div style="font-size:.78rem;color:var(--text-4);padding:8px 4px">${esc(t('accounts.notLoggedIn'))}</div>`;
    return;
  }
  el.innerHTML = _accounts.map(a => {
    const stCls = a.authorized ? 'ok' : 'no';
    const stTxt = a.authorized ? t('accounts.loggedIn') : t('accounts.notLoggedIn');
    const meta = `${esc(t('accounts.groupCount', {n: (a.group_count||0).toLocaleString()}))} · ${esc(t('accounts.fileCount', {n: (a.file_count||0).toLocaleString()}))}`;
    const phone = a.phone ? esc(a.phone) + ' · ' : '';
    const apiPart = `API ${a.api_id||'—'} · ${esc(a.api_hash_masked||'—')}`;
    const loginBtn = a.authorized
      ? `<button class="acc-btn" onclick="logoutAcc(${a.id})">${esc(t('accounts.logout'))}</button>`
      : `<button class="acc-btn" onclick="startLoginForAccount(${a.id},'settings')">${esc(t('accounts.login'))}</button>`;
    return `<div class="acc-row">
      <span class="acc-name">${esc(a.name)}</span>
      <span class="acc-meta">${phone}${apiPart} · ${meta}</span>
      <span class="acc-status ${stCls}">${esc(stTxt)}</span>
      <span class="acc-actions">
        ${loginBtn}
        <button class="acc-btn acc-btn-danger" onclick="deleteAcc(${a.id})">${esc(t('accounts.delete'))}</button>
      </span>
    </div>`;
  }).join('');
}

function openAddAccount() {
  document.getElementById('add-account-form').style.display = '';
  document.getElementById('new-acc-name').focus();
}

function closeAddAccount() {
  document.getElementById('add-account-form').style.display = 'none';
  document.getElementById('new-acc-name').value = '';
  document.getElementById('new-acc-api-id').value = '';
  document.getElementById('new-acc-api-hash').value = '';
}

async function submitAddAccount() {
  const name = document.getElementById('new-acc-name').value.trim();
  const apiId = parseInt(document.getElementById('new-acc-api-id').value.trim(), 10);
  const apiHash = document.getElementById('new-acc-api-hash').value.trim();
  if (!name || !apiId || !apiHash) {
    alert('name, API ID, API Hash');
    return;
  }
  try {
    const r = await api('/api/accounts', { method:'POST', json:{ name, api_id: apiId, api_hash: apiHash } });
    _setCredsSkipped(false);   // Hesap eklendi; "Şimdi geç" hafızası geçersiz.
    closeAddAccount();
    await loadAccountsList();
    // Immediately offer login for the new account
    startLoginForAccount(r.id, 'settings');
  } catch (e) {
    alert('Eklenemedi: ' + e.message);
  }
}

async function logoutAcc(accountId) {
  await api(`/api/auth/logout?account_id=${accountId}`, { method:'POST' });
  await loadAccountsList();
}

async function deleteAcc(accountId) {
  if (!confirm(t('accounts.confirmDelete'))) return;
  await api(`/api/accounts/${accountId}`, { method:'DELETE' });
  await loadAccountsList();
}


// ── Column group multi-select dropdown ────────────────────────────────────────
function cgfToggle(e) {
  if (e) e.stopPropagation();
  const dd = document.getElementById('cgf-dropdown');
  const open = dd.style.display !== 'none';
  if (open) {
    cgfClose();
  } else {
    cgfOpen();
  }
}

function cgfOpen() {
  const dd = document.getElementById('cgf-dropdown');
  dd.style.display = 'flex';
  cgfRenderList();
  setTimeout(() => document.getElementById('cgf-search')?.focus(), 30);
  document.addEventListener('mousedown', _cgfOutside, true);
}

function cgfClose() {
  const dd = document.getElementById('cgf-dropdown');
  if (dd) dd.style.display = 'none';
  document.removeEventListener('mousedown', _cgfOutside, true);
}

function _cgfOutside(e) {
  const dd = document.getElementById('cgf-dropdown');
  const trg = document.getElementById('cgf-trigger');
  if (!dd) return;
  if (dd.contains(e.target) || (trg && trg.contains(e.target))) return;
  cgfClose();
}

function cgfRenderList() {
  const list = document.getElementById('cgf-list');
  if (!list) return;
  const q = (document.getElementById('cgf-search')?.value || '').toLowerCase().trim();
  const groups = (_groups || []).filter(g => {
    if (g.hidden) return false;
    if (!q) return true;
    return (g.display_name || g.name || '').toLowerCase().includes(q);
  });
  groups.sort((a, b) => (b.file_count || 0) - (a.file_count || 0));
  if (!groups.length) {
    list.innerHTML = `<div style="padding:10px;text-align:center;color:var(--text-4);font-size:.74rem">—</div>`;
    return;
  }
  list.innerHTML = groups.map(g => {
    const sel = S.colGroupIds.has(g.id);
    const name = g.display_name || g.name || `#${g.id}`;
    return `<label class="cgf-item${sel?' cgf-selected':''}">
      <input type="checkbox" ${sel?'checked':''} onchange="cgfToggleId(${g.id}, this.checked)">
      <span class="cgf-name" title="${esc(name)}">${esc(name)}</span>
      <span class="cgf-cnt">${(g.file_count||0).toLocaleString()}</span>
    </label>`;
  }).join('');
}

function cgfToggleId(id, checked) {
  if (checked) S.colGroupIds.add(id); else S.colGroupIds.delete(id);
  cgfUpdateLabel();
  S.offset = 0;
  loadFiles();
}

function cgfSelectAll() {
  // Apply to currently filtered list (search-aware)
  const q = (document.getElementById('cgf-search')?.value || '').toLowerCase().trim();
  for (const g of (_groups || [])) {
    if (g.hidden) continue;
    if (q && !(g.display_name||g.name||'').toLowerCase().includes(q)) continue;
    S.colGroupIds.add(g.id);
  }
  cgfRenderList();
  cgfUpdateLabel();
  S.offset = 0;
  loadFiles();
}

function cgfClear() {
  S.colGroupIds.clear();
  cgfRenderList();
  cgfUpdateLabel();
  S.offset = 0;
  loadFiles();
}

function cgfUpdateLabel() {
  const el = document.getElementById('cgf-label');
  const trg = document.getElementById('cgf-trigger');
  if (!el) return;
  const n = S.colGroupIds.size;
  if (n === 0) {
    el.textContent = t('cgf.allGroups');
    if (trg) trg.classList.remove('cgf-active');
  } else if (n === 1) {
    const id = [...S.colGroupIds][0];
    const g = (_groups || []).find(x => x.id === id);
    el.textContent = g ? (g.display_name || g.name) : t('cgf.selectedCount', {n});
    if (trg) trg.classList.add('cgf-active');
  } else {
    el.textContent = t('cgf.selectedCount', {n});
    if (trg) trg.classList.add('cgf-active');
  }
}


// ── Hunter (channel discovery) ────────────────────────────────────────────────
let _hunterPollTimer = null;
let _hunterCandidates = [];
let _hunterSettings = null;

function startHunterPoll() {
  hunterReloadCandidates();
  loadHunterSettings();
  pollHunterStatus();
  _hunterPollTimer = setInterval(() => { pollHunterStatus(); hunterReloadCandidates(true); }, 1500);
  // Kullanıcının önceki "log paneli kapalı" tercihini geri yükle.
  try {
    if (localStorage.getItem('tf_hunter_log_collapsed') === '1') {
      document.getElementById('hunter-log-list')?.classList.add('collapsed');
      document.getElementById('hc-log-arrow')?.classList.remove('open');
    }
  } catch (e) {}
}

// "Kanal Avcısı nedir?" — opens the static info card as a modal.
function hunterShowWhatis() {
  document.getElementById('hunter-whatis-overlay')?.classList.add('open');
}

function hunterCloseWhatis() {
  document.getElementById('hunter-whatis-overlay')?.classList.remove('open');
}

function hunterWhatisOverlayClick(e) {
  if (e.target.id === 'hunter-whatis-overlay') hunterCloseWhatis();
}

function stopHunterPoll() {
  if (_hunterPollTimer) { clearInterval(_hunterPollTimer); _hunterPollTimer = null; }
}

async function pollHunterStatus() {
  try {
    const s = await api('/api/hunter/status');
    const el = document.getElementById('hunter-live-status');
    const monitor = document.getElementById('hunter-monitor');
    if (s.running) {
      const parts = [];
      if (s.stage) parts.push(s.stage);
      if (s.current) parts.push(`${esc(s.current)}`);
      if (s.total) parts.push(`${s.progress}/${s.total}`);
      if (el) el.textContent = `${t('hunter.running')} ${parts.join(' · ')}`;
      const btn = document.getElementById('hunter-run-btn');
      if (btn) { btn.disabled = true; btn.textContent = t('hunter.running'); }
      if (monitor) {
        monitor.style.display = '';
        renderHunterMonitor(s);
      }
    } else {
      if (el) el.textContent = '';
      const btn = document.getElementById('hunter-run-btn');
      if (btn) { btn.disabled = false; btn.textContent = t('hunter.runNow'); }
      if (monitor) monitor.style.display = 'none';
    }
  } catch (e) {}
}

function renderHunterMonitor(s) {
  // Stage breadcrumbs
  const stages = ['stage1','stage2','stage3'];
  const curIdx = stages.indexOf(s.stage);
  document.querySelectorAll('#hunter-monitor .hm-stage').forEach(el => {
    const st = el.dataset.stage;
    const idx = stages.indexOf(st);
    el.classList.remove('active','done');
    if (st === s.stage) el.classList.add('active');
    else if (curIdx >= 0 && idx < curIdx) el.classList.add('done');
  });

  // Elapsed
  const elapsedEl = document.getElementById('hm-elapsed');
  if (elapsedEl && s.started_at) {
    const elapsed = Math.max(0, (Date.now() - Date.parse(s.started_at)) / 1000);
    const mm = Math.floor(elapsed / 60), ss = Math.floor(elapsed % 60);
    elapsedEl.textContent = `${mm}:${String(ss).padStart(2,'0')}`;
  }

  // Stage detail panel
  const detailEl = document.getElementById('hm-detail');
  if (detailEl) {
    let html = '';
    if (s.stage === 'stage1') {
      const seeds = (s.seeds_found || 0).toLocaleString();
      html = `<div class="hm-current">${esc(t('hm.stage1Detail', {n: seeds}))}</div>`;
    } else if (s.stage === 'stage2') {
      const sd = s.stage_detail || {};
      const total = sd.sources_total || 0;
      const done = sd.sources_done || 0;
      const cur = sd.current_source || '—';
      const pct = total ? Math.round(done/total*100) : 0;
      const persrc = sd.per_source || {};
      const cards = Object.entries(persrc).map(([name, info]) => {
        const cls = info.state || 'queued';
        const cnt = info.found != null ? info.found : '';
        const tip = info.error ? esc(info.error) : (info.cooldown_until ? `cooldown until ${info.cooldown_until}` : name);
        return `<div class="hm-source-card ${cls}" title="${tip}"><span>${esc(name)}</span><span class="hm-src-cnt">${cnt !== '' ? cnt : (info.error ? '⚠' : '·')}</span></div>`;
      }).join('');
      const head = esc(t('hm.stage2Detail', {done, total, current: cur}));
      const subline = s.current ? `<div style="font-size:.7rem;color:var(--text-3);margin-top:3px;font-family:'Cascadia Code',monospace">${esc(s.current)}</div>` : '';
      html = `<div class="hm-current">${head}</div>${subline}
              <div class="hm-progress"><div class="hm-bar"><div class="hm-bar-fill" style="width:${pct}%"></div></div><span class="hm-progress-text">${pct}%</span></div>
              <div class="hm-sources-grid">${cards}</div>`;
    } else if (s.stage === 'stage3') {
      const total = s.total || 0;
      const done = s.progress || 0;
      const pct = total ? Math.round(done/total*100) : 0;
      html = `<div class="hm-current">${esc(t('hm.stage3Detail', {done, total, user: s.current||'-', ok: s.enriched||0, fail: s.failed||0}))}</div>
              <div class="hm-progress"><div class="hm-bar"><div class="hm-bar-fill" style="width:${pct}%"></div></div><span class="hm-progress-text">${done}/${total}</span></div>`;
    } else {
      html = `<div style="color:var(--text-3)">${esc(t('hm.preparingStage'))}</div>`;
    }
    detailEl.innerHTML = html;
  }

  // Events log
  const evtEl = document.getElementById('hm-events');
  if (evtEl) {
    const events = (s.events || []).slice(-30);  // last 30
    if (!events.length) {
      evtEl.innerHTML = `<div style="color:var(--text-4);padding:4px 0">${esc(t("hm.noEvents"))}</div>`;
    } else {
      const wasAtBottom = evtEl.scrollHeight - evtEl.scrollTop - evtEl.clientHeight < 30;
      evtEl.innerHTML = events.map(e => {
        const ts = e.ts ? e.ts.substring(0,19).replace('T',' ') : '';
        const text = _eventText(e);
        return `<div class="hm-event ${esc(e.level||'info')}">
          <span class="hm-evt-time">${esc(ts)}</span>
          <span class="hm-evt-stage">${esc(e.stage||'')}</span>
          <span class="hm-evt-msg" title="${esc(text)}">${esc(text)}</span>
        </div>`;
      }).join('');
      if (wasAtBottom) evtEl.scrollTop = evtEl.scrollHeight;
    }
  }
  // Detailed log panel below "Tarama Geçmişi" — always visible, paints from
  // the same status.events list. Keeps the user informed even after a run ends
  // (events are no longer wiped between runs).
  _renderHunterLog(s.events || []);
}

function _eventText(e) {
  // Server may send a stable i18n key + params; fall back to the English msg
  // when the key isn't present (older event rows, or _emit_event call without
  // a key= argument).
  if (e && e.key) return t(e.key, e.params || {});
  return e ? (e.msg || '') : '';
}

function _renderHunterLog(events) {
  const el = document.getElementById('hunter-log-list');
  if (!el) return;
  if (!events.length) {
    el.innerHTML = `<div class="hl-empty">${esc(t('hunter.logEmpty'))}</div>`;
    return;
  }
  // En yeni satır en üstte: array'i ters çevirip render et. Kullanıcı en
  // üstteyse (yeni gelenleri takip ediyor) scroll'u 0'da tutuyoruz; daha
  // aşağıdaysa (eski olayı okuyor) scroll pozisyonunu bozmadan içeriği
  // yeniliyoruz.
  const wasAtTop = el.scrollTop < 30;
  const prevTop = el.scrollTop;
  el.innerHTML = events.slice().reverse().map(e => {
    const ts = (e.ts || '').substring(0, 19).replace('T', ' ');
    const text = _eventText(e);
    const cls = text.startsWith('───') ? 'sep' : (e.level || 'info');
    return `<div class="hl-event ${esc(cls)}">
      <span class="hl-time">${esc(ts)}</span>
      <span class="hl-stage">${esc(e.stage || '')}</span>
      <span class="hl-msg">${esc(text)}</span>
    </div>`;
  }).join('');
  el.scrollTop = wasAtTop ? 0 : prevTop;
}

function hunterToggleLog() {
  const list  = document.getElementById('hunter-log-list');
  const arrow = document.getElementById('hc-log-arrow');
  if (!list) return;
  const collapsed = list.classList.toggle('collapsed');
  if (arrow) arrow.classList.toggle('open', !collapsed);
  // Tercihi sayfa yenilemelerinde de hatırla.
  try { localStorage.setItem('tf_hunter_log_collapsed', collapsed ? '1' : '0'); } catch (e) {}
}

async function hunterCancelRun() {
  if (!confirm(t('hm.confirmCancel'))) return;
  await api('/api/hunter/cancel', { method:'POST' });
}

async function hunterSkipStage() {
  await api('/api/hunter/skip_stage', { method:'POST' });
}

async function loadHunterSettings() {
  try {
    _hunterSettings = await api('/api/hunter/settings');
    if (!_hunterSettings) return;
    const set = (id, val) => { const el = document.getElementById(id); if (el) {
      if (el.type === 'checkbox') el.checked = !!val;
      else el.value = val == null ? '' : val;
    }};
    set('h-enabled', _hunterSettings.enabled);
    set('h-stage1',  _hunterSettings.stage1_enabled);
    set('h-stage2',  _hunterSettings.stage2_enabled);
    set('h-web-delay', _hunterSettings.web_request_delay_ms);
    set('h-web-conc',  _hunterSettings.web_concurrency);
    set('h-tg-delay',  _hunterSettings.tg_request_delay_ms);
    set('h-tg-cap',    _hunterSettings.tg_daily_lookup_cap);
    set('h-tg-sample', _hunterSettings.tg_messages_to_sample);
    set('h-tg-account',_hunterSettings.tg_account_id);
    set('h-temp-join', _hunterSettings.tg_temp_join_enabled);
    set('h-schedule-kind', _hunterSettings.schedule_kind);
    set('h-schedule-int',  _hunterSettings.schedule_interval_seconds);
    set('h-keywords',  _hunterSettings.keywords || '');
    set('h-sources',   _hunterSettings.sources || '');
    set('h-min-subs',  _hunterSettings.min_subscribers);
    set('h-languages', _hunterSettings.languages || '');
    set('h-anthropic-key', _hunterSettings.anthropic_api_key || '');
    const cap = document.getElementById('hunter-cap-info');
    if (cap) cap.textContent = t('hunter.lookupsToday', {n: _hunterSettings.lookups_used_today || 0, cap: _hunterSettings.tg_daily_lookup_cap || 500});
  } catch(e) {}
}

function hunterToggleSettings() {
  const card = document.getElementById('hunter-settings-card');
  const btn  = document.getElementById('hunter-settings-btn');
  if (card.style.display === 'none') {
    loadHunterSettings();
    card.style.display = '';
    btn && btn.classList.add('active');
  } else {
    card.style.display = 'none';
    btn && btn.classList.remove('active');
  }
}

async function hunterSaveSettings() {
  const get = id => {
    const el = document.getElementById(id);
    if (!el) return null;
    if (el.type === 'checkbox') return el.checked;
    if (el.type === 'number') { const v = el.value.trim(); return v === '' ? null : parseInt(v, 10); }
    return el.value;
  };
  const patch = {
    enabled: get('h-enabled'),
    stage1_enabled: get('h-stage1'),
    stage2_enabled: get('h-stage2'),
    web_request_delay_ms: get('h-web-delay'),
    web_concurrency: get('h-web-conc'),
    tg_request_delay_ms: get('h-tg-delay'),
    tg_daily_lookup_cap: get('h-tg-cap'),
    tg_messages_to_sample: get('h-tg-sample'),
    tg_account_id: get('h-tg-account'),
    tg_temp_join_enabled: get('h-temp-join'),
    schedule_kind: get('h-schedule-kind'),
    schedule_interval_seconds: get('h-schedule-int'),
    keywords: get('h-keywords'),
    sources: get('h-sources'),
    min_subscribers: get('h-min-subs'),
    languages: get('h-languages'),
    anthropic_api_key: get('h-anthropic-key'),
  };
  try {
    await api('/api/hunter/settings', { method: 'PUT', json: patch });
    hunterToggleSettings();
    loadHunterSettings();
  } catch (e) {
    alert(e.message);
  }
}

async function hunterRun() {
  const r = await api('/api/hunter/run', { method: 'POST' });
  pollHunterStatus();
  setTimeout(hunterReloadCandidates, 800);
}

async function hunterReloadCandidates(silent) {
  const status = document.getElementById('hunter-filter-status')?.value || '';
  const sort = document.getElementById('hunter-sort')?.value || 'score';
  const params = new URLSearchParams({
    sort,
    limit:  String(S.hunterLimit),
    offset: String(S.hunterOffset),
  });
  if (status) params.set('status', status);
  // Only paint the loading state on the explicit (user-driven) reload —
  // not for the silent 1.5s polling refresh, which would cause flicker.
  if (!silent) _paintGridLoading('hunter-grid-body', 11);
  try {
    const r = await api('/api/hunter/candidates?' + params);
    _hunterCandidates = r.candidates || [];
    S.hunterTotal = r.total || 0;
    // If user deleted/blacklisted enough rows that the current page no
    // longer exists, slide the offset back to a valid page.
    if (S.hunterOffset > 0 && S.hunterOffset >= S.hunterTotal) {
      S.hunterOffset = Math.max(0, Math.floor((S.hunterTotal - 1) / S.hunterLimit) * S.hunterLimit);
      return hunterReloadCandidates(silent);
    }
    renderHunterCandidates();
    renderHunterPager();
  } catch(e) {
    if (!silent) console.warn(e);
  }
}

function renderHunterPager() {
  const el = document.getElementById('hunter-pager');
  if (!el) return;
  const total = S.hunterTotal;
  const limit = S.hunterLimit;
  const offset = S.hunterOffset;

  if (!total) {
    // Empty grid — drop pager content. Memoize so we don't keep wiping
    // an already-empty node and stomping any unrelated focus.
    if (el._lastPagerHtml !== '') {
      el.innerHTML = '';
      el._lastPagerHtml = '';
      el.classList.remove('has-rows');
    }
    return;
  }
  el.classList.add('has-rows');

  const totalPages = Math.max(1, Math.ceil(total / limit));
  const current = Math.floor(offset / limit);
  const fromN = offset + 1;
  const toN = Math.min(offset + limit, total);

  let html = `
    <span class="hp-info">${esc(t('hg.pagerRange', { from: fromN.toLocaleString(), to: toN.toLocaleString(), total: total.toLocaleString() }))}</span>
    <span style="flex:1"></span>
    <label style="display:inline-flex;align-items:center;gap:6px">${esc(t('hg.perPage'))}
      <select onchange="hunterSetLimit(this.value)">
        <option value="20"${limit===20?' selected':''}>20</option>
        <option value="50"${limit===50?' selected':''}>50</option>
        <option value="100"${limit===100?' selected':''}>100</option>
        <option value="200"${limit===200?' selected':''}>200</option>
        <option value="500"${limit===500?' selected':''}>500</option>
        <option value="1000"${limit===1000?' selected':''}>1000</option>
      </select>
    </label>
    <button class="pg-btn" onclick="hunterGotoPage(${current-1})" ${current===0?'disabled':''}>‹</button>
  `;
  // Use the same window-style page list builder as the files grid.
  buildPageList(current, totalPages).forEach(p => {
    if (p === '…') { html += `<span class="pg-ellipsis">…</span>`; return; }
    html += `<button class="pg-btn${p===current?' active':''}" onclick="hunterGotoPage(${p})">${p+1}</button>`;
  });
  html += `<button class="pg-btn" onclick="hunterGotoPage(${current+1})" ${current>=totalPages-1?'disabled':''}>›</button>`;
  // Hunter status polling redraws this element every 1.5s. If we rewrite
  // innerHTML unconditionally, an open <select> dropdown ("sayfa başı")
  // disappears with its DOM node and the browser auto-closes the menu
  // 1-2 sec after the user clicked it. Skip the write when nothing
  // changed.
  if (el._lastPagerHtml === html) return;
  el._lastPagerHtml = html;
  el.innerHTML = html;
}

function hunterSetLimit(v) {
  const lim = parseInt(v, 10);
  if (!Number.isFinite(lim) || lim <= 0) return;
  S.hunterLimit = lim;
  S.hunterOffset = 0;
  try { localStorage.setItem('tf_hunter_limit', lim); } catch (e) {}
  hunterReloadCandidates();
}

function hunterGotoPage(pg) {
  if (!Number.isFinite(pg)) return;
  const total = S.hunterTotal;
  const limit = S.hunterLimit;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const clamped = Math.max(0, Math.min(totalPages - 1, pg));
  S.hunterOffset = clamped * limit;
  hunterReloadCandidates();
}

// Status/sort changes shrink or shuffle the result set, so any non-zero
// offset becomes meaningless — reset to page 1.
function hunterFilterChange() {
  S.hunterOffset = 0;
  hunterReloadCandidates();
}

const _HUNTER_TYPE_COLORS = {
  audio: '#7c3aed', video: '#ef4444', image: '#059669',
  archive: '#f59e0b', document: '#2563eb', software: '#374151', other: '#9ca3af',
};

function _hunterBars(breakdown) {
  if (!breakdown || typeof breakdown !== 'object') return '';
  const total = Object.values(breakdown).reduce((s, v) => s + (v || 0), 0);
  if (!total) return `<div class="h-bars" style="opacity:.4"></div>`;
  const segs = ['audio','video','image','archive','document','software','other'].map(k => {
    const v = breakdown[k] || 0;
    if (!v) return '';
    const w = (v / total * 100).toFixed(2);
    return `<div class="h-bar-seg" style="flex:${v};background:${_HUNTER_TYPE_COLORS[k]}" title="${k}: ${v}"></div>`;
  }).join('');
  return `<div class="h-bars" title="${esc(JSON.stringify(breakdown))}">${segs}</div>`;
}

let _hgSortBy = 'score';
let _hgSortDir = 'desc';

function hgSort(col) {
  if (_hgSortBy === col) {
    _hgSortDir = _hgSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _hgSortBy = col;
    _hgSortDir = (col === 'username') ? 'asc' : 'desc';
  }
  renderHunterCandidates();
}

function _hgUpdateSortArrows() {
  const map = {score:'hg-arr-score', username:'hg-arr-username', members:'hg-arr-members',
                estimated_files:'hg-arr-files', last_message_at:'hg-arr-last', discovered_at:'hg-arr-disc',
                status:'hg-arr-status', sources:'hg-arr-sources'};
  for (const [k, id] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.textContent = _hgSortBy === k ? (_hgSortDir === 'asc' ? '▲' : '▼') : '▲▼';
    el.classList.toggle('active', _hgSortBy === k);
  }
}

function hgFilterChange() { renderHunterCandidates(); }

function _stripEmojiAndFormat(s) {
  // Drop all emoji / pictographs / symbols and collapse extra whitespace
  if (!s) return '';
  return String(s)
    .replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2300}-\u{23FF}\u{FE00}-\u{FE0F}\u{200D}\u{20D0}-\u{20FF}\u{1F1E6}-\u{1F1FF}]/gu, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function _hgTypesBar(breakdown) {
  if (!breakdown || typeof breakdown !== 'object') return `<div class="hg-types"></div>`;
  const total = Object.values(breakdown).reduce((s,v) => s+(v||0), 0);
  if (!total) return `<div class="hg-types" style="opacity:.3"><div class="hg-types-seg" style="flex:1;background:var(--border-2)"></div></div>`;
  const segs = ['audio','video','image','archive','document','software','other'].map(k => {
    const v = breakdown[k] || 0;
    if (!v) return '';
    return `<div class="hg-types-seg" style="flex:${v};background:${_HUNTER_TYPE_COLORS[k]||'#9ca3af'}" title="${k}: ${v}"></div>`;
  }).join('');
  return `<div class="hg-types" title="${esc(JSON.stringify(breakdown))}">${segs}</div>`;
}

function renderHunterCandidates() {
  const tbody = document.getElementById('hunter-grid-body');
  const empty = document.getElementById('hunter-grid-empty');
  if (!tbody) return;

  // Filters
  const fU = (document.getElementById('hg-flt-username')?.value || '').toLowerCase().trim();
  const fM = parseInt(document.getElementById('hg-flt-min-members')?.value || '0', 10) || 0;
  const fF = parseInt(document.getElementById('hg-flt-min-files')?.value || '0', 10) || 0;
  const fS = (document.getElementById('hg-flt-status')?.value || '').trim();
  const fSrc = (document.getElementById('hg-flt-sources')?.value || '').toLowerCase().trim();

  let rows = _hunterCandidates.filter(c => {
    if (fU && !((c.username || '').toLowerCase().includes(fU) || (c.title||'').toLowerCase().includes(fU))) return false;
    if (fM && (c.members || 0) < fM) return false;
    if (fF && (c.estimated_files || 0) < fF) return false;
    if (fS && (c.status || '') !== fS) return false;
    if (fSrc) {
      const joined = ((c.sources||[]).join(' ')).toLowerCase();
      if (!joined.includes(fSrc)) return false;
    }
    return true;
  });

  // Sort
  const dir = _hgSortDir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    let va = a[_hgSortBy], vb = b[_hgSortBy];
    if (_hgSortBy === 'last_message_at' || _hgSortBy === 'discovered_at') {
      va = va ? Date.parse(va) : 0; vb = vb ? Date.parse(vb) : 0;
    } else if (_hgSortBy === 'username' || _hgSortBy === 'status') {
      va = (va||'').toLowerCase(); vb = (vb||'').toLowerCase();
    } else if (_hgSortBy === 'sources') {
      va = (a.sources||[]).join(',').toLowerCase();
      vb = (b.sources||[]).join(',').toLowerCase();
    } else {
      va = va || 0; vb = vb || 0;
    }
    if (va < vb) return -1*dir;
    if (va > vb) return  1*dir;
    return 0;
  });

  _hgUpdateSortArrows();

  if (!rows.length) {
    tbody.innerHTML = '';
    if (empty) { empty.style.display = ''; empty.textContent = t('hg.empty') || t('hunter.empty'); }
    return;
  }
  if (empty) empty.style.display = 'none';

  tbody.innerHTML = rows.map(c => {
    const score = c.score || 0;
    const scoreCls = score > 0 ? '' : ' zero';
    const status = c.status || 'discovered';
    const title = _stripEmojiAndFormat(c.title || c.username) || c.username;
    const members = c.members ? c.members.toLocaleString() : '—';
    // "Tam Tara" yapılmışsa deep_scan_total (gerçek toplam, ✓), aksi
    // halde estimated_files (200 mesaj örnekleminden, ~). Tooltip ile
    // hangi olduğu netleştiriliyor.
    let files;
    if (c.deep_scan_status === 'done' && (c.deep_scan_total || 0) > 0) {
      files = `${c.deep_scan_total.toLocaleString()} <span title="${esc(t('hg.deepScanDoneTitle'))}" style="color:#16a34a;font-size:.85em">✓</span>`;
    } else if (c.estimated_files != null) {
      files = `${c.estimated_files.toLocaleString()} <span title="${esc(t('hg.estimatedTitle'))}" style="color:var(--text-4);font-size:.85em">~</span>`;
    } else {
      files = '—';
    }
    const last = c.last_message_at ? fmtDate(c.last_message_at).substring(0,16) : '—';
    const disc = c.discovered_at ? fmtDate(c.discovered_at).substring(0,16) : '—';
    const sources = (c.sources || []).map(s => s.replace('internal:', '')).join(', ');
    const isEnriched = status === 'enriched' || status === 'reviewed';
    let actions;
    if (status !== 'joined' && status !== 'rejected' && status !== 'blacklisted') {
      if (c.already_joined) {
        // User is already a member of this channel via Telegram; skip the
        // Join button (which would fire JoinChannelRequest and risk FloodWait).
        actions = `<span class="hg-already-joined" title="${esc(t('hunter.alreadyJoinedTitle'))}">${esc(t('hunter.alreadyJoined'))}</span>
                   <button class="hg-btn hg-btn-reject" onclick="hunterReject(${c.id}, event)">${esc(t('hunter.reject'))}</button>`;
      } else {
        actions = `<button class="hg-btn hg-btn-join" onclick="hunterJoin(${c.id}, event)">${esc(t('hunter.join'))}</button>
                   <button class="hg-btn hg-btn-reject" onclick="hunterReject(${c.id}, event)">${esc(t('hunter.reject'))}</button>`;
      }
    } else {
      actions = `<span style="color:var(--text-4);font-size:.7rem">—</span>`;
    }
    const sel = S.hunterSelected.has(c.id);
    // Hourglass when this candidate sits in the FloodWait join queue.
    let queueBadge = '';
    if (c.queue_due_at) {
      const dueMs = Date.parse(c.queue_due_at);
      let waitTxt = '';
      if (Number.isFinite(dueMs)) {
        const diff = Math.max(0, Math.round((dueMs - Date.now()) / 1000));
        waitTxt = _fmtWait(diff);
      }
      const tip = t('hg.queueTip', { wait: waitTxt, attempts: c.queue_attempts || 1 });
      queueBadge = ` <span class="hg-queue-badge" title="${esc(tip)}">⏳ ${esc(waitTxt)}</span>`;
    }
    return `<tr class="${sel?'hg-row-selected':''}" onclick="hgRowClick(event, ${c.id})">
      <td class="hg-chk-cell"><input type="checkbox" data-hg-cid="${c.id}" ${sel?'checked':''}></td>
      <td><div class="hg-score${scoreCls}">${score.toFixed(0)}</div></td>
      <td><div class="hg-channel"><span class="hg-title" title="${esc(title)}">${esc(title)}</span> <code>@${esc(c.username)}</code>${queueBadge}</div></td>
      <td>${members}</td>
      <td>${files}</td>
      <td>${_hgTypesBar(c.file_type_breakdown)}</td>
      <td style="font-size:.72rem;color:var(--text-3)">${esc(last)}</td>
      <td style="font-size:.72rem;color:var(--text-3)">${esc(disc)}</td>
      <td><span class="hg-status s-${status}">${esc(t('hunter.status' + status.charAt(0).toUpperCase() + status.slice(1)) || status)}</span></td>
      <td><span class="hg-sources" title="${esc((c.sources||[]).join(', '))}">${esc(sources || '—')}</span></td>
      <td onclick="event.stopPropagation()"><div class="hg-act">${actions}</div></td>
    </tr>`;
  }).join('');

  // Direct per-checkbox click binding. Each render creates fresh DOM nodes
  // so listeners are added to NEW elements (old ones GC with the old DOM).
  // This avoids any delegated-listener subtlety with event.shiftKey loss
  // and makes the binding obvious in DevTools' "Event Listeners" panel.
  tbody.querySelectorAll('.hg-chk-cell input[type="checkbox"]').forEach(cb => {
    cb.addEventListener('click', _hgCbClick);
  });
}

function _hgCbClick(ev) {
  const cb = ev.currentTarget;
  const cidStr = cb.getAttribute('data-hg-cid');
  if (!cidStr) return;
  // Log so user can confirm in DevTools that handler fires + shiftKey arrives
  console.log('[hg-checkbox]', cidStr, 'checked=', cb.checked, 'shift=', ev.shiftKey);
  ev.stopPropagation();   // keep the row's modal-open handler quiet
  hgToggleSelect(parseInt(cidStr, 10), cb.checked, ev);
}

async function hunterShowDetail(cid) {
  try {
    const c = await api(`/api/hunter/candidates/${cid}`);
    const breakdown = c.file_type_breakdown || {};
    const types = Object.entries(breakdown).filter(([,v]) => v > 0)
      .map(([k,v]) => `<span class="hd-type-pill" style="border-left:3px solid ${_HUNTER_TYPE_COLORS[k]||'#9ca3af'};padding-left:8px">${esc(k)}: <b>${v}</b></span>`)
      .join('');
    const sources = (c.sources || []).join(', ');
    document.getElementById('hunter-detail-body').innerHTML = `
      <div class="hd-head">
        <h2>🎯 ${esc(c.title || c.username)} <code style="font-size:.75rem;background:var(--bg-info);color:var(--accent-h);padding:2px 8px;border-radius:5px">@${esc(c.username)}</code></h2>
        ${c.description ? `<div class="hd-desc">${esc(c.description)}</div>` : ''}
      </div>

      <div class="hd-meta">
        <div class="hd-grid">
          <b>${esc(t('hunter.score'))}:</b> <span><b style="color:var(--accent);font-size:1.05rem">${(c.score||0).toFixed(1)}</b></span>
          <b>${esc(t('hunter.members'))}:</b> <span>${c.members ? c.members.toLocaleString() : '—'}</span>
          <b>${esc(t('hunter.totalFilesSampled'))}:</b> <span>${c.file_count_sample||0} / ${c.sampled_messages||0}</span>
          <b>${esc(t('hunter.avgFileSize'))}:</b> <span>${fmtSize(c.avg_file_size||0)}</span>
          <b>${esc(t('hunter.lastMessage'))}:</b> <span>${c.last_message_at ? fmtDate(c.last_message_at) : '—'}</span>
          <b>${esc(t('hunter.discovered'))}:</b> <span>${c.discovered_at ? fmtDate(c.discovered_at) : '—'}</span>
          <b>${esc(t('hunter.sources_'))}:</b> <span>${esc(sources || '—')}</span>
          <b>${esc(t('table.status'))}:</b> <span><span class="h-status-pill s-${c.status||'discovered'}">${esc(c.status||'')}</span></span>
          ${c.error ? `<b style="color:#dc2626">⚠</b><span style="color:#dc2626">${esc(c.error)}</span>` : ''}
        </div>
        ${types ? `<div class="hd-types">${types}</div>` : ''}
      </div>

      <div class="hd-actions" id="hd-actions">
        <a href="https://t.me/${esc(c.username)}" target="_blank" rel="noopener" class="h-btn">↗ Telegram</a>
        <button class="h-btn"               data-act="deepScan"  title="${esc(t('hd.deepScan'))}">${esc(t('hd.deepScan'))}</button>
        ${c.status !== 'joined' && c.status !== 'blacklisted' ? `<button class="h-btn h-btn-join" data-act="join"      title="${esc(t('hunter.actionHelpJoin'))}">${esc(t('hunter.join'))}</button>` : ''}
        ${c.status !== 'rejected' && c.status !== 'blacklisted' ? `<button class="h-btn"          data-act="reject"    title="${esc(t('hunter.actionHelpReject'))}">${esc(t('hunter.reject'))}</button>` : ''}
        ${c.status !== 'blacklisted' ? `<button class="h-btn h-btn-reject" data-act="blacklist" title="${esc(t('hunter.actionHelpBlacklist'))}">${esc(t('hunter.blacklist'))}</button>` : ''}
        ${c.status === 'blacklisted' || c.status === 'rejected' ? `<button class="h-btn" data-act="restore" title="${esc(t('hunter.restoreBtnTitle'))}">${esc(t('hunter.restoreBtn'))}</button>` : ''}
        <button class="h-btn" style="margin-left:auto" data-act="close">${esc(t('common.close'))}</button>
      </div>

      <div class="hd-files">
        <h4>${esc(t('hd.fileList'))}</h4>
        <div id="hd-files-area"></div>
      </div>`;
    document.getElementById('hunter-detail-overlay').classList.add('open');
    _currentDetailCid = c.id;
    _currentDetailUsername = c.username;
    _hdFilesQ = ''; _hdFilesExt = ''; _hdFilesSortBy = 'date'; _hdFilesSortDir = 'desc';
    _bindHdActions();
    refreshHdFiles();
    pollDeepScan();
    // NOT auto-kicking a deep scan on open. Stage 3 enrichment now writes
    // the files it sees in its 200-msg sample to hunter_candidate_files,
    // so the lightbox is already populated for the typical case. The
    // "Tam Tara" button is the explicit gesture for fetching the rest of
    // the channel history; we don't burn FloodWait budget just because
    // the user clicked a row to look at it.
  } catch (e) {
    alert(e.message);
  }
}

// ── Detail modal: file list / deep-scan ─────────────────────────────────────
let _currentDetailCid = null;
let _currentDetailUsername = null;
let _hdFilesQ = '';
let _hdFilesExt = '';
let _hdFilesSortBy = 'date';
let _hdFilesSortDir = 'desc';
let _hdDeepPollTimer = null;
let _hdScanState = null;        // 'running' | 'done' | 'error' | 'cancelled' | null
let _hdScanProcessed = 0;       // processed-message count emitted by backend
let _hdRefreshSkip = 0;         // refresh files every other tick to keep UI snappy

async function pollDeepScan() {
  if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
  if (!_currentDetailCid) return;
  const tick = async () => {
    try {
      const s = await api(`/api/hunter/candidates/${_currentDetailCid}/deep_scan_status`);
      _hdScanState = s.state || null;
      _hdScanProcessed = s.processed || 0;
      const stateEl = document.getElementById('hd-deep-state');
      if (stateEl) {
        if (s.state === 'running') {
          stateEl.innerHTML = `<span class="hd-files-progress"><span class="hd-spinner"></span>
            <span>${esc(t('hd.deepScanRunning', {n: s.processed.toLocaleString()}))}</span>
            <button class="h-btn" onclick="cancelDeepScan()">${esc(t('common.cancel'))}</button>
          </span>`;
        } else if (s.state === 'done') {
          stateEl.innerHTML = `<span style="color:#16a34a;font-weight:600">✓ ${esc(t('hd.deepScanDone'))}</span>`;
          if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
          refreshHdFiles();
        } else if (s.state === 'error') {
          stateEl.innerHTML = `<span style="color:#dc2626">⚠ ${esc(t('hd.deepScanError'))}: ${esc(s.error || '')}</span>`;
          if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
          refreshHdFiles();
        } else if (s.state === 'cancelled') {
          stateEl.innerHTML = `<span style="color:var(--text-3)">${esc(t('common.cancel'))}…</span>`;
          if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
          refreshHdFiles();
        } else {
          stateEl.innerHTML = '';
        }
      }
      // Refresh the file list every other running tick (~4 s) so the user
      // sees the count climb instead of staring at a static "0 files" slate.
      if (s.state === 'running') {
        _hdRefreshSkip = (_hdRefreshSkip + 1) % 2;
        if (_hdRefreshSkip === 0) refreshHdFiles();
      }
    } catch(e) {}
  };
  await tick();
  _hdDeepPollTimer = setInterval(tick, 2000);
}

async function hunterDeepScan(cid) {
  await api(`/api/hunter/candidates/${cid}/deep_scan`, { method: 'POST' });
  pollDeepScan();
}

async function _autoStartDeepScan(cid) {
  // Skip when a scan is already running or already completed; in those
  // cases pollDeepScan + refreshHdFiles already render the right thing.
  let s;
  try { s = await api(`/api/hunter/candidates/${cid}/deep_scan_status`); }
  catch (e) { return; }
  if (s.state === 'running' || s.state === 'done') return;
  try { await api(`/api/hunter/candidates/${cid}/deep_scan`, { method: 'POST' }); }
  catch (e) { /* surface only via the modal's progress line */ }
  pollDeepScan();
}

async function cancelDeepScan() {
  if (!_currentDetailCid) return;
  await api(`/api/hunter/candidates/${_currentDetailCid}/deep_scan/cancel`, { method: 'POST' });
}

function _hdSetSort(col) {
  if (_hdFilesSortBy === col) _hdFilesSortDir = _hdFilesSortDir === 'asc' ? 'desc' : 'asc';
  else { _hdFilesSortBy = col; _hdFilesSortDir = (col === 'name' || col === 'ext') ? 'asc' : 'desc'; }
  refreshHdFiles();
}

async function refreshHdFiles() {
  if (!_currentDetailCid) return;
  const area = document.getElementById('hd-files-area');
  if (!area) return;
  const params = new URLSearchParams({
    sort_by: _hdFilesSortBy, sort_dir: _hdFilesSortDir, limit: '500'
  });
  if (_hdFilesQ) params.set('q', _hdFilesQ);
  if (_hdFilesExt) params.set('ext', _hdFilesExt);
  let data;
  try { data = await api(`/api/hunter/candidates/${_currentDetailCid}/files?${params}`); }
  catch(e) { area.innerHTML = `<div style="color:#dc2626">${esc(e.message)}</div>`; return; }
  const summary = data.summary || {};
  const total = summary.total || 0;
  const totalSize = summary.total_size || 0;

  const arr = (col) => _hdFilesSortBy === col ? (_hdFilesSortDir==='asc' ? '▲' : '▼') : '▲▼';

  // Adlı vs doğal-medya kırılımı: kullanıcı kanalı üye-olmadan-önce
  // değerlendirirken bunu görmek istiyor. Çoğunluğu sesli mesaj/kamera
  // videosu olan bir kanal genelde sohbet grubu; çoğunluğu adlı dosya
  // olan bir kanal asıl dosya paylaşım kanalı.
  const named     = +summary.named_count     || 0;
  const ephemeral = +summary.ephemeral_count || 0;
  const namedSz   = +summary.named_size      || 0;
  const ephemSz   = +summary.ephemeral_size  || 0;
  const namedPct  = total > 0 ? Math.round((named / total) * 100) : 0;
  const ephemPct  = total > 0 ? (100 - namedPct) : 0;
  const breakdownLine = total > 0
    ? `<span class="hd-kind-bar" title="${esc(t('hd.kindBarTitle'))}">
         <span class="hd-kind hd-kind-named"   title="${esc(t('hd.kindNamedTitle'))}">📄 ${named.toLocaleString()} ${esc(t('hd.kindNamedLabel'))} (${namedPct}%) · ${fmtSize(namedSz)}</span>
         <span class="hd-kind hd-kind-ephem" title="${esc(t('hd.kindEphemTitle'))}">🎤 ${ephemeral.toLocaleString()} ${esc(t('hd.kindEphemLabel'))} (${ephemPct}%) · ${fmtSize(ephemSz)}</span>
       </span>`
    : '';
  let body = `<div class="hd-files-bar">
    <span class="hd-files-meta">${esc(t('hd.totalFiles', {n: total.toLocaleString(), size: fmtSize(totalSize)}))}</span>
    ${breakdownLine}
    <span class="hd-files-meta" id="hd-deep-state"></span>
  </div>`;

  if (!total) {
    if (_hdScanState === 'running') {
      // Centered loading block — replaces the empty placeholder while the
      // backend is still walking the channel's history.
      body += `<div class="hd-loading-big">
        <span class="hd-spinner"></span>
        <span>${esc(t('hd.deepScanLoading'))}</span>
        <small>${esc(t('hd.deepScanLoadingHint', {n: _hdScanProcessed.toLocaleString()}))}</small>
      </div>`;
    } else {
      body += `<div style="text-align:center;padding:20px;color:var(--text-4);font-size:.78rem">${esc(t('hd.noFiles'))}</div>`;
    }
  } else {
    // Seed the per-row download state from f.local_path so reopening the
    // lightbox after a successful download immediately renders 💾/🗑 instead
    // of a stale 📥.
    data.files.forEach(f => {
      if (f.local_path && !_hdDlStatus[f.message_id]) {
        _hdDlStatus[f.message_id] = { state: 'done', progress: 1.0, local_path: f.local_path };
      }
    });
    body += `<ul class="hd-file-list" id="hd-file-ul">${data.files.map(f => _renderHdFileRow(f)).join('')}</ul>`;
  }

  area.innerHTML = body;
  _resumeActiveFileDownloads();
}

// Renders a single <li> for one candidate file. State for an in-flight
// download (if any) lives in _hdDlStatus[msg_id]; persisted "already
// downloaded" state lives in f.local_path.
function _renderHdFileRow(f) {
  const msgId = f.message_id;
  const dl    = _hdDlStatus[msgId];
  const dlState = dl ? dl.state : (f.local_path ? 'done' : 'idle');

  let actionsHtml = '';
  let liExtraClass = '';
  if (dlState === 'downloading') {
    liExtraClass = ' hf-downloading';
    const pct = dl && dl.progress != null ? Math.round(dl.progress * 100) : 0;
    actionsHtml = `<span class="hf-progress">${pct}%</span>
      <button class="hf-btn hf-btn-del" data-act="cancel" data-msg="${msgId}" title="${esc(t('hf.cancel'))}">✕</button>`;
  } else if (dlState === 'done') {
    actionsHtml = `<button class="hf-btn" data-act="open" data-msg="${msgId}" title="${esc(t('hf.open'))}">💾</button>
      <button class="hf-btn hf-btn-del" data-act="delete" data-msg="${msgId}" title="${esc(t('hf.delete'))}">🗑</button>`;
  } else if (dlState === 'error') {
    liExtraClass = ' hf-error';
    actionsHtml = `<button class="hf-btn" data-act="download" data-msg="${msgId}" title="${esc(t('hf.retry'))}">↻</button>`;
  } else if (dlState === 'needs_temp_join') {
    actionsHtml = `<button class="hf-btn" data-act="downloadJoin" data-msg="${msgId}" title="${esc(t('hf.needsJoin'))}">🔒</button>`;
  } else {
    actionsHtml = `<button class="hf-btn" data-act="download" data-msg="${msgId}" title="${esc(t('hf.download'))}">📥</button>`;
  }
  const kindBadge = (f.is_named === false)
    ? `<span class="hf-kind hf-kind-ephem" title="${esc(t('hf.kindEphemTitle'))}">🎤</span>`
    : `<span class="hf-kind hf-kind-named" title="${esc(t('hf.kindNamedTitle'))}">📄</span>`;
  return `<li class="hf-row${liExtraClass}${f.is_named === false ? ' hf-ephem' : ''}" data-msg="${msgId}">
    ${kindBadge}
    <span class="hf-name" title="${esc(f.file_name||'')}">${esc(f.file_name || '—')}</span>
    <span class="hf-size">${fmtSize(f.file_size||0)}</span>
    <span class="hf-date">${f.date ? fmtDate(f.date).substring(0,16) : '—'}</span>
    <span class="hf-actions">${actionsHtml}</span>
  </li>`;
}

// ── Per-file download state (candidate detail) ───────────────────────────────
let _hdDlStatus = {};
let _hdDlPollers = {};

function _resumeActiveFileDownloads() {
  Object.keys(_hdDlStatus).forEach(msgId => {
    if (_hdDlStatus[msgId] && _hdDlStatus[msgId].state === 'downloading') {
      _startFileDlPoller(parseInt(msgId, 10));
    }
  });
  const ul = document.getElementById('hd-file-ul');
  if (ul && !ul._wired) {
    ul.addEventListener('click', _onHdFileAction);
    ul._wired = true;
  }
}

async function _onHdFileAction(ev) {
  const btn = ev.target.closest('[data-act]');
  if (!btn) return;
  const act   = btn.dataset.act;
  const msgId = parseInt(btn.dataset.msg, 10);
  if (!Number.isFinite(msgId) || _currentDetailCid == null) return;
  switch (act) {
    case 'download':     hfStartDownload(msgId, false); break;
    case 'downloadJoin': hfDownloadWithTempJoin(msgId); break;
    case 'cancel':       hfCancelDownload(msgId); break;
    case 'open':         hfOpenDownloaded(msgId); break;
    case 'delete':       hfDeleteDownloaded(msgId); break;
  }
}

async function hfStartDownload(msgId, withTempJoin) {
  const cid = _currentDetailCid;
  try {
    const qs = withTempJoin ? '?confirm_temp_join=1' : '';
    const res = await api(`/api/hunter/candidates/${cid}/files/${msgId}/download${qs}`, { method: 'POST' });
    _hdDlStatus[msgId] = res;
    _refreshHdFileRow(msgId);
    if (res.state === 'downloading') {
      _startFileDlPoller(msgId);
    } else if (res.state === 'needs_temp_join') {
      _showTempJoinConfirm(msgId, res.username || '');
    }
  } catch (e) {
    _hdDlStatus[msgId] = { state: 'error', error: e.message || String(e) };
    _refreshHdFileRow(msgId);
    showToast(`✗ ${esc(e.message || e)}`, 4000);
  }
}

function _startFileDlPoller(msgId) {
  if (_hdDlPollers[msgId]) return;
  const cid = _currentDetailCid;
  _hdDlPollers[msgId] = setInterval(async () => {
    if (_currentDetailCid !== cid) { _stopFileDlPoller(msgId); return; }
    try {
      const s = await api(`/api/hunter/candidates/${cid}/files/${msgId}/status`);
      _hdDlStatus[msgId] = s;
      _refreshHdFileRow(msgId);
      if (s.state !== 'downloading') {
        _stopFileDlPoller(msgId);
        if (s.state === 'done') showToast(`✓ ${esc(t('hf.downloadDone'))}`, 1800);
        if (s.state === 'error') showToast(`✗ ${esc(s.error || '')}`, 4500);
      }
    } catch(e) { _stopFileDlPoller(msgId); }
  }, 1200);
}

function _stopFileDlPoller(msgId) {
  if (_hdDlPollers[msgId]) {
    clearInterval(_hdDlPollers[msgId]);
    delete _hdDlPollers[msgId];
  }
}

function _refreshHdFileRow(msgId) {
  const li = document.querySelector(`#hd-file-ul li[data-msg="${msgId}"]`);
  if (!li) return;
  const actions = li.querySelector('.hf-actions');
  if (!actions) return;
  const dl = _hdDlStatus[msgId];
  const dlState = dl ? dl.state : 'idle';
  li.classList.remove('hf-downloading', 'hf-error');
  if (dlState === 'downloading') {
    li.classList.add('hf-downloading');
    const pct = dl.progress != null ? Math.round(dl.progress * 100) : 0;
    actions.innerHTML = `<span class="hf-progress">${pct}%</span>
      <button class="hf-btn hf-btn-del" data-act="cancel" data-msg="${msgId}" title="${esc(t('hf.cancel'))}">✕</button>`;
  } else if (dlState === 'done') {
    actions.innerHTML = `<button class="hf-btn" data-act="open" data-msg="${msgId}" title="${esc(t('hf.open'))}">💾</button>
      <button class="hf-btn hf-btn-del" data-act="delete" data-msg="${msgId}" title="${esc(t('hf.delete'))}">🗑</button>`;
  } else if (dlState === 'error') {
    li.classList.add('hf-error');
    actions.innerHTML = `<button class="hf-btn" data-act="download" data-msg="${msgId}" title="${esc(t('hf.retry'))}">↻</button>`;
  } else if (dlState === 'needs_temp_join') {
    actions.innerHTML = `<button class="hf-btn" data-act="downloadJoin" data-msg="${msgId}" title="${esc(t('hf.needsJoin'))}">🔒</button>`;
  } else {
    actions.innerHTML = `<button class="hf-btn" data-act="download" data-msg="${msgId}" title="${esc(t('hf.download'))}">📥</button>`;
  }
}

function _showTempJoinConfirm(msgId, username) {
  const msg = t('hf.tempJoinConfirm', {u: username || _currentDetailUsername || ''});
  if (confirm(msg)) {
    hfStartDownload(msgId, true);
  } else {
    _hdDlStatus[msgId] = { state: 'idle' };
    _refreshHdFileRow(msgId);
  }
}

function hfDownloadWithTempJoin(msgId) {
  _showTempJoinConfirm(msgId, _currentDetailUsername || '');
}

async function hfCancelDownload(msgId) {
  const cid = _currentDetailCid;
  try { await api(`/api/hunter/candidates/${cid}/files/${msgId}/download/cancel`, { method: 'POST' }); }
  catch(e) {}
  _stopFileDlPoller(msgId);
  _hdDlStatus[msgId] = { state: 'idle' };
  _refreshHdFileRow(msgId);
}

function hfOpenDownloaded(msgId) {
  const cid = _currentDetailCid;
  const a = document.createElement('a');
  a.href = `/api/hunter/candidates/${cid}/files/${msgId}/blob`;
  a.rel = 'noopener';
  document.body.appendChild(a); a.click(); a.remove();
}

async function hfDeleteDownloaded(msgId) {
  if (!confirm(t('hf.deleteConfirm'))) return;
  const cid = _currentDetailCid;
  try { await api(`/api/hunter/candidates/${cid}/files/${msgId}/blob`, { method: 'DELETE' }); }
  catch(e) { showToast(`✗ ${esc(e.message || e)}`, 4000); return; }
  _hdDlStatus[msgId] = { state: 'idle' };
  _refreshHdFileRow(msgId);
}

function closeHunterDetail() {
  document.getElementById('hunter-detail-overlay').classList.remove('open');
  if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
  // Drop any per-file download pollers so they don't keep ticking against a
  // closed lightbox.
  Object.keys(_hdDlPollers).forEach(k => _stopFileDlPoller(parseInt(k, 10)));
  _hdDlStatus = {};
  _currentDetailCid = null;
  _hdScanState = null;
  _hdScanProcessed = 0;
}

// Wire the action bar via event delegation. Replaces the old inline-onclick
// approach: clicks land on a single listener no matter how the inner HTML
// was assembled. Logs to console so any future "the button does nothing"
// complaint shows up as missing/visible click events instead of dead silence.
function _bindHdActions() {
  const bar = document.getElementById('hd-actions');
  if (!bar) return;
  bar.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (!btn || !bar.contains(btn)) return;
    const act = btn.dataset.act;
    const cid = _currentDetailCid;
    console.log('[hunter-detail] action', act, 'cid', cid);
    if (cid == null && act !== 'close') return;
    switch (act) {
      case 'deepScan':  hunterDeepScan(cid); break;
      case 'join':      hunterJoin(cid); break;
      case 'reject':    hunterReject(cid); break;
      case 'blacklist': hunterBlacklist(cid); break;
      case 'restore':   hunterRestore(cid); break;
      case 'close':     closeHunterDetail(); break;
    }
  });
}

function hunterDetailOverlayClick(e) {
  if (e.target.id === 'hunter-detail-overlay') closeHunterDetail();
}

async function hunterJoin(cid, ev) {
  if (ev) ev.stopPropagation();
  const c = _hunterCandidates.find(x => x.id === cid) || await api(`/api/hunter/candidates/${cid}`);
  let r;
  try { r = await api(`/api/hunter/candidates/${cid}/join`, { method: 'POST' }); }
  catch (e) { showToast(t('hunter.joinFail') + ' ' + esc(e.message), 4500); return; }
  if (!r.ok) { showToast(t('hunter.joinFail') + ' ' + esc(r.error || ''), 4500); return; }
  closeHunterDetail();
  hunterReloadCandidates();
  if (r.queued) {
    const wait = _fmtWait(r.wait_s);
    showToast(t('hunter.joinQueuedMsg', { username: esc(c.username), wait }), 4500);
  } else {
    loadGroups();
    showToast(t('hunter.joinOkMsg', { username: esc(c.username) }), 3000);
  }
}

function _fmtWait(s) {
  s = Math.max(0, parseInt(s, 10) || 0);
  if (s < 60)   return t('fmt.seconds',    { n: s });
  if (s < 3600) return t('fmt.minutes',    { n: Math.round(s/60) });
  return               t('fmt.hoursShort', { n: Math.round(s/3600) });
}

async function hunterReject(cid, ev) {
  if (ev) ev.stopPropagation();
  const c = _hunterCandidates.find(x => x.id === cid) || await api(`/api/hunter/candidates/${cid}`);
  try { await api(`/api/hunter/candidates/${cid}/reject`, { method: 'POST' }); }
  catch (e) { showToast(t('hunter.rejectFail') + ' ' + esc(e.message), 4500); return; }
  closeHunterDetail();
  hunterReloadCandidates();
  showToast(t('hunter.rejectOkMsg', { username: esc(c.username) }), 3000);
}

async function hunterBlacklist(cid, ev) {
  if (ev) ev.stopPropagation();
  const c = _hunterCandidates.find(x => x.id === cid) || await api(`/api/hunter/candidates/${cid}`);
  try { await api(`/api/hunter/candidates/${cid}/blacklist`, { method: 'POST' }); }
  catch (e) { showToast(t('hunter.blacklistFail') + ' ' + esc(e.message), 4500); return; }
  closeHunterDetail();
  hunterReloadCandidates();
  showToast(t('hunter.blacklistOkMsg', { username: esc(c.username) }), 3000);
}

async function hunterRestore(cid) {
  const c = _hunterCandidates.find(x => x.id === cid) || await api(`/api/hunter/candidates/${cid}`);
  let r;
  try { r = await api(`/api/hunter/candidates/${cid}/restore`, { method: 'POST' }); }
  catch (e) { showToast(t('hunter.undoFail') + ' ' + esc(e.message), 4500); return; }
  if (!r.ok) { showToast(t('hunter.undoFail') + ' ' + esc(r.error || ''), 4500); return; }
  closeHunterDetail();
  hunterReloadCandidates();
  showToast(t('hunter.undoOkMsg', { username: esc(c.username) }), 3000);
}

// ── Hunter: clear list ──────────────────────────────────────────────────────
async function hunterClearList() {
  if (!confirm(t('hunter.confirmClear'))) return;
  try {
    await api('/api/hunter/candidates', { method: 'DELETE' });
    await hunterReloadCandidates();
  } catch (e) {
    alert(e.message);
  }
}


// ── Hunter grid: multi-select + bulk actions ────────────────────────────────

function hgRowClick(e, cid) {
  // If user is dragging or selecting text, don't trigger; if click hits a button/input, ignore
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON' || e.target.closest('button,input,a')) return;
  hunterShowDetail(cid);
}

let _lastHunterToggleId = null;

// Walk the visible checkbox elements in display order; we need this to
// resolve the shift-click range against the user's current sort/filter view.
function _hgVisibleRowIds() {
  return [...document.querySelectorAll('#hunter-grid tbody .hg-chk-cell input[type="checkbox"]')]
    .map(cb => {
      const v = cb.getAttribute('data-hg-cid');
      return v ? parseInt(v, 10) : null;
    })
    .filter(x => x != null);
}

function hgToggleSelect(id, checked, e) {
  // Shift+click extends the selection to every row between the last
  // single-clicked checkbox and this one, copying this click's checked
  // state across the range. Defaults to single toggle otherwise.
  if (e && e.shiftKey && _lastHunterToggleId != null && _lastHunterToggleId !== id) {
    const ids = _hgVisibleRowIds();
    const a = ids.indexOf(_lastHunterToggleId);
    const b = ids.indexOf(id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      const sel = window.getSelection && window.getSelection();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
      const boxes = [...document.querySelectorAll('#hunter-grid tbody .hg-chk-cell input[type="checkbox"]')];
      for (let i = lo; i <= hi; i++) {
        const rid = ids[i];
        if (checked) S.hunterSelected.add(rid);
        else        S.hunterSelected.delete(rid);
        const cb = boxes[i];
        if (cb) {
          cb.checked = checked;
          cb.closest('tr')?.classList.toggle('hg-row-selected', checked);
        }
      }
      hgUpdateBulkBar();
      _lastHunterToggleId = id;
      return;
    }
  }
  if (checked) S.hunterSelected.add(id);
  else S.hunterSelected.delete(id);
  hgUpdateBulkBar();
  // update row highlight without full re-render
  const row = document.querySelector(`#hunter-grid tbody tr td input[data-hg-cid="${id}"]`)?.closest('tr');
  if (row) row.classList.toggle('hg-row-selected', checked);
  _lastHunterToggleId = id;
}

function hgSelectAll(checked) {
  // Use the current visible rows (post filter+sort) — derive from displayed checkboxes
  const boxes = document.querySelectorAll('#hunter-grid tbody .hg-chk-cell input[type="checkbox"]');
  S.hunterSelected.clear();
  boxes.forEach(cb => {
    cb.checked = checked;
    if (checked) {
      const v = cb.getAttribute('data-hg-cid');
      if (v) S.hunterSelected.add(parseInt(v, 10));
    }
    cb.closest('tr')?.classList.toggle('hg-row-selected', checked);
  });
  hgUpdateBulkBar();
}

function hgClearSelection() {
  S.hunterSelected.clear();
  document.querySelectorAll('#hunter-grid tbody .hg-chk-cell input[type="checkbox"]').forEach(cb => {
    cb.checked = false;
    cb.closest('tr')?.classList.remove('hg-row-selected');
  });
  const sa = document.getElementById('hg-select-all'); if (sa) sa.checked = false;
  hgUpdateBulkBar();
}

function hgUpdateBulkBar() {
  const bar = document.getElementById('hunter-bulk-bar');
  const cnt = document.getElementById('hunter-bulk-count');
  const wrap = document.getElementById('hunter-grid-wrap');
  if (!bar || !cnt) return;
  const n = S.hunterSelected.size;
  if (n === 0) {
    bar.style.display = 'none';
    if (wrap) wrap.style.removeProperty('--hg-bar-h');
    return;
  }
  bar.style.display = 'flex';
  cnt.textContent = t('hbb.selected', {n});
  // Push the grid header down so the (sticky) thead sits below the (sticky) bar
  // instead of behind it.
  if (wrap) {
    requestAnimationFrame(() => {
      const h = bar.offsetHeight;
      if (h) wrap.style.setProperty('--hg-bar-h', h + 'px');
    });
  }
}

async function _hgBulkLoop(action, ids, progressMsg, delayMs = 0) {
  const bar = document.getElementById('hunter-bulk-bar');
  let done = 0;
  const total = ids.length;
  const updateProgress = () => {
    const cnt = document.getElementById('hunter-bulk-count');
    if (cnt) cnt.textContent = t('hbb.bulkProgress', {done, total});
  };
  for (const id of ids) {
    try { await action(id); }
    catch (e) { console.warn('bulk action error for id', id, e); }
    done++;
    updateProgress();
    if (delayMs > 0 && done < total) await new Promise(r => setTimeout(r, delayMs));
  }
}

async function hgBulkJoin() {
  const ids = [...S.hunterSelected];
  if (!ids.length) return;
  showToast(t('hunter.bulkJoinStart', { n: ids.length }), 2500);
  let joined = 0, queued = 0;
  await _hgBulkLoop(async (id) => {
    const r = await api(`/api/hunter/candidates/${id}/join`, { method: 'POST' });
    if (r && r.queued) queued++;
    else if (r && r.ok) joined++;
  }, ids, null, 1500);
  hgClearSelection();
  await hunterReloadCandidates();
  loadGroups();
  // Build a single summary toast that distinguishes immediate joins from
  // FloodWait-queued ones, so the user knows which need patience.
  const parts = [];
  if (joined) parts.push(t('hunter.bulkJoinOk', { joined }));
  if (queued) parts.push(t('hunter.bulkJoinQueued', { queued }));
  if (!parts.length) parts.push(t('hunter.bulkJoinDone', { n: ids.length }));
  showToast(parts.join(' · '), 4000);
}

async function hgBulkReject() {
  const ids = [...S.hunterSelected];
  if (!ids.length) return;
  await _hgBulkLoop(async (id) => {
    await api(`/api/hunter/candidates/${id}/reject`, { method: 'POST' });
  }, ids);
  hgClearSelection();
  await hunterReloadCandidates();
  showToast(t('hunter.bulkRejectOk', { n: ids.length }), 3000);
}

async function hgBulkBlacklist() {
  const ids = [...S.hunterSelected];
  if (!ids.length) return;
  await _hgBulkLoop(async (id) => {
    await api(`/api/hunter/candidates/${id}/blacklist`, { method: 'POST' });
  }, ids);
  hgClearSelection();
  await hunterReloadCandidates();
  showToast(t('hunter.bulkBlacklistOk', { n: ids.length }), 3000);
}

async function hgBulkDeepScan() {
  const ids = [...S.hunterSelected];
  if (!ids.length) return;
  showToast(t('hunter.bulkDeepScanStart', { n: ids.length }), 2500);
  // Trigger them; backend serializes via _deep_scan_tasks dict, so we just kick all
  await _hgBulkLoop(async (id) => {
    await api(`/api/hunter/candidates/${id}/deep_scan`, { method: 'POST' });
  }, ids, null, 800);
  hgClearSelection();
  // Reload to surface progress on the rows
  await hunterReloadCandidates();
}


// ── Hunter help modal ────────────────────────────────────────────────────────
function hunterShowHelp() {
  const body = document.getElementById('hunter-help-body');
  if (!body) return;
  body.innerHTML = `
    <div class="hh-head">
      <h2>${esc(t('hm.helpTitle'))}</h2>
      <button class="hh-close" onclick="hunterCloseHelp()">${esc(t('common.close'))}</button>
    </div>
    <div class="hh-body">${t('hm.helpBody')}</div>`;
  document.getElementById('hunter-help-overlay').classList.add('open');
}

function hunterCloseHelp() {
  document.getElementById('hunter-help-overlay').classList.remove('open');
}

function hunterHelpOverlayClick(e) {
  if (e.target.id === 'hunter-help-overlay') hunterCloseHelp();
}


// ── Transfer Destinations ────────────────────────────────────────────────────
let _tdEditId = null;
let _tdOpenId = null; // null=closed, number=existing item open, 'new'=add form open

function _typeLabelShort(type) {
  return { local: 'Yerel', ftp: 'FTP', sftp: 'SFTP' }[type] || type;
}

function _destPathLabel(d) {
  const cfg = d.config || {};
  if (d.type === 'local') return cfg.path || '';
  return `${cfg.host || ''}:${cfg.port || ''} → ${cfg.path || '/'}`;
}

async function loadTransferDestinations() {
  _closeTdItem();
  try {
    const dests = await api('/api/transfer-destinations');
    renderTransferDestinations(dests || []);
  } catch (e) { console.error('Transfer hedefleri yüklenemedi:', e); }
}

function renderTransferDestinations(dests) {
  const list = document.getElementById('td-list');
  if (!list) return;
  if (!dests.length) {
    list.innerHTML = `<div style="font-size:.78rem;color:var(--text-4);padding:8px 0">${esc(t('td.noDestinations'))}</div>`;
    return;
  }
  list.innerHTML = dests.map(d => `
    <div class="td-item ${d.enabled ? '' : 'td-disabled'}" id="td-item-${d.id}">
      <div class="td-item-head" onclick="toggleTdItem(${d.id})">
        <span class="td-chevron" id="td-chevron-${d.id}">›</span>
        <span class="td-badge ${d.type}">${_typeLabelShort(d.type)}</span>
        <span class="td-name">${esc(d.name)}</span>
        <span class="td-path">${esc(_destPathLabel(d))}</span>
        <div class="td-actions" onclick="event.stopPropagation()">
          <button class="td-btn td-btn-test" id="td-test-${d.id}" onclick="testTransferDest(${d.id})">Test</button>
          <button class="td-btn td-btn-danger" onclick="deleteTransferDest(${d.id})">${esc(t('common.delete'))}</button>
        </div>
      </div>
      <div class="td-item-body" id="td-body-${d.id}" style="display:none"></div>
    </div>`).join('');
}

function toggleTdItem(id) {
  if (_tdOpenId === id) { _closeTdItem(); return; }
  _openTdItem(id);
}

async function _openTdItem(id) {
  _closeTdItem();
  const dests = await api('/api/transfer-destinations');
  const d = (dests || []).find(x => x.id === id);
  if (!d) return;
  _tdEditId = id;
  _tdOpenId = id;
  resetTdForm();
  document.getElementById('td-form-title').textContent = t('td.editTitle');
  document.getElementById('td-name').value = d.name || '';
  document.getElementById('td-type').value = d.type || 'local';
  document.getElementById('td-enabled').checked = !!d.enabled;
  const cfg = d.config || {};
  if (d.type === 'local') {
    document.getElementById('td-local-path').value = cfg.path || '';
    document.getElementById('td-local-mode').value = cfg.mode || 'copy';
  } else {
    document.getElementById('td-host').value = cfg.host || '';
    document.getElementById('td-port').value = cfg.port || (d.type === 'sftp' ? 22 : 21);
    document.getElementById('td-user').value = cfg.username || '';
    document.getElementById('td-pass').value = cfg.password || '';
    document.getElementById('td-remote-path').value = cfg.path || '/';
    document.getElementById('td-passive').value = cfg.passive !== false ? 'true' : 'false';
    document.getElementById('td-remote-mode').value = cfg.mode || 'copy';
  }
  onTdTypeChange();
  const body = document.getElementById(`td-body-${id}`);
  const form = document.getElementById('td-add-form');
  if (body && form) {
    body.style.display = 'block';
    body.appendChild(form);
    document.getElementById('td-name').focus();
  }
  const chevron = document.getElementById(`td-chevron-${id}`);
  if (chevron) chevron.classList.add('open');
}

function _closeTdItem() {
  if (_tdOpenId === null) return;
  const prevId = _tdOpenId;
  _tdOpenId = null;
  _tdEditId = null;
  const form = document.getElementById('td-add-form');
  const container = document.getElementById('td-form-container');
  if (form && container) container.appendChild(form);
  if (prevId === 'new') {
    const newItem = document.getElementById('td-item-new');
    if (newItem) newItem.remove();
  } else {
    const body = document.getElementById(`td-body-${prevId}`);
    if (body) body.style.display = 'none';
    const chevron = document.getElementById(`td-chevron-${prevId}`);
    if (chevron) chevron.classList.remove('open');
  }
  resetTdForm();
}

function openAddTransferDest() {
  _closeTdItem();
  _tdEditId = null;
  _tdOpenId = 'new';
  resetTdForm();
  document.getElementById('td-form-title').textContent = t('td.newTitle');
  const list = document.getElementById('td-list');
  const newItem = document.createElement('div');
  newItem.className = 'td-item';
  newItem.id = 'td-item-new';
  newItem.innerHTML = `
    <div class="td-item-head" style="cursor:default;background:var(--bg-info)">
      <span style="font-size:.8rem;font-weight:700;color:var(--accent)">${esc(t('td.newItem'))}</span>
    </div>
    <div class="td-item-body" id="td-body-new" style="display:block"></div>`;
  list.appendChild(newItem);
  const bodyEl = document.getElementById('td-body-new');
  const form = document.getElementById('td-add-form');
  if (bodyEl && form) {
    bodyEl.appendChild(form);
    document.getElementById('td-name').focus();
  }
}

function closeAddTransferDest() {
  _closeTdItem();
}

function resetTdForm() {
  document.getElementById('td-name').value = '';
  document.getElementById('td-type').value = 'local';
  document.getElementById('td-local-path').value = '';
  document.getElementById('td-local-mode').value = 'copy';
  document.getElementById('td-host').value = '';
  document.getElementById('td-port').value = '';
  document.getElementById('td-user').value = '';
  document.getElementById('td-pass').value = '';
  document.getElementById('td-remote-path').value = '/';
  document.getElementById('td-passive').value = 'true';
  document.getElementById('td-remote-mode').value = 'copy';
  document.getElementById('td-enabled').checked = true;
  const res = document.getElementById('td-test-result');
  if (res) res.style.display = 'none';
  onTdTypeChange();
}

function onTdTypeChange() {
  const type = document.getElementById('td-type').value;
  document.getElementById('td-fields-local').style.display = type === 'local' ? '' : 'none';
  document.getElementById('td-fields-remote').style.display = type !== 'local' ? 'flex' : 'none';
  const passiveRow = document.getElementById('td-passive-row');
  if (passiveRow) passiveRow.style.display = type === 'ftp' ? '' : 'none';
  // Default port
  const portEl = document.getElementById('td-port');
  if (!portEl.value) portEl.value = type === 'sftp' ? '22' : '21';
}


function _buildTdBody() {
  const type = document.getElementById('td-type').value;
  const name = document.getElementById('td-name').value.trim();
  if (!name) { showToast(t('td.errNameRequired')); return null; }
  let config = {};
  if (type === 'local') {
    const path = document.getElementById('td-local-path').value.trim();
    if (!path) { showToast(t('td.errPathRequired')); return null; }
    config = { path, mode: document.getElementById('td-local-mode').value };
  } else {
    const host = document.getElementById('td-host').value.trim();
    if (!host) { showToast(t('td.errHostRequired')); return null; }
    config = {
      host,
      port: parseInt(document.getElementById('td-port').value) || (type === 'sftp' ? 22 : 21),
      username: document.getElementById('td-user').value.trim(),
      password: document.getElementById('td-pass').value,
      path: document.getElementById('td-remote-path').value.trim() || '/',
      mode: document.getElementById('td-remote-mode').value,
    };
    if (type === 'ftp') config.passive = document.getElementById('td-passive').value !== 'false';
  }
  return { name, type, config, enabled: document.getElementById('td-enabled').checked };
}

async function testTransferDestForm() {
  const body = _buildTdBody();
  if (!body) return;
  const btn = document.getElementById('td-form-test-btn');
  const res = document.getElementById('td-test-result');
  if (btn) { btn.textContent = t('td.testing'); btn.disabled = true; }
  if (res) { res.style.display = 'none'; }
  try {
    const r = await api('/api/transfer-destinations/test-config', {
      method: 'POST',
      json: { type: body.type, config: body.config },
    });
    if (res) {
      res.style.display = 'block';
      res.style.background = r.ok ? '#dcfce7' : '#fee2e2';
      res.style.color = r.ok ? '#15803d' : '#dc2626';
      res.style.border = r.ok ? '1px solid #86efac' : '1px solid #fca5a5';
      res.textContent = (r.ok ? '✓ ' : '✗ ') + (r.message || '');
    }
  } catch (e) {
    if (res) {
      res.style.display = 'block';
      res.style.background = '#fee2e2';
      res.style.color = '#dc2626';
      res.style.border = '1px solid #fca5a5';
      res.textContent = '✗ ' + (e.message || t('td.testFail'));
    }
  } finally {
    if (btn) { btn.textContent = t('td.testBtn'); btn.disabled = false; }
  }
}

async function saveTransferDest() {
  const body = _buildTdBody();
  if (!body) return;
  try {
    if (_tdEditId) {
      await api(`/api/transfer-destinations/${_tdEditId}`, { method: 'PUT', json: body });
    } else {
      await api('/api/transfer-destinations', { method: 'POST', json: body });
    }
    _closeTdItem();
    loadTransferDestinations();
    showToast(t('td.saved'));
  } catch (e) {
    showToast(t('td.saveError') + ' ' + esc(e.message || e));
  }
}

async function deleteTransferDest(id) {
  if (!confirm(t('td.deleteConfirm'))) return;
  if (_tdOpenId === id) _closeTdItem();
  try {
    await api(`/api/transfer-destinations/${id}`, { method: 'DELETE' });
    loadTransferDestinations();
    showToast(t('td.deleted'));
  } catch (e) {
    showToast(t('td.deleteError') + ' ' + esc(e.message || e));
  }
}

async function testTransferDest(id) {
  const btn = document.getElementById(`td-test-${id}`);
  if (btn) { btn.textContent = '…'; btn.disabled = true; btn.className = 'td-btn td-btn-test'; }
  try {
    const r = await api(`/api/transfer-destinations/${id}/test`, { method: 'POST' });
    if (btn) {
      btn.textContent = r.ok ? '✓ OK' : '✗';
      btn.className = `td-btn td-btn-test ${r.ok ? 'ok' : 'fail'}`;
      btn.title = r.message || '';
      btn.disabled = false;
    }
    showToast(r.message || (r.ok ? t('td.testOk') : t('td.testFail')));
  } catch (e) {
    if (btn) { btn.textContent = '✗'; btn.className = 'td-btn td-btn-test fail'; btn.disabled = false; }
    showToast(t('td.testError') + ' ' + esc(e.message || e));
  }
}

// ── Telemetry settings ───────────────────────────────────────────────────────
async function loadTelemetrySettings() {
  try {
    const s = await api('/api/telemetry/settings');
    const en = document.getElementById('tlm-enabled');
    if (en) en.checked = !!s.enabled;
  } catch (e) {}
}

async function tlmToggle(checked) {
  try { await api('/api/telemetry/settings', { method: 'PUT', json: { enabled: !!checked } }); }
  catch (e) {}
}
