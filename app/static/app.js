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
  searchMode: localStorage.getItem('tf_search_mode') || 'exact',  // exact | hybrid
  // Channels tab (Files-style grid over the same _groups data).
  chSort: 'count', chSortDir: 'desc',
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
      const prev = document.getElementById('hf-preview-overlay');
      if (prev && prev.classList.contains('open')) { hfClosePreview(); return; }
    }
  });
  document.getElementById('table-wrap').addEventListener('mousemove', onCtxMove);
  document.getElementById('table-wrap').addEventListener('mouseleave', () => {
    document.getElementById('ctx-tip').style.display = 'none';
  });
  // Toolbar can wrap at narrow widths, changing its height.
  // Recalculate sticky offsets so the hunter grid header stays below it.
  let _hgResizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(_hgResizeTimer);
    _hgResizeTimer = setTimeout(() => {
      if (S.activeTab === 'hunter') _hgUpdateStickyTop();
    }, 100);
  });
});

// ── UI password gate (greeter) ───────────────────────────────────────────────
function _startupPhaseMsg(retries) {
  if (retries < 4)  return t('startup.connecting');
  if (retries < 10) return t('startup.dbInit');
  return t('startup.waiting', { n: retries });
}

function _showStartupBar(retries) {
  const bar = document.getElementById('loading-startup-bar');
  const msg = document.getElementById('loading-startup-msg');
  if (bar) bar.style.display = 'flex';
  if (msg) msg.textContent = _startupPhaseMsg(retries);
}

function _hideStartupBar() {
  const bar = document.getElementById('loading-startup-bar');
  if (bar) bar.style.display = 'none';
}

function _isNetworkError(e) { return e instanceof TypeError; }

// ── Greeter readiness polling ────────────────────────────────────────────────
// Called when a network error occurs while the greeter is visible. Disables
// the login button and polls until the backend responds, then re-enables it.
let _greeterPollRunning = false;
function _greeterSetReady(ready) {
  const btn = document.getElementById('ug-login');
  const bar = document.getElementById('ug-startup-bar');
  if (btn) btn.disabled = !ready;
  if (bar) bar.style.display = ready ? 'none' : 'flex';
}
async function _greeterReadinessPoll() {
  if (_greeterPollRunning) return;
  _greeterPollRunning = true;
  _greeterSetReady(false);
  const msgEl = document.getElementById('ug-startup-msg');
  let retries = 0;
  while (true) {
    await new Promise(r => setTimeout(r, 3000));
    retries++;
    if (msgEl) msgEl.textContent = _startupPhaseMsg(retries);
    try {
      const res = await fetch('/api/uiauth/check');
      if (res.ok) {
        _greeterPollRunning = false;
        _greeterSetReady(true);
        document.getElementById('ug-msg').textContent = '';
        document.getElementById('ug-pass')?.focus();
        return;
      }
    } catch (e) {}
  }
}

async function uiAuthBoot() {
  show('loading-screen');
  let retries = 0;
  while (true) {
    try {
      const res = await fetch('/api/uiauth/check');
      if (res.ok) {
        const r = await res.json();
        _hideStartupBar();
        if (r.authenticated) {
          // Keep loading screen visible until checkAuth() completes
          try {
            await checkAuth();
            return;
          } catch (e) {
            // Backend dropped during checkAuth — keep retrying
          }
        } else {
          hide('loading-screen');
          show('ui-greeter');
          setTimeout(() => document.getElementById('ug-pass')?.focus(), 30);
          return;
        }
      }
    } catch (e) {}
    // Backend not reachable yet — keep polling and show progress
    retries++;
    show('loading-screen');
    _showStartupBar(retries);
    await new Promise(resolve => setTimeout(resolve, 3000));
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
    if (_isNetworkError(e)) {
      msg.textContent = t('startup.connecting');
      _greeterReadinessPoll();
      return;
    }
    msg.textContent = e.message || t('pw.wrongPass');
    return;
  }
  document.getElementById('ug-pass').value = '';
  hide('ui-greeter');
  try {
    await checkAuth();
  } catch (e) {
    // Backend disappeared right after login — go back to startup polling
    show('loading-screen');
    uiAuthBoot();
  }
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
const _THEME_META = {
  light:     { bg:'#f0f2f5', card:'#fff',     l1:'#2563eb', l2:'#9ca3af', l3:'#d1d5db', shadow:true  },
  dark:      { bg:'#0f172a', card:'#1e293b',  l1:'#3b82f6', l2:'#94a3b8', l3:'#475569'              },
  nord:      { bg:'#2e3440', card:'#3b4252',  l1:'#88c0d0', l2:'#d8dee9', l3:'#5e81ac'              },
  solarized: { bg:'#fdf6e3', card:'#fefaf3',  l1:'#268bd2', l2:'#586e75', l3:'#93a1a1', shadow:true  },
  sepia:     { bg:'#f4ecd8', card:'#fbf6e8',  l1:'#a64b1c', l2:'#5d4a26', l3:'#9b8762', shadow:true  },
  forest:    { bg:'#0d1f17', card:'#142c20',  l1:'#4ade80', l2:'#bcd6c5', l3:'#5e8474'              },
  slate:     { bg:'#0f172a', card:'#1e293b',  l1:'#a78bfa', l2:'#cbd5e1', l3:'#64748b'              },
  crimson:   { bg:'#1a0d11', card:'#291418',  l1:'#ef4444', l2:'#e2c3c8', l3:'#8b6470'              },
  rosepine:  { bg:'#191724', card:'#1f1d2e',  l1:'#ebbcba', l2:'#c4c1d8', l3:'#6e6a86'              },
  mocha:     { bg:'#f3eee6', card:'#faf6ee',  l1:'#7c5a30', l2:'#5a4a30', l3:'#9c8966', shadow:true  },
};

function _applyThemePreview(name) {
  const m = _THEME_META[name] || _THEME_META.light;
  const prev = document.getElementById('theme-prev');
  if (prev) prev.style.background = m.bg;
  const card = document.getElementById('theme-prev-card');
  if (card) { card.style.background = m.card; card.style.boxShadow = m.shadow ? '0 1px 2px rgba(0,0,0,.08)' : 'none'; }
  const l1 = document.getElementById('theme-prev-l1');
  if (l1) l1.style.background = m.l1;
  const l2 = document.getElementById('theme-prev-l2');
  if (l2) l2.style.background = m.l2;
  const l3 = document.getElementById('theme-prev-l3');
  if (l3) l3.style.background = m.l3;
  const sel = document.getElementById('theme-select');
  if (sel) sel.value = name;
}

function setTheme(name) {
  document.documentElement.setAttribute('data-theme', name);
  try { localStorage.setItem('theme', name); } catch(e){}
  _applyThemePreview(name);
}

function applySavedTheme() {
  let saved = 'light';
  try { saved = localStorage.getItem('theme') || 'light'; } catch(e){}
  document.documentElement.setAttribute('data-theme', saved);
  _applyThemePreview(saved);
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
  _initSemanticToggle();
  _initCaptionToggle();
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
  const exportMeta = document.getElementById('export-meta');
  if (exportMeta && _stats.total_files) {
    // Export now contains three concatenated sources:
    //   • telegram messages excluding parsed-torrent placeholders
    //     (raw row count: total_files − torrent_parsed_count)
    //   • inner files of every parsed torrent (torrent_content_files)
    //   • inner files of every magnet's files_json (magnet_file_count)
    // The status bar's "Tümü" pill shows the deduped/library variant —
    // these numbers are close but not identical because the TSV keeps
    // cross-channel duplicates whereas the pill collapses them.
    const telegramRows = (_stats.total_files || 0) - (_stats.torrent_parsed_count || 0);
    const torrentInner = _stats.torrent_content_files || 0;
    const magnetInner  = _stats.magnet_file_count || 0;
    const exportTotal  = telegramRows + torrentInner + magnetInner;
    exportMeta.textContent = `${exportTotal.toLocaleString()} ${t('export.rowCount')}`;
    const parts = [`📨 ${telegramRows.toLocaleString()} Telegram`];
    if (torrentInner > 0) parts.push(`📦 ${torrentInner.toLocaleString()} torrent içi`);
    if (magnetInner > 0)  parts.push(`🧲 ${magnetInner.toLocaleString()} magnet içi`);
    exportMeta.title = parts.join(' · ');
  }

  const sz = (n, b) =>
    `${(n||0).toLocaleString()}<br><span class="ts-sz">${fmtSize(b||0)}</span>`;

  if (el24) {
    el24.innerHTML = sz(_stats.recent_24h || 0, _stats.recent_24h_size || 0);
    const sb24 = el24.closest('.sb-stats');
    if (sb24) {
      const inner24 = (_stats.torrent_content_24h || 0) - (_stats.torrent_parsed_24h || 0);
      if (inner24 > 0) sb24.dataset.tip = `+${inner24.toLocaleString()} ${t('topstats.torrentInner')}`;
      else delete sb24.dataset.tip;
    }
  }

  if (el7d) {
    el7d.innerHTML = sz(_stats.recent_7d || 0, _stats.recent_7d_size || 0);
    const sb7 = el7d.closest('.sb-stats');
    if (sb7) {
      const inner7 = (_stats.torrent_content_7d || 0) - (_stats.torrent_parsed_7d || 0);
      if (inner7 > 0) sb7.dataset.tip = `+${inner7.toLocaleString()} ${t('topstats.torrentInner')}`;
      else delete sb7.dataset.tip;
    }
  }

  if (elAll) {
    const magnetCount = _stats.magnet_file_count || 0;
    const magnetSize  = _stats.magnet_file_size  || 0;
    // Use the DEDUPED counts (files_canonical) so the bar matches the
    // "X benzersiz dosya · Y" pill on the Files tab. Earlier we used raw
    // counts which inflated the totals when the same file appeared in
    // multiple channels.
    const allCount = (_stats.unique_virtual_files || _stats.unique_files || 0) + magnetCount;
    const allSize  = (_stats.unique_total_size  || 0) + magnetSize;
    elAll.innerHTML = sz(allCount, allSize);
    const sbAll = document.getElementById('sb-stats-all');
    if (sbAll) {
      // Tooltip surfaces the raw Telegram count so the user can still see
      // the "with duplicates" number when they need it.
      const parts = [];
      const rawCount     = _stats.total_files || 0;
      const torrentInner = (_stats.torrent_content_files || 0) - (_stats.torrent_parsed_count || 0);
      const uniqueCount  = _stats.unique_files || 0;
      parts.push(`${uniqueCount.toLocaleString()} ${t('topstats.uniqueFiles') || 'benzersiz dosya'}`);
      parts.push(`(${rawCount.toLocaleString()} ${t('topstats.realTg')})`);
      if (torrentInner > 0) parts.push(`+${torrentInner.toLocaleString()} ${t('topstats.torrentInner')}`);
      if (magnetCount > 0)  parts.push(`+${magnetCount.toLocaleString()} ${t('topstats.magnetInner')}`);
      sbAll.dataset.tip = parts.join('\n');
    }
  }

  _bindSbTooltip();
}

// Fixed-position tooltip for stat blocks — sidesteps overflow:hidden on #app-status-bar/#main
function _bindSbTooltip() {
  const tip = document.getElementById('sb-tip');
  if (!tip) return;
  document.querySelectorAll('#app-status-bar .sb-stats[data-tip]').forEach(el => {
    if (el._sbTipBound) return;
    el._sbTipBound = true;
    el.addEventListener('mouseenter', () => {
      const text = el.dataset.tip;
      if (!text) return;
      tip.textContent = text;
      tip.style.display = 'block';
      const r  = el.getBoundingClientRect();
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      let left = r.left + r.width / 2 - tw / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
      tip.style.left = left + 'px';
      tip.style.top  = (r.top - th - 8) + 'px';
    });
    el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  });
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

function _loginNetworkErr(e) {
  if (_isNetworkError(e)) { loginMsg(t('startup.connecting')); return true; }
  return false;
}
async function authSendCode() {
  const phone = document.getElementById('inp-phone').value.trim();
  if (!phone) return;
  try {
    await api('/api/auth/send-code', {method:'POST',json:{phone, account_id: _loginAccountId}});
    showStep('code');
  } catch(e) { if (!_loginNetworkErr(e)) loginMsg(e.message); }
}
async function authVerifyCode() {
  const phone = document.getElementById('inp-phone').value.trim();
  const code  = document.getElementById('inp-code').value.trim();
  try {
    const r = await api('/api/auth/verify-code', {method:'POST',json:{phone, code, account_id: _loginAccountId}});
    if (r.needs_2fa) { showStep('2fa'); return; }
    _afterLogin();
  } catch(e) { if (!_loginNetworkErr(e)) loginMsg(e.message); }
}
async function authVerifyPass() {
  const password = document.getElementById('inp-pass').value;
  try {
    await api('/api/auth/verify-password', {method:'POST',json:{password, account_id: _loginAccountId}});
    _afterLogin();
  } catch(e) { if (!_loginNetworkErr(e)) loginMsg(e.message); }
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
      // While sync is running, refresh in place only when the user is on the
      // FIRST page of an infinite-scroll grid — otherwise the silent reload
      // would jump them back to the top mid-scroll. After they've loaded
      // more, we wait until sync finishes (see else branch below).
      if (S.activeTab === 'files' && _currentFiles.length <= S.limit) {
        loadFiles(true);
      } else if (S.activeTab === 'links') {
        loadLinks(true);
      }
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
  // Channels tab uses the same dataset; keep it in sync after every refresh.
  if (typeof renderChannelsTable === 'function') renderChannelsTable();
  if (typeof _syncChannelsShowHiddenBtn === 'function') _syncChannelsShowHiddenBtn();
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
  // Legacy settings→groups sidebar. After the Channels tab took over, the
  // markup may not be in the DOM anymore — bail out cleanly when missing.
  const q  = (document.getElementById('group-filter')?.value||'').toLowerCase();
  const el = document.getElementById('group-list');
  if (!el) { renderChannelsTable(); return; }

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

function toggleShowHidden() {
  S.showHidden = !S.showHidden;
  renderSidebar();
  // Mirror the same toggle on the Channels tab so the two views stay in sync.
  if (S.activeTab === 'channels') renderChannelsTable();
  _syncChannelsShowHiddenBtn();
}

// ── Channels tab (Files-style grid over /api/groups) ─────────────────────────
async function loadChannelsTab() {
  // Refresh the groups dataset first so the grid reflects the latest sync.
  await loadGroups();
  _syncChannelsShowHiddenBtn();
  renderChannelsTable();
}

function _syncChannelsShowHiddenBtn() {
  const btn = document.getElementById('ch-show-hidden');
  if (!btn) return;
  btn.classList.toggle('active', !!S.showHidden);
  const hiddenCount = _groups.filter(g => g.hidden).length;
  btn.textContent = S.showHidden
    ? t('channels.showAll')
    : `${t('channels.showHidden')}${hiddenCount ? ' · ' + hiddenCount : ''}`;
}

function channelsToggleShowHidden() {
  S.showHidden = !S.showHidden;
  _syncChannelsShowHiddenBtn();
  renderChannelsTable();
}

function channelsToggleAddCard() {
  const card = document.getElementById('channels-add-card');
  if (!card) return;
  const open = card.style.display !== 'none';
  card.style.display = open ? 'none' : '';
  if (!open) setTimeout(() => document.getElementById('ch-add-input')?.focus(), 30);
}

function channelsSortBy(col) {
  if (S.chSort === col) {
    S.chSortDir = S.chSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    S.chSort = col;
    // Numeric columns default to descending, text columns to ascending.
    S.chSortDir = (col === 'name' || col === 'username') ? 'asc' : 'desc';
  }
  renderChannelsTable();
}

function _chState(g) {
  // Single-token state used both for filter matching and sorting.
  if (g.hidden)   return 'hidden';
  if (g.excluded) return 'excluded';
  return 'active';
}

function _chSortFn(a, b) {
  let av, bv;
  switch (S.chSort) {
    case 'name':     av = (a.display_name || a.name || '').toLowerCase();
                     bv = (b.display_name || b.name || '').toLowerCase(); break;
    case 'username': av = (a.username || '').toLowerCase();
                     bv = (b.username || '').toLowerCase(); break;
    case 'count':    av = a.file_count  || 0; bv = b.file_count  || 0; break;
    case 'size':     av = a.total_size  || 0; bv = b.total_size  || 0; break;
    case 'members':  av = a.member_count|| 0; bv = b.member_count|| 0; break;
    case 'sync':     av = a.last_sync_at    ? Date.parse(a.last_sync_at)    : 0;
                     bv = b.last_sync_at    ? Date.parse(b.last_sync_at)    : 0; break;
    case 'msg':      av = a.last_message_at ? Date.parse(a.last_message_at) : 0;
                     bv = b.last_message_at ? Date.parse(b.last_message_at) : 0; break;
    case 'state':    av = _chState(a); bv = _chState(b); break;
    case 'type_video':    av = a.type_video    || 0; bv = b.type_video    || 0; break;
    case 'type_audio':    av = a.type_audio    || 0; bv = b.type_audio    || 0; break;
    case 'type_image':    av = a.type_image    || 0; bv = b.type_image    || 0; break;
    case 'type_archive':  av = a.type_archive  || 0; bv = b.type_archive  || 0; break;
    case 'type_document': av = a.type_document || 0; bv = b.type_document || 0; break;
    case 'type_software': av = a.type_software || 0; bv = b.type_software || 0; break;
    case 'type_torrent':  av = a.type_torrent  || 0; bv = b.type_torrent  || 0; break;
    case 'type_other':    av = a.type_other    || 0; bv = b.type_other    || 0; break;
    default:         av = 0; bv = 0;
  }
  const cmp = (typeof av === 'string') ? av.localeCompare(bv, 'tr') : (av - bv);
  return S.chSortDir === 'asc' ? cmp : -cmp;
}

function _fmtAgo(iso) {
  if (!iso) return '—';
  const t0 = Date.parse(iso);
  if (!isFinite(t0)) return '—';
  const diff = Math.max(0, Date.now() - t0);
  const m = Math.floor(diff / 60000);
  if (m < 1) return t('channels.justNow');
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h';
  const d = Math.floor(h / 24);
  if (d < 30) return d + 'd';
  return new Date(t0).toLocaleDateString();
}

function renderChannelsTable() {
  const tbody = document.getElementById('channels-tbody');
  if (!tbody) return;

  const qSearch  = (document.getElementById('ch-search')?.value || '').toLowerCase();
  const qName    = (document.getElementById('ch-flt-name')?.value || '').toLowerCase();
  const qUser    = (document.getElementById('ch-flt-user')?.value || '').toLowerCase();
  const minFiles = parseInt(document.getElementById('ch-flt-min-files')?.value || '', 10);
  const minMembers = parseInt(document.getElementById('ch-flt-min-members')?.value || '', 10);
  const minSizeMB = parseFloat(document.getElementById('ch-flt-min-size')?.value || '');
  const stFlt    = document.getElementById('ch-flt-state')?.value || '';
  const minSizeBytes = isNaN(minSizeMB) ? null : Math.round(minSizeMB * 1048576);

  const visible = _groups.filter(g => {
    // "Gizliler" toggle handles hidden visibility; explicit state filter
    // (Tümü/Takipte/Takip Edilmiyor/Gizli) overrides if set.
    if (stFlt) {
      if (_chState(g) !== stFlt) return false;
    } else if (S.showHidden) { if (!g.hidden) return false; }
      else                    { if ( g.hidden) return false; }
    const nm = (g.display_name || g.name || '').toLowerCase();
    const un = (g.username || '').toLowerCase();
    if (qSearch && !nm.includes(qSearch) && !un.includes(qSearch)) return false;
    if (qName   && !nm.includes(qName))                            return false;
    if (qUser   && !un.includes(qUser))                            return false;
    if (!isNaN(minMembers)  && (g.member_count || 0) < minMembers) return false;
    if (!isNaN(minFiles)    && (g.file_count   || 0) < minFiles)   return false;
    if (minSizeBytes != null && (g.total_size  || 0) < minSizeBytes) return false;
    return true;
  });

  visible.sort(_chSortFn);

  // Sort arrows. Map sort key → arrow element id (kept short in the markup).
  const _arr = {
    name:'name', username:'username', members:'members',
    count:'count', size:'size',
    sync:'sync', msg:'msg', state:'state',
    type_video:'tv', type_audio:'ta', type_image:'ti', type_archive:'tar',
    type_document:'td', type_software:'ts', type_torrent:'tt', type_other:'to',
  };
  Object.entries(_arr).forEach(([k, id]) => {
    const el = document.getElementById('ch-arr-' + id);
    if (!el) return;
    el.textContent = S.chSort === k ? (S.chSortDir === 'asc' ? '↑' : '↓') : '';
  });

  // Pill always shows the LIBRARY total (sum across every group), so it
  // matches the figures on the Status tab regardless of the current filter.
  // When a filter narrows the table, the filtered count is appended.
  const libTotalChans = _groups.length;
  const libTotalFiles = _groups.reduce((s, g) => s + (g.file_count || 0), 0);
  const libTotalSize  = _groups.reduce((s, g) => s + (g.total_size || 0), 0);
  const fltFiles = visible.reduce((s, g) => s + (g.file_count || 0), 0);
  const fltSize  = visible.reduce((s, g) => s + (g.total_size || 0), 0);
  const filtered = (visible.length !== libTotalChans);
  const pill = document.getElementById('ch-count-pill');
  if (pill) {
    let txt = `${libTotalChans.toLocaleString()} ${t('channels.unit')} · ` +
              `${libTotalFiles.toLocaleString()} ${t('channels.fileUnit')} · ${fmtSize(libTotalSize)}`;
    if (filtered) {
      txt += ` · ${t('channels.filtered')}: ${visible.length.toLocaleString()} / ` +
             `${fltFiles.toLocaleString()} / ${fmtSize(fltSize)}`;
    }
    pill.textContent = txt;
  }

  document.getElementById('channels-empty').style.display = visible.length === 0 ? '' : 'none';

  const _mini = v => (v ? v.toLocaleString() : '');
  tbody.innerHTML = visible.map((g, i) => {
    const isSel = S.selectedGroups.has(g.id);
    const name  = plainName(g.display_name || g.name || ('id ' + g.id));
    const user  = g.username ? '@' + g.username : '';
    const tgHref = tgGroupHref(g);
    const state = g.hidden
      ? `<span class="ch-state ch-state-hidden">${esc(t('channels.stHidden'))}</span>`
      : (g.excluded
          ? `<span class="ch-state ch-state-excl">${esc(t('channels.stUntracked'))}</span>`
          : `<span class="ch-state ch-state-active">${esc(t('channels.stActive'))}</span>`);
    return `<tr class="${isSel ? 'row-selected' : ''}${g.hidden ? ' row-hidden' : ''}" data-gid="${g.id}" onclick="channelsRowClick(${g.id},event)">
      <td class="chk-cell"><input type="checkbox" ${isSel ? 'checked' : ''} onclick="event.stopPropagation();_chChk(event,${g.id},this.checked)"></td>
      <td class="num-cell">${i + 1}</td>
      <td class="ch-name-cell" title="${esc(name)}">${esc(name)}</td>
      <td class="ch-col-user">${user ? `<a href="${esc(tgHref)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(user)}</a>` : ''}</td>
      <td class="ch-col-members" title="${g.member_count_updated_at ? esc(t('channels.membersUpdated') + ': ' + new Date(g.member_count_updated_at).toLocaleString()) : ''}">${g.member_count != null ? Number(g.member_count).toLocaleString() : '—'}</td>
      <td class="ch-col-count">${(g.file_count || 0).toLocaleString()}</td>
      <td class="ch-col-size">${fmtSize(g.total_size || 0)}</td>
      <td class="ch-col-mini">${_mini(g.type_video)}</td>
      <td class="ch-col-mini">${_mini(g.type_audio)}</td>
      <td class="ch-col-mini">${_mini(g.type_image)}</td>
      <td class="ch-col-mini">${_mini(g.type_archive)}</td>
      <td class="ch-col-mini">${_mini(g.type_document)}</td>
      <td class="ch-col-mini">${_mini(g.type_software)}</td>
      <td class="ch-col-mini">${_mini(g.type_torrent)}</td>
      <td class="ch-col-mini">${_mini(g.type_other)}</td>
      <td class="ch-col-msg"  title="${g.last_message_at ? esc(new Date(g.last_message_at).toLocaleString()) : ''}">${esc(_fmtAgo(g.last_message_at))}</td>
      <td class="ch-col-sync" title="${g.last_sync_at    ? esc(new Date(g.last_sync_at).toLocaleString())    : ''}">${esc(_fmtAgo(g.last_sync_at))}</td>
      <td class="ch-col-state">${state}</td>
      <td class="ch-col-act"><div class="ch-row-acts">
        <button class="ga" onclick="event.stopPropagation();openGroupNameEdit(event,${g.id})" title="${esc(t('groups.editName'))}">✏</button>
        <a class="ga" href="${esc(tgHref)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="${esc(t('table.openTg'))}">↗</a>
      </div></td>
    </tr>`;
  }).join('');

  // Bulk bar visibility + button labels follow the same rules as the legacy sidebar.
  const bar = document.getElementById('ch-bulk-bar');
  if (bar) {
    if (S.selectedGroups.size > 0) {
      bar.style.display = 'inline-flex';
      document.getElementById('ch-bulk-count').textContent =
        t('groups.bulkSelected', { n: S.selectedGroups.size });
      const ids = [...S.selectedGroups];
      const sel = ids.map(id => _groups.find(x => x.id === id)).filter(Boolean);
      const allHidden   = sel.length > 0 && sel.every(g => g.hidden);
      const allExcluded = sel.length > 0 && sel.every(g => g.excluded);
      const hideBtn = document.getElementById('ch-bulk-hide');
      const exclBtn = document.getElementById('ch-bulk-excl');
      if (hideBtn) hideBtn.textContent = t(allHidden   ? 'groups.bulkShow'  : 'groups.bulkHide');
      if (exclBtn) exclBtn.textContent = t(allExcluded ? 'groups.bulkTrack' : 'groups.bulkUntrack');
    } else {
      bar.style.display = 'none';
    }
  }

  // Sync "select all" checkbox state.
  const sa = document.getElementById('ch-select-all');
  if (sa) sa.checked = visible.length > 0 && visible.every(g => S.selectedGroups.has(g.id));
}

function channelsRowClick(id, e) {
  // Don't hijack clicks on row-internal interactive elements (checkboxes,
  // links, per-row action buttons). Plain row click opens the detail popup;
  // selection is done via the explicit checkbox column.
  if (e && (e.target.closest('a') || e.target.closest('button') || e.target.closest('input'))) return;
  channelShowDetail(id);
}

let _lastChChannelId = null;

function _chChk(e, id, on) {
  if (e.shiftKey && _lastChChannelId != null) {
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.removeAllRanges) sel.removeAllRanges();
    const rows = [...document.querySelectorAll('#channels-tbody tr[data-gid]')];
    const ids = rows.map(tr => parseInt(tr.dataset.gid, 10));
    const i = ids.indexOf(_lastChChannelId);
    const j = ids.indexOf(id);
    if (i >= 0 && j >= 0) {
      const [a, b] = i <= j ? [i, j] : [j, i];
      for (let k = a; k <= b; k++) {
        if (on) S.selectedGroups.add(ids[k]); else S.selectedGroups.delete(ids[k]);
      }
      renderChannelsTable();
      return;
    }
  }
  if (on) S.selectedGroups.add(id); else S.selectedGroups.delete(id);
  _lastChChannelId = id;
  renderChannelsTable();
}

function channelsToggleOne(id, on) {
  if (on) S.selectedGroups.add(id); else S.selectedGroups.delete(id);
  renderChannelsTable();
}

// ── Channel detail popup — same overlay as Channel Hunter ────────────────────
let _currentChannelGid = null;

async function channelShowDetail(gid) {
  try {
    const c = await api(`/api/channels/${gid}/detail`);
    // Backend sets c.kind = 'channel'; shared renderer handles the visuals.
    _renderDetailModal(c);
  } catch (e) {
    alert(e.message || e);
  }
}

function closeDetailModal() {
  hfClosePreview();
  document.getElementById('hunter-detail-overlay').classList.remove('open');
  if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
  Object.keys(_hdDlPollers).forEach(k => _stopFileDlPoller(parseInt(k, 10)));
  _hdDlStatus = {};
  _currentDetailCid = null;
  _currentChannelGid = null;
  _currentDetailUsername = null;
  _hdScanState = null;
  _hdScanProcessed = 0;
  _hdFilesBase = null;
}
function closeChannelDetail() { closeDetailModal(); }  // compat alias

function _bindChDetailActions() {
  const bar = document.getElementById('hd-actions');
  if (!bar) return;
  // Replace any prior listener (hunter or channel) by cloning the node.
  const fresh = bar.cloneNode(true);
  bar.parentNode.replaceChild(fresh, bar);
  fresh.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === 'close') { closeChannelDetail(); return; }
    const gid = _currentChannelGid;
    if (gid == null) return;
    const g = _groups.find(x => x.id === gid);
    switch (act) {
      case 'ch-files': {
        closeChannelDetail();
        S.activeGroupId = gid;
        S.offset = 0;
        renderChips();
        switchTab('files');
        break;
      }
      case 'ch-rescan':
        try {
          const r = await api(`/api/groups/${gid}/rescan`, { method: 'POST' });
          showToast(r.queued
            ? t('groups.rescanStarted', { name: esc(r.name || g?.name || gid) })
            : t('groups.rescanQueued',  { name: esc(r.name || g?.name || gid) }),
            3000);
        } catch (err) {
          showToast(t('groups.rescanFail') + ' ' + esc(err.message), 4000);
        }
        break;
      case 'ch-hide': {
        const next = !(g && g.hidden);
        await api(`/api/groups/${gid}`, { method: 'PATCH', json: { hidden: next } });
        _setGroupOverride(gid, { hidden: next });
        await loadGroups();
        closeChannelDetail();
        break;
      }
      case 'ch-excl': {
        const next = !(g && g.excluded);
        await api(`/api/groups/${gid}`, { method: 'PATCH', json: { excluded: next } });
        await loadGroups();
        closeChannelDetail();
        break;
      }
      case 'ch-leave': {
        const name = g ? (g.display_name || g.name) : `#${gid}`;
        if (!confirm(t('groups.leaveConfirm', { name }))) break;
        const purge = confirm(t('groups.purgeConfirm', { count: (g?.file_count || 0).toLocaleString() }));
        try {
          await api(`/api/groups/${gid}/leave?purge=${purge}`, { method: 'POST' });
          showToast(t('groups.leaveOk', { name: esc(name) }), 3000);
        } catch (err) {
          showToast(t('groups.leaveFail') + ' ' + esc(err.message), 5000);
          break;
        }
        S.selectedGroups.delete(gid);
        await loadGroups();
        closeChannelDetail();
        break;
      }
    }
  });
}

function channelsToggleAll(on) {
  // Apply to the currently visible (filtered) rows only.
  document.querySelectorAll('#channels-tbody tr[data-gid]').forEach(tr => {
    const id = +tr.dataset.gid;
    if (on) S.selectedGroups.add(id); else S.selectedGroups.delete(id);
  });
  renderChannelsTable();
}

// ── Channels tab — Add Channel(s) flow ───────────────────────────────────────
const _CH_USERNAME_RE = /(?:@|t(?:elegram)?\.me\/|tg:\/\/resolve\?domain=)([A-Za-z][A-Za-z0-9_]{3,31})\b/g;

function _parseChannelInput(raw) {
  // Extract every plausible Telegram channel reference from arbitrary text.
  // Returns a deduped list of bare usernames (no @, lowercase).
  const out = new Set();
  if (!raw) return [];
  // First, scan with the URL/@ regex (catches t.me/x, @x, tg://resolve?domain=x).
  let m;
  _CH_USERNAME_RE.lastIndex = 0;
  while ((m = _CH_USERNAME_RE.exec(raw)) !== null) {
    out.add(m[1].toLowerCase());
  }
  // Second pass: bare tokens on whitespace boundaries that look like usernames.
  // Keeps the simple "@foo, @bar" or "foo\nbar" cases working when users skip
  // the @ prefix entirely.
  for (const tok of raw.split(/[\s,;]+/)) {
    const t = tok.replace(/^@/, '').trim();
    if (/^[A-Za-z][A-Za-z0-9_]{3,31}$/.test(t)) out.add(t.toLowerCase());
  }
  return [...out];
}

function _renderAddPreview(list) {
  const wrap = document.getElementById('ch-add-preview');
  const node = document.getElementById('ch-add-preview-list');
  if (!wrap || !node) return;
  if (!list.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  node.innerHTML = list.map(u => `<span class="ch-tok">@${esc(u)}</span>`).join('');
}

function _showAddResult(kind, html) {
  const res = document.getElementById('ch-add-result');
  if (!res) return;
  res.style.display = '';
  res.className = 'ch-add-result ' + kind;
  res.innerHTML = html;
}

function _clearAddInputs() {
  const ta = document.getElementById('ch-add-input');
  if (ta) ta.value = '';
  _renderAddPreview([]);
  _chParsedCache = null;
}

// Disable inputs + buttons + show busy cursor on the add card so it never
// looks like the page hung mid-operation.
function _chSetBusy(busy) {
  const card = document.getElementById('channels-add-card');
  if (!card) return;
  card.classList.toggle('ch-busy', !!busy);
  card.querySelectorAll('button, textarea, input').forEach(el => {
    el.disabled = !!busy;
  });
}

function _chRenderProgress(done, total, label) {
  const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  _showAddResult('warn',
    `<div class="ch-prog-row"><span>${esc(label)}</span>` +
      `<span class="ch-prog-count">${done.toLocaleString()} / ${total.toLocaleString()}</span></div>` +
    `<div class="ch-prog-bar"><div class="ch-prog-fill" style="width:${pct}%"></div></div>`);
}

// Cache of {raw, list} from the last preview/submit so chunked sends can
// skip re-parsing + avoid re-fetching external URLs on the backend.
let _chParsedCache = null;

// Resolve user input → username list. Uses the cached parse if the raw text
// is unchanged; otherwise asks the backend (which can also fetch external
// pages for embedded channel links).
async function _chResolveUsernames(raw) {
  if (_chParsedCache && _chParsedCache.raw === raw) return _chParsedCache.list;
  const hasExternal = /https?:\/\/(?!t(?:elegram)?\.me\/)/i.test(raw);
  _chRenderProgress(0, 1, hasExternal ? t('channels.parsingUrl') : t('channels.parsing'));
  const r = await api('/api/channels/parse', { method: 'POST', json: { text: raw } });
  const list = r.usernames || [];
  _chParsedCache = { raw, list };
  return list;
}

async function channelsParsePreview() {
  const raw = document.getElementById('ch-add-input')?.value || '';
  if (!raw.trim()) {
    _chParsedCache = null;
    _renderAddPreview([]);
    _showAddResult('warn', esc(t('channels.parseEmpty')));
    return;
  }
  // Show instant local-parse feedback while the backend fetch (if any) runs
  // so the UI never looks frozen during the round-trip.
  const localList = _parseChannelInput(raw);
  if (localList.length) _renderAddPreview(localList);

  _chSetBusy(true);
  try {
    const list = await _chResolveUsernames(raw);
    _renderAddPreview(list);
    _showAddResult(list.length ? 'ok' : 'warn',
      list.length ? esc(t('channels.parsedN', { n: list.length }))
                  : esc(t('channels.parseEmpty')));
  } catch (e) {
    _showAddResult('err', esc(t('channels.error') + ': ' + (e?.message || e)));
  } finally {
    _chSetBusy(false);
  }
}

// Generic chunked submitter — yields control between batches and updates the
// progress bar so a 5,000-channel paste never blocks the UI thread.
async function _chSubmitChunked(usernames, action, labelKey, mergeFn) {
  const CHUNK = 200;
  const aggregate = { joined: [], queued: [], skipped: [], failed: [], added: [],
                      skipped_blacklisted: [], skipped_joined: [], skipped_queued: [] };
  for (let i = 0; i < usernames.length; i += CHUNK) {
    const slice = usernames.slice(i, i + CHUNK);
    _chRenderProgress(i, usernames.length, t(labelKey));
    const r = await api('/api/channels/add', {
      method: 'POST',
      json:   { usernames: slice, action },
    });
    if (action === 'hunter') {
      aggregate.added.push(...(r.added || []));
      aggregate.skipped_blacklisted.push(...(r.skipped_blacklisted || []));
      aggregate.skipped_joined.push(...(r.skipped_joined || []));
      aggregate.skipped_queued.push(...(r.skipped_queued || []));
    } else {
      aggregate.joined.push (...(r.joined  || []));
      aggregate.queued.push (...(r.queued  || []));
      aggregate.skipped.push(...(r.skipped || []));
      aggregate.failed.push (...(r.failed  || []));
    }
    // Yield to let the browser repaint between batches.
    await new Promise(rs => setTimeout(rs, 0));
  }
  _chRenderProgress(usernames.length, usernames.length, t(labelKey));
  return aggregate;
}

async function channelsSubmitJoin() {
  const raw = document.getElementById('ch-add-input')?.value || '';
  if (!raw.trim()) { _showAddResult('warn', esc(t('channels.parseEmpty'))); return; }
  _chSetBusy(true);
  try {
    const list = await _chResolveUsernames(raw);
    if (!list.length) { _showAddResult('warn', esc(t('channels.parseEmpty'))); return; }
    _renderAddPreview(list);
    const agg = await _chSubmitChunked(list, 'join', 'channels.joinProgress');
    let html = `<b>${esc(t('channels.joinDone', { n: agg.joined.length }))}</b>`;
    if (agg.queued.length)  html += `<br>${esc(t('channels.joinQueued',  { n: agg.queued.length  }))}`;
    if (agg.skipped.length) html += `<br>${esc(t('channels.joinSkipped', { n: agg.skipped.length }))}`;
    if (agg.failed.length) {
      html += `<br><b>${esc(t('channels.joinFailed', { n: agg.failed.length }))}</b><ul>` +
              agg.failed.slice(0, 8).map(f => `<li>@${esc(f.username)} — ${esc(f.error || '')}</li>`).join('') +
              `</ul>`;
    }
    _showAddResult(agg.failed.length ? 'warn' : 'ok', html);
    if (!agg.failed.length) _clearAddInputs();
    await loadChannelsTab();
    if (typeof loadFiles === 'function') loadFiles();
  } catch (e) {
    _showAddResult('err', esc(t('channels.error') + ': ' + (e?.message || e)));
  } finally {
    _chSetBusy(false);
  }
}

async function channelsSubmitHunter() {
  const raw = document.getElementById('ch-add-input')?.value || '';
  if (!raw.trim()) { _showAddResult('warn', esc(t('channels.parseEmpty'))); return; }
  _chSetBusy(true);
  try {
    const list = await _chResolveUsernames(raw);
    if (!list.length) { _showAddResult('warn', esc(t('channels.parseEmpty'))); return; }
    _renderAddPreview(list);
    const agg = await _chSubmitChunked(list, 'hunter', 'channels.huntProgress');
    const hasAny = agg.added.length || agg.skipped_blacklisted.length ||
                   agg.skipped_joined.length || agg.skipped_queued.length;
    const kind = agg.skipped_blacklisted.length && !agg.added.length ? 'warn' : 'ok';
    let html = `<b>${esc(t('channels.huntDone', { n: agg.added.length }))}</b>`;
    if (agg.added.length) html += `<br><span style="font-size:.76rem;color:var(--text-3)">${esc(t('channels.huntDoneHint'))}</span>`;
    if (agg.skipped_blacklisted.length) html += `<br>${esc(t('channels.huntSkippedBlacklisted', { n: agg.skipped_blacklisted.length }))}`;
    if (agg.skipped_joined.length)      html += `<br>${esc(t('channels.huntSkippedJoined',      { n: agg.skipped_joined.length }))}`;
    if (agg.skipped_queued.length)      html += `<br>${esc(t('channels.huntSkippedQueued',      { n: agg.skipped_queued.length }))}`;
    _showAddResult(kind, html);
    _clearAddInputs();
  } catch (e) {
    _showAddResult('err', esc(t('channels.error') + ': ' + (e?.message || e)));
  } finally {
    _chSetBusy(false);
  }
}

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
  // Sync localStorage override so _applyGroupOverrides doesn't undo the change
  // on the next loadGroups() call (this was masking bulk hide/show updates).
  ids.forEach(id => _setGroupOverride(id, { hidden: next }));
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
  renderChannelsTable();
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

function bulkClearSelection() { S.selectedGroups.clear(); renderSidebar(); renderChannelsTable(); }

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
    loadNotifyPushSettings();
  } else if (name === 'account') {
    loadAccountsList();
    _refreshUiPwState();
    loadTelemetrySettings();
    _torrentCtrlRefresh();
  } else if (name === 'transfer') {
    loadTransferDestinations();
    loadBandwidthTab();
  }
}

function switchTab(tab) {
  S.activeTab = tab;
  ['files','channels','links','settings','downloads','status','hunter'].forEach(t =>
    document.getElementById('tab-'+t)?.classList.toggle('active', t===tab));

  const isFiles     = tab === 'files';
  const isChannels  = tab === 'channels';
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
  const cp = document.getElementById('channels-panel');
  if (cp) cp.style.display = isChannels ? 'flex' : 'none';
  const hp = document.getElementById('hunter-panel');
  if (hp) hp.style.display = isHunter ? 'flex' : 'none';
  document.getElementById('files-table').style.display     = isFiles ? '' : 'none';
  document.getElementById('links-table').style.display     = isLinks ? '' : 'none';

  if (isFiles)         { stopStatusPoll(); stopHunterPoll(); loadFiles(false, true); }
  else if (isChannels) {
    stopStatusPoll(); stopHunterPoll();
    const addCard = document.getElementById('channels-add-card');
    if (addCard) addCard.style.display = 'none';
    loadChannelsTab();
  }
  else if (isLinks)    { stopStatusPoll(); stopHunterPoll(); loadLinks(); }
  else if (isSettings) { stopStatusPoll(); stopHunterPoll(); loadCredentials(); loadSyncInterval(); _refreshUiPwState(); loadAccountsList(); }
  else if (isDownloads){ stopStatusPoll(); stopHunterPoll(); loadDownloadsList(); }
  else if (isStatus)   { stopHunterPoll(); startStatusPoll(); }
  else if (isHunter)   { stopStatusPoll(); startHunterPoll(); _magnetHuntInitOnSwitch(); requestAnimationFrame(_hgUpdateStickyTop); }

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

let _statusGroupsCache = null;
let _statusGroupsTs = 0;
async function _fetchGroupsForStatus() {
  const now = Date.now();
  if (_statusGroupsCache && now - _statusGroupsTs < 30000) return _statusGroupsCache;
  try {
    const g = await api('/api/groups');
    _statusGroupsCache = Array.isArray(g) ? g : (g.groups || []);
    _statusGroupsTs = now;
  } catch(_) {}
  return _statusGroupsCache || [];
}

async function loadStatus() {
  try {
    const d = await api('/api/status');
    const el = document.getElementById('status-panel');
    if (!el || el.style.display === 'none') return;
    const scroll = el.scrollTop;
    const groups = await _fetchGroupsForStatus();
    renderStatus(d, groups);
    // Repaint heatmap from cache (fast) or trigger first fetch
    if (_hmapData !== null) {
      _renderHeatmapCells();
    } else {
      loadActivityHeatmap(null);
    }
    el.scrollTop = scroll;
  } catch(e) { /* ignore while tab is switching */ }
}

const _TYPE_ICON  = {audio:'🎵',video:'🎬',image:'🖼',archive:'🗜',document:'📄',software:'💾',other:'📦'};
const _TYPE_COLOR = {audio:'#7c3aed',video:'#ef4444',image:'#059669',archive:'#f59e0b',document:'#2563eb',software:'#374151',other:'#9ca3af'};
const _TYPE_NAME  = {audio:'type.audio',video:'type.video',image:'type.image',archive:'type.archive',document:'type.document',software:'type.software',other:'type.other'};

// ── Activity heatmap state ────────────────────────────────────────────────────
let _hmapData    = null;  // raw rows from API, null = not yet loaded
let _hmapGroupId = null;  // currently selected group_id filter

function renderStatus(d, groups = []) {
  const el = document.getElementById('status-panel');
  el.innerHTML =
    stCards(d) +
    stFileTypes(d) +
    `<div class="st-2col">${stGroups(d)}${stPlatforms(d)}</div>` +
    stIndexStatus(groups, d.sync || {}) +
    stPgTables(d) +
    `<div class="st-2col">${stSystem(d)}${stSync(d)}</div>` +
    stActivityHeatmap(groups) +
    stLogs(d);
}

function stActivityHeatmap(groups) {
  const opts = [`<option value="">${esc(t('heatmap.allChannels'))}</option>`];
  for (const g of (groups || [])) {
    const sel = (_hmapGroupId != null && g.id === _hmapGroupId) ? ' selected' : '';
    opts.push(`<option value="${g.id}"${sel}>${esc(g.display_name || g.name || `#${g.id}`)}</option>`);
  }
  return `<div class="st-section">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
      <h4 style="margin:0;flex:1">${esc(t('heatmap.title'))}</h4>
      <select class="hunter-sel" style="font-size:.72rem" onchange="loadActivityHeatmap(this.value||null)">${opts.join('')}</select>
    </div>
    <div style="overflow-x:auto">
      <div id="act-grid" class="act-grid"></div>
    </div>
    <div id="act-peak" class="act-peak"></div>
    <div id="act-legend" class="act-legend" style="display:none">
      <span>0</span>
      <div class="act-leg-bar"></div>
      <span id="act-leg-max"></span>
    </div>
    <div style="font-size:.63rem;color:var(--text-4);margin-top:4px;text-align:right">${esc(t('heatmap.utcNote'))}</div>
  </div>`;
}

async function loadActivityHeatmap(groupId) {
  _hmapGroupId = groupId ? parseInt(groupId) : null;
  try {
    const url = _hmapGroupId ? `/api/activity/heatmap?group_id=${_hmapGroupId}` : '/api/activity/heatmap';
    _hmapData = await api(url);
    _renderHeatmapCells();
  } catch (e) { /* ignore */ }
}

function _renderHeatmapCells() {
  const grid = document.getElementById('act-grid');
  if (!grid || !_hmapData) return;

  // Build 7×24 matrix; DOW: 0=Sunday … 6=Saturday
  const matrix = Array.from({length: 7}, () => new Array(24).fill(0));
  let maxVal = 0, peakDow = 0, peakHour = 0;
  for (const r of _hmapData) {
    matrix[r.dow][r.hour] = r.cnt;
    if (r.cnt > maxVal) { maxVal = r.cnt; peakDow = r.dow; peakHour = r.hour; }
  }

  const days = [t('cal.sun'),t('cal.mon'),t('cal.tue'),t('cal.wed'),t('cal.thu'),t('cal.fri'),t('cal.sat')];
  const dowOrder = [1,2,3,4,5,6,0]; // Mon → Sun

  let html = `<div class="act-corner"></div>`;
  for (let h = 0; h < 24; h++) {
    html += `<div class="act-hlabel">${h % 6 === 0 ? h : ''}</div>`;
  }
  for (const dow of dowOrder) {
    html += `<div class="act-dlabel">${esc(days[dow])}</div>`;
    for (let h = 0; h < 24; h++) {
      const cnt = matrix[dow][h];
      const i   = maxVal > 0 ? (cnt / maxVal) : 0;
      const tip = `${days[dow]} ${String(h).padStart(2,'0')}:00 — ${cnt.toLocaleString()} ${t('heatmap.files')}`;
      html += `<div class="act-cell" style="--act-i:${i.toFixed(3)}" title="${esc(tip)}"></div>`;
    }
  }
  grid.innerHTML = html;

  const peakEl = document.getElementById('act-peak');
  if (peakEl) {
    peakEl.innerHTML = maxVal > 0
      ? t('heatmap.peak', { day: `<b>${esc(days[peakDow])}</b>`, hour: String(peakHour).padStart(2,'0'), n: maxVal.toLocaleString() })
      : esc(t('heatmap.noData'));
  }
  const legEl = document.getElementById('act-legend');
  if (legEl) legEl.style.display = maxVal > 0 ? 'flex' : 'none';
  const legMax = document.getElementById('act-leg-max');
  if (legMax) legMax.textContent = maxVal.toLocaleString();
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

function stIndexStatus(groups, sync) {
  if (!groups || !groups.length) return '';
  const currentGroup = (sync.current_group || '').toLowerCase();
  const typeKeys = ['video','audio','image','archive','document','software','torrent','other'];
  const typeColors = {video:'#ef4444',audio:'#7c3aed',image:'#059669',archive:'#f59e0b',document:'#2563eb',software:'#374151',torrent:'#0891b2',other:'#9ca3af'};

  const rows = groups.filter(g => !g.hidden).map(g => {
    const isActive = currentGroup && (
      (g.username||'').toLowerCase() === currentGroup ||
      String(g.id) === currentGroup ||
      (g.name||'').toLowerCase().includes(currentGroup)
    );
    const excluded = g.excluded;
    const fileCount = (g.file_count || 0).toLocaleString();
    const lastSync = g.last_synced_at ? fmtDate(g.last_synced_at).substring(0,16) : '—';

    // Mini type bar
    const total = typeKeys.reduce((s, k) => s + (g['type_'+k] || 0), 0);
    const bar = total > 0
      ? `<div style="display:flex;gap:1px;height:10px;border-radius:2px;overflow:hidden;min-width:80px">` +
        typeKeys.map(k => {
          const v = g['type_'+k] || 0;
          if (!v) return '';
          return `<div style="flex:${v};background:${typeColors[k]}" title="${k}:${v}"></div>`;
        }).join('') + `</div>`
      : `<span style="color:var(--text-4);font-size:.7rem">—</span>`;

    let badge;
    if (isActive)    badge = `<span style="background:#dcfce7;color:#166534;border:1px solid #86efac;padding:1px 7px;border-radius:99px;font-size:.68rem;font-weight:600"><span class="hd-spinner" style="width:8px;height:8px;border-width:1.5px"></span> Aktif</span>`;
    else if (excluded) badge = `<span style="background:var(--bg-3);color:var(--text-4);border:1px solid var(--border-2);padding:1px 7px;border-radius:99px;font-size:.68rem">Hariç</span>`;
    else if (g.last_synced_at) badge = `<span style="background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;padding:1px 7px;border-radius:99px;font-size:.68rem">Senkronize</span>`;
    else               badge = `<span style="background:#fefce8;color:#854d0e;border:1px solid #fde68a;padding:1px 7px;border-radius:99px;font-size:.68rem">Bekliyor</span>`;

    return `<tr>
      <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(g.display_name||g.name)}">${esc(g.display_name||g.name)}</td>
      <td class="r" style="font-size:.72rem">${fileCount}</td>
      <td>${bar}</td>
      <td style="font-size:.7rem;color:var(--text-3)">${esc(lastSync)}</td>
      <td>${badge}</td>
    </tr>`;
  }).join('');

  return `<div class="st-section">
    <h4>İndeksleme Durumu <button onclick="_statusGroupsTs=0;loadStatus()" style="font-size:.7rem;padding:1px 8px;border:1px solid var(--border-2);border-radius:4px;background:var(--bg-3);cursor:pointer;color:var(--text-2)">↻ Yenile</button></h4>
    <div style="overflow-x:auto">
    <table class="st-tbl">
      <tr><th>Kanal/Grup</th><th class="r">Dosya</th><th>Tür Dağılımı</th><th>Son Senkronizasyon</th><th>Durum</th></tr>
      ${rows || '<tr><td colspan="5" style="text-align:center;color:var(--text-4);padding:10px">Grup bulunamadı</td></tr>'}
    </table>
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

// ── Trend Mode ────────────────────────────────────────────────────────────
// "Most Shared Files" card view — replaces the table when active.
let _trendWindow = 'all';
let _trendActive = false;
let _trendPrevSortBy  = 'date';
let _trendPrevSortDir = 'desc';

function toggleTrendMode() {
  _trendActive = !_trendActive;
  document.getElementById('trend-mode-btn')?.classList.toggle('active', _trendActive);
  if (_trendActive) {
    _trendPrevSortBy  = S.sortBy;
    _trendPrevSortDir = S.sortDir;
    S.sortBy  = 'shares';
    S.sortDir = 'desc';
    S.offset  = 0;
  } else {
    S.sortBy  = _trendPrevSortBy;
    S.sortDir = _trendPrevSortDir;
    S.offset  = 0;
  }
  loadFiles();
}

function setTrendWindow(w) {
  _trendWindow = w;
  document.querySelectorAll('.trend-win-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.window === w));
  loadTopShared();
}

async function loadTopShared() {
  const container = document.getElementById('trend-cards');
  if (!container) return;
  container.innerHTML = `<div class="trend-loading">${esc(t('common.loadingData'))}</div>`;
  try {
    const r = await api(`/api/files/top-shared?window=${encodeURIComponent(_trendWindow)}&limit=30&min_shares=2`);
    const items = r.items || [];
    if (!items.length) {
      container.innerHTML = `<div class="trend-empty">${esc(t('trend.empty'))}</div>`;
      return;
    }
    container.innerHTML = items.map((it, i) => _renderTrendCard(it, i + 1)).join('');
  } catch (e) {
    container.innerHTML = `<div class="trend-empty">${esc(t('common.error'))} — ${esc(e.message || e)}</div>`;
  }
}

function _renderTrendCard(it, rank) {
  const sc      = it.share_count || 0;
  const sc7     = it.share_count_7d || 0;
  const sc30    = it.share_count_30d || 0;
  const fname   = cleanText(it.file_name || '') || '—';
  const ext     = (it.file_ext || '').toUpperCase();
  const extPill = ext ? `<span class="ext-badge" style="${extColor(it.file_ext||'')};font-size:.68rem">${esc(ext)}</span>` : '';
  const rising  = it.is_rising
    ? `<span class="rise-badge" title="${esc(t('table.rising'))}">↑ ${esc(t('trend.rising'))}</span>`
    : '';
  const winLbl = _trendWindow === '7d'  ? t('trend.winShare7d')
               : _trendWindow === '30d' ? t('trend.winShare30d')
               :                          t('trend.winShareAll');
  const winNum = _trendWindow === '7d' ? sc7 : _trendWindow === '30d' ? sc30 : sc;

  // Group chips (top 5)
  const groups = (it.sharing_groups || []).slice(0, 5);
  const more   = Math.max(0, (it.sharing_groups?.length || 0) - groups.length);
  const groupChips = groups
    .map(g => `<b>${esc(g.name || ('#' + g.id))}</b>`)
    .join(', ') + (more > 0 ? ` <span style="color:var(--text-4)">+${more}</span>` : '');

  // 7-day sparkline — normalize bar heights to the daily max
  const spark = _renderSpark7d(it.spark_7d || []);

  // Actions: file-name filter to see all copies, plus straight download
  const escFname = esc(fname).replace(/'/g, "&#39;");
  return `<div class="trend-card">
    <div class="trend-card-head">
      <div class="trend-rank">#${rank}</div>
      <div style="flex:1;min-width:0">
        <div class="trend-name" title="${esc(fname)}">${esc(fname)}</div>
        <div class="trend-meta-row">
          ${extPill}
          <span class="trend-size">${fmtSize(it.file_size || 0)}</span>
          <span>·</span>
          <span class="trend-share-big">
            <span class="num">🔁 ${winNum.toLocaleString()}</span>
            <span class="lbl">${esc(winLbl)}</span>
          </span>
          ${rising}
        </div>
      </div>
    </div>
    <div class="trend-meta-row">
      <span style="color:var(--text-4)">${esc(t('trend.share7'))}:</span> <b style="color:var(--text-2)">${sc7}</b>
      <span style="color:var(--text-4)">${esc(t('trend.share30'))}:</span> <b style="color:var(--text-2)">${sc30}</b>
      <span style="color:var(--text-4)">${esc(t('trend.shareAll'))}:</span> <b style="color:var(--text-2)">${sc}</b>
      <span class="trend-flex"></span>
      ${spark}
    </div>
    <div class="trend-groups"><span style="color:var(--text-4)">${esc(t('trend.groups'))}:</span> ${groupChips || '<i>—</i>'}</div>
    <div class="trend-actions">
      <button class="trend-act-btn" onclick="trendShowAllCopies('${escFname}', ${it.file_size || 0})">📂 ${esc(t('trend.allCopies'))}</button>
      <button class="trend-act-btn primary" onclick="downloadFile(${it.id})">⬇ ${esc(t('trend.download'))}</button>
    </div>
  </div>`;
}

function _renderSpark7d(days) {
  // Map the per-day counts into 7 buckets ending today. Missing days = 0.
  const buckets = new Array(7).fill(0);
  const today = new Date(); today.setHours(0,0,0,0);
  const keyFor = (offset) => {
    const d = new Date(today); d.setDate(today.getDate() - (6 - offset));
    return d.toISOString().slice(0, 10);
  };
  const idx = {};
  for (let i = 0; i < 7; i++) idx[keyFor(i)] = i;
  for (const d of (days || [])) {
    const k = (d.d || '').slice(0, 10);
    if (k in idx) buckets[idx[k]] = d.n || 0;
  }
  const max = Math.max(1, ...buckets);
  const bars = buckets.map((n, i) => {
    const h = Math.round((n / max) * 22) || (n > 0 ? 2 : 1);
    const peak = n === max && n > 0 ? ' peak' : '';
    const day = keyFor(i);
    return `<span class="bar${peak}" style="height:${h}px" title="${esc(day)}: ${n}"></span>`;
  }).join('');
  return `<span class="trend-spark" title="${esc(t('trend.sparkTitle'))}">${bars}</span>`;
}

// Filter the regular files table to all copies of a specific (file_name, file_size).
// Cleanest: drop into normal mode, set col-name + size range to the exact value.
function trendShowAllCopies(fname, size) {
  _trendActive = false;
  document.getElementById('trend-mode-btn')?.classList.remove('active');
  S.sortBy  = _trendPrevSortBy  || 'date';
  S.sortDir = _trendPrevSortDir || 'desc';
  const colName = document.getElementById('col-name');
  if (colName) colName.value = fname;
  // Tight size range so we match exactly this file, not just same-name files
  const mb = (size / (1024 * 1024)) || 0;
  const sMin = document.getElementById('col-size-min');
  const sMax = document.getElementById('col-size-max');
  if (sMin) sMin.value = (mb - 0.01).toFixed(2);
  if (sMax) sMax.value = (mb + 0.01).toFixed(2);
  // Disable dedupe so we see EVERY copy across groups
  if (typeof S === 'object') S.dedupe = false;
  S.offset = 0;
  loadFiles();
}

// Magnet'ler `files` tablosunda değil `links` tablosunda. Bu yüzden "Magnet"
// düğmesi kullanıcıyı linkler sekmesine, platform=Magnet filtresi açık olarak
// atlatır — dosya filtre çubuğunda sade ve beklendiği gibi davranır.
function jumpToMagnetLinks() {
  switchTab('links');
  setTimeout(() => {
    const sel = document.getElementById('lcol-platform');
    if (sel) {
      sel.value = 'Magnet';
      if (typeof loadLinks === 'function') loadLinks();
    }
  }, 50);
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
  // Skip when the cursor is over a dup-badge — its own mouseenter handler
  // manages #ctx-tip (async channel-list fetch) and we'd otherwise hide its
  // result on every mousemove tick.
  if (e.target.closest('.file-dup-badge')) return;
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

// İstemci tarafı önbellek: aynı sorgu parametreleri için son sonuç.
// Sekme değiştir → geri dön akışında ağ gidiş-dönüşü ve dedupe sorgusunu
// beklemeden anında boyar; ardından sessiz arkaplan tazelemesi yapar.
const _filesCache = new Map();   // key → {ts, data}
const _FILES_CACHE_TTL = 60_000; // 60 sn

const _PIPE_TYPE_MAP = {
  video:'video', vid:'video',
  ses:'audio', audio:'audio',
  resim:'image', image:'image', img:'image',
  'arşiv':'archive', arsiv:'archive', archive:'archive',
  belge:'document', document:'document', doc:'document',
  'yazılım':'software', yazilim:'software', software:'software',
  torrent:'torrent',
};

function _buildFilesParams() {
  const rawQ = document.getElementById('col-name').value.trim();
  let nameQ = rawQ, pipeGroup = '';
  if (rawQ.includes('|')) {
    const [left, right] = rawQ.split('|').map(s => s.trim());
    nameQ = left;
    pipeGroup = _PIPE_TYPE_MAP[right.toLowerCase()] || '';
  }
  const params = new URLSearchParams({
    q:         nameQ,
    ext:       S.extChip || document.getElementById('ext-input').value.trim() ||
               document.getElementById('col-ext').value.trim(),
    ext_group: pipeGroup || S.typeGroup,
    sort_by:   S.sortBy, sort_dir: S.sortDir,
    limit:     S.limit,  offset:   S.offset,
  });
  if (S.searchCaption) params.set('search_caption', '1');
  if (S.activeGroupId!=null) params.set('group_id', S.activeGroupId);
  if (S.colGroupIds && S.colGroupIds.size > 0) {
    params.set('group_ids', [...S.colGroupIds].join(','));
  }
  if (S.fileIdsFilter && S.fileIdsFilter.size > 0) {
    params.set('file_ids', [...S.fileIdsFilter].join(','));
  }
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
  // Semantic toggle: only meaningful when there's a name query. Server
  // will fall back to exact silently if the embedding subsystem isn't ready.
  if (S.searchMode === 'hybrid' && document.getElementById('col-name').value.trim()) {
    params.set('mode', 'hybrid');
  }
  return params;
}

function toggleCaptionSearch() {
  S.searchCaption = !S.searchCaption;
  localStorage.setItem('tf_search_caption', S.searchCaption ? '1' : '0');
  document.getElementById('caption-toggle')?.classList.toggle('active', S.searchCaption);
  S.offset = 0;
  loadFiles();
}

function _initCaptionToggle() {
  S.searchCaption = localStorage.getItem('tf_search_caption') === '1';
  document.getElementById('caption-toggle')?.classList.toggle('active', S.searchCaption);
}

// Semantic search toggle + boot-time availability probe.
function toggleSemanticSearch() {
  S.searchMode = (S.searchMode === 'hybrid') ? 'exact' : 'hybrid';
  localStorage.setItem('tf_search_mode', S.searchMode);
  const btn = document.getElementById('sem-toggle');
  if (btn) btn.classList.toggle('active', S.searchMode === 'hybrid');
  // Re-run search with the new mode.
  S.offset = 0;
  loadFiles();
}

async function _initSemanticToggle() {
  try {
    const r = await fetch('/api/embed/status');
    if (!r.ok) return;
    const s = await r.json();
    if (!s.available) return;
    const btn = document.getElementById('sem-toggle');
    if (!btn) return;
    btn.style.display = '';
    btn.classList.toggle('active', S.searchMode === 'hybrid');
  } catch (e) {}
}

function _paintFilesResult(data, mode = 'replace') {
  renderFiles(data.files, '', mode);
  _filesTotal = data.total || 0;
  renderFilesFooter(data);
  const fc = document.getElementById('flt-count');
  if (fc) {
    // Filtre pili alttaki "Tümü" istatistiği gibi torrent içi dosyaları da
    // dahil ederek "X benzersiz dosya · Y boyut" gösterir.
    const vt = (data.virtual_total != null ? data.virtual_total : data.total) || 0;
    const sz = data.total_size || 0;
    fc.textContent = `${t("filter.fileCount", { n: vt.toLocaleString() })} · ${fmtSize(sz)}`;
  }
  _mountFilesInfiniteScroll();
  // After an append the sentinel is below the freshly-added rows; arming the
  // observer fires immediately if more rows still fit in the viewport. Guard
  // with the loading flag so we don't blast the API in a tight loop.
  _filesLoadingMore = false;
}

let _filesTotal = 0;

async function loadFiles(silent = false, allowCache = false, mode = 'replace') {
  // Infinite-scroll grid (replaces paginated mode).
  //   mode='replace'  → fresh load from offset 0, paint skeleton, reset state
  //   mode='append'   → fetch next page starting at the currently-loaded count
  //                     and append rows to the existing tbody
  if (mode === 'append') {
    const params = _buildFilesParams();
    params.set('offset', String(_currentFiles.length));
    params.set('limit',  String(S.limit));
    try {
      const data = await api('/api/files?' + params);
      _paintFilesResult(data, 'append');
    } catch (e) {
      console.warn('loadFiles append failed', e);
    }
    return;
  }

  // Replace mode — same flow as before with cache + skeleton + parallel stats.
  const params = _buildFilesParams();
  params.set('offset', '0');
  const key = params.toString();

  if (allowCache) {
    const cached = _filesCache.get(key);
    const fresh  = cached && (Date.now() - cached.ts) < _FILES_CACHE_TTL;
    if (fresh) {
      _paintFilesResult(cached.data, 'replace');
      (async () => {
        try {
          const data = await api('/api/files?' + params);
          _filesCache.set(key, { ts: Date.now(), data });
          if (data.total !== cached.data.total ||
              (data.files || []).length !== (cached.data.files || []).length ||
              JSON.stringify((data.files || []).map(f => f.id)) !==
                JSON.stringify((cached.data.files || []).map(f => f.id))) {
            _paintFilesResult(data, 'replace');
          }
        } catch (e) {}
      })();
      return;
    }
  }

  if (!silent) _paintGridLoading('files-body', 10);

  const filesPromise = api('/api/files?' + params);
  const statsPromise = (S.sliderMax === 0) ? api('/api/stats').catch(() => null) : null;
  const nameQ = document.getElementById('col-name').value.trim();
  const torrentMatchPromise = nameQ
    ? fetch(`/api/torrents/search?q=${encodeURIComponent(nameQ)}&limit=200`)
        .then(r => r.ok ? r.json() : []).catch(() => [])
    : null;

  const data = await filesPromise;
  _filesCache.set(key, { ts: Date.now(), data });
  _paintFilesResult(data, 'replace');

  if (statsPromise) {
    statsPromise.then(stats => { if (stats) initSizeSlider(stats.max_file_size || 0); });
  }
  if (torrentMatchPromise) {
    const matches = await torrentMatchPromise;
    _autoExpandTorrentMatches(matches, nameQ);
  }
}

// Veri değişikliği olan eylemlerden sonra çağrılmak üzere — önbelleği boşalt.
function _invalidateFilesCache() { _filesCache.clear(); }

function renderFiles(files, gFilter, mode = 'replace') {
  const tbody = document.getElementById('files-body');
  if (gFilter) files = files.filter(f=>(f.group_name||'').toLowerCase().includes(gFilter));
  // Infinite scroll: in append mode we extend the existing list + tbody rather
  // than blowing it away. Row numbering picks up where the previous batch left.
  const startIdx = (mode === 'append') ? _currentFiles.length : 0;
  if (mode === 'append') {
    _currentFiles = _currentFiles.concat(files);
  } else {
    _currentFiles = files;
  }
  if (mode === 'replace' && !files.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="no-data">${esc(t("table.noFiles"))}</td></tr>`;
    return;
  }
  const rows = [];
  files.forEach((f, i) => {
    const rowNum  = startIdx + i + 1;
    const checked = S.selectedFiles.has(f.id) ? ' checked' : '';
    const ext     = (f.file_ext||'').toUpperCase();
    const color   = extColor(f.file_ext||'');
    const badge   = ext ? `<span class="ext-badge" style="${color}" onclick="filterByExt('${esc(f.file_ext)}')">${esc(ext)}</span>` : '—';
    // Strip emojis / formatting glue from anything that came out of a
    // Telegram message (channel display name, file name, message body)
    // so cells render as plain text regardless of how the source was decorated.
    const gName   = plainName(f.group_name || '');
    const fName   = cleanText(f.file_name || '') || '—';
    const ctxRaw  = cleanText(f.context || '');
    const gLink   = `<span class="g-link" onclick="filterByGroup(${f.group_id})">${esc(gName)}</span>`;
    const tg      = tgLink(f);
    const selRow  = f.id === _selectedFileId ? ' class="row-selected"' : '';
    // Same file (name + size) re-posted across multiple messages collapses
    // into one row; surface the underlying count.
    const dupBadge = (f.appearances && f.appearances > 1)
      ? `<span class="link-dup-badge file-dup-badge" data-fname="${esc(f.file_name || '')}" data-fsize="${f.file_size || 0}" data-appearances="${f.appearances}" title="${esc(t('table.appearances', { n: f.appearances }))}">×${f.appearances}</span>`
      : '';

    const isTorrent = (f.file_ext || '').toLowerCase() === 'torrent';
    const tc = isTorrent ? _torrentCache[f.id] : null;
    const toggleBtn = isTorrent
      ? `<button class="torrent-toggle${tc?.open ? ' open' : ''}" onclick="toggleTorrentTree(event,${f.id})" title="${esc(t('torrent.toggle'))}">▶</button>`
      : '';

    // Share marker: subtle text + small icon. Hot/veryhot bump weight+color only.
    const sc      = f.share_count || 1;
    const sc7     = f.share_count_7d || 0;
    const sc30    = f.share_count_30d || 0;
    const isRising = sc7 >= 2 && sc7 * 4 > sc30;
    const pillCls = sc >= 20 ? 'share-pill veryhot' : sc >= 5 ? 'share-pill hot' : 'share-pill';
    const scTip   = t('table.sharesTipDetail', {n: sc, w: sc7, m: sc30});
    const sharePill = `<span class="${pillCls}" title="${esc(scTip)}"><span class="share-icon">🔁</span><span class="share-num">${sc.toLocaleString()}</span>${isRising ? '<span class="rise-badge" title="' + esc(t('table.rising')) + '">↑</span>' : ''}</span>`;

    rows.push(`<tr${selRow} onclick="selectFileRow(event,${f.id})">
      <td class="chk-cell"><input type="checkbox" class="row-chk" data-fid="${f.id}"${checked}></td>
      <td class="num-cell">${rowNum}</td>
      <td>${badge}</td>
      <td title="${esc(fName)}"><div class="fname-cell">${toggleBtn}<span class="fname-trunc">${esc(fName)}</span>${tg}${dupBadge}</div></td>
      <td class="ctx-cell"${ctxRaw ? ` data-ctx="${esc(ctxRaw)}"` : ''} title="${esc(ctxRaw)}">${ctxRaw ? esc(ctxRaw.substring(0,50)) : '—'}</td>
      <td>${fmtSize(f.file_size)}</td>
      <td>${gLink}</td>
      <td>${fmtDate(f.date)}</td>
      <td class="col-shares">${sharePill}</td>
      <td>${dlState(f)}</td>
    </tr>`);

    if (isTorrent) {
      const open = tc?.open;
      let treeContent;
      if (!open) {
        treeContent = '';
      } else if (tc?.state === 'done') {
        treeContent = _buildTorrentPanelHtml(f.id, tc.data, tc.filter || '');
      } else if (tc?.state === 'error') {
        treeContent = `<div class="tt-error">⚠ ${esc(tc.error || t('torrent.error'))}</div>`;
      } else {
        treeContent = `<div class="tt-loading"><span class="hm-spinner"></span> ${esc(t('common.loadingData'))}</div>`;
      }
      rows.push(`<tr class="torrent-tree-row" id="torrent-tree-${f.id}"${open ? '' : ' style="display:none"'}>
        <td colspan="10"><div class="torrent-tree-panel" id="torrent-tree-panel-${f.id}">${treeContent}</div></td>
      </tr>`);
    }
  });
  if (mode === 'append') {
    tbody.insertAdjacentHTML('beforeend', rows.join(''));
  } else {
    tbody.innerHTML = rows.join('');
  }
  // Direct per-checkbox listeners — gives us a real DOM event with shiftKey.
  // We rebind on the entire tbody every time because new rows just landed and
  // don't yet have listeners; old rows are idempotent (addEventListener on the
  // same handler reference is deduped by the browser).
  tbody.querySelectorAll('.row-chk').forEach(cb => {
    cb.addEventListener('click', _fileCbClick);
  });
  // Hover the ×N dup badge → fetch + show the channel list in #ctx-tip.
  tbody.querySelectorAll('.file-dup-badge[data-fname]').forEach(el => {
    el.addEventListener('mouseenter', _dupBadgeHover);
    el.addEventListener('mouseleave', _dupBadgeHoverEnd);
    el.addEventListener('mousemove',  _dupBadgeMove);
  });
  updateBulkFileBtn();
}

// ── Dup badge tooltip (lazy channel-list fetch on hover) ────────────────────
const _DUP_CACHE = new Map();   // key = "fname|fsize" → {ts, html}
const _DUP_TTL   = 60_000;

function _dupBadgeKey(el) { return `${el.dataset.fname}|${el.dataset.fsize}`; }

async function _dupBadgeHover(ev) {
  const el = ev.currentTarget;
  const key = _dupBadgeKey(el);
  const tip = document.getElementById('ctx-tip');
  if (!tip) return;
  // Hide native title while our richer tip is up.
  if (el.title) { el.dataset.origTitle = el.title; el.title = ''; }
  let cached = _DUP_CACHE.get(key);
  if (!cached || (Date.now() - cached.ts) > _DUP_TTL) {
    tip.innerHTML = `<div class="dup-tip-head">${esc(t('table.appearancesLoading') || 'Kanallar yükleniyor…')}</div>`;
    tip.style.display = 'block';
    _dupBadgeMove(ev);
    try {
      const r = await api(`/api/files/shares?fname=${encodeURIComponent(el.dataset.fname)}&fsize=${el.dataset.fsize}`);
      cached = { ts: Date.now(), html: _renderDupTipHtml(r.shares || [], +el.dataset.appearances) };
      _DUP_CACHE.set(key, cached);
    } catch (e) {
      cached = { ts: Date.now(), html: `<div class="dup-tip-head">${esc(t('table.appearancesError') || 'Yüklenemedi')}</div>` };
    }
  }
  tip.innerHTML = cached.html;
  tip.style.display = 'block';
  _dupBadgeMove(ev);
}

function _dupBadgeHoverEnd(ev) {
  const tip = document.getElementById('ctx-tip');
  if (tip) tip.style.display = 'none';
  const el = ev.currentTarget;
  if (el.dataset.origTitle) { el.title = el.dataset.origTitle; delete el.dataset.origTitle; }
}

function _dupBadgeMove(ev) {
  const tip = document.getElementById('ctx-tip');
  if (!tip || tip.style.display === 'none') return;
  const x = ev.clientX + 14, y = ev.clientY + 14;
  const bx = tip.offsetWidth, by = tip.offsetHeight;
  tip.style.left = (x + bx > window.innerWidth  ? x - bx - 20 : x) + 'px';
  tip.style.top  = (y + by > window.innerHeight ? y - by - 20 : y) + 'px';
}

function _renderDupTipHtml(shares, total) {
  if (!shares.length) return `<div class="dup-tip-head">${esc(t('table.appearancesEmpty') || 'Kayıt bulunamadı')}</div>`;
  const head = total > shares.length
    ? `${total} kanal · ilk ${shares.length} gösteriliyor`
    : `${shares.length} kanal`;
  const rows = shares.map(s => {
    const name   = plainName(s.group_name || `#${s.group_id}`);
    const uname  = s.group_username ? `@${s.group_username}` : '';
    const dateTx = s.date ? new Date(s.date).toLocaleDateString() : '';
    return `<div class="dup-tip-row"><span class="dup-tip-name">${esc(name)}</span>${uname ? ` <span class="dup-tip-uname">${esc(uname)}</span>` : ''}${dateTx ? ` <span class="dup-tip-date">· ${esc(dateTx)}</span>` : ''}</div>`;
  }).join('');
  return `<div class="dup-tip-head">${esc(head)}</div>${rows}`;
}

function selectFileRow(e, id) {
  if (e.target.tagName === 'INPUT' || e.target.closest('a, .dl-link, .dl-done, .dl-prog, .ext-badge, .g-link, .torrent-toggle')) return;
  const was = _selectedFileId === id;
  _selectedFileId = was ? null : id;
  document.querySelectorAll('#files-body tr:not(.torrent-tree-row)').forEach(r => r.classList.remove('row-selected'));
  if (!was) e.currentTarget.classList.add('row-selected');
}

// ── Torrent tree ──────────────────────────────────────────────────────────────

const _torrentCache = {};   // file_id → {state, data, open, filter, error}
let _torrentPollTimer = null;

function _buildTorrentPanelHtml(fileId, data, filter) {
  if (!data) return `<div class="tt-loading"><span class="hm-spinner"></span> ${esc(t('common.loadingData'))}</div>`;
  if (data.error && !data.tree?.length) {
    return `<div class="tt-error">⚠ ${esc(data.error)}</div>`;
  }
  const name  = data.torrent_name || data.name || '';
  const total = data.total_size || 0;
  const count = data.file_count || (data.tree || []).length;
  return `<div class="torrent-tree-header">
    <span class="torrent-tree-name" title="${esc(name)}">📦 ${esc(name)}</span>
    <span class="torrent-tree-stats">${count.toLocaleString()} ${esc(t('torrent.files'))} · ${fmtSize(total)}</span>
    <input class="torrent-tree-filter" type="text"
      placeholder="${esc(t('torrent.filterPh'))}"
      value="${esc(filter)}"
      oninput="filterTorrentTree(${fileId},this.value)"
      onclick="event.stopPropagation()">
  </div>${_buildTorrentListHtml(data.tree || [], filter)}`;
}

const _TORRENT_DISPLAY_LIMIT = 1000;

function _buildTorrentListHtml(tree, filter) {
  const term = (filter || '').toLowerCase().trim();
  const filtered = term ? tree.filter(f => f.path.toLowerCase().includes(term)) : tree;
  if (!filtered.length) {
    return `<div class="torrent-tree-list"><div class="tt-empty">${esc(t('torrent.noMatch'))}</div></div>`;
  }
  const shown = filtered.length > _TORRENT_DISPLAY_LIMIT ? filtered.slice(0, _TORRENT_DISPLAY_LIMIT) : filtered;
  const hiddenCount = filtered.length - shown.length;
  const dirsSeen = new Set();
  const rows = [];
  const _guides = (d) => {
    let g = '';
    for (let i = 0; i < d; i++) g += '<span class="tt-guide"></span>';
    return g;
  };
  for (const f of shown) {
    const parts = f.path.replace(/\\/g, '/').split('/');
    const depth = parts.length - 1;
    for (let d = 1; d < parts.length; d++) {
      const dirKey = parts.slice(0, d).join('/');
      if (!dirsSeen.has(dirKey)) {
        dirsSeen.add(dirKey);
        rows.push(`<div class="tt-entry tt-dir" data-depth="${d - 1}">
          ${_guides(d - 1)}<span class="tt-icon">📁</span>
          <span class="tt-path">${esc(parts[d - 1])}</span>
        </div>`);
      }
    }
    const fileName = parts[parts.length - 1];
    rows.push(`<div class="tt-entry${term ? ' tt-match' : ''}" data-depth="${depth}">
      ${_guides(depth)}<span class="tt-icon">📄</span>
      <span class="tt-path" title="${esc(f.path)}">${esc(fileName)}</span>
      <span class="tt-size">${fmtSize(f.size)}</span>
    </div>`);
  }
  if (hiddenCount > 0) {
    rows.push(`<div class="tt-more-notice">+${hiddenCount.toLocaleString()} ${esc(t('torrent.moreFiles'))}</div>`);
  }
  return `<div class="torrent-tree-list">${rows.join('')}</div>`;
}

async function toggleTorrentTree(event, fileId) {
  event.stopPropagation();
  const row = document.getElementById(`torrent-tree-${fileId}`);
  const btn = event.currentTarget;
  if (!row) return;

  const wasOpen = row.style.display !== 'none';
  if (wasOpen) {
    row.style.display = 'none';
    btn.classList.remove('open');
    if (_torrentCache[fileId]) _torrentCache[fileId].open = false;
    return;
  }

  row.style.display = '';
  btn.classList.add('open');
  if (!_torrentCache[fileId]) _torrentCache[fileId] = {};
  _torrentCache[fileId].open = true;

  if (_torrentCache[fileId].state === 'done') {
    _refreshTorrentPanel(fileId);
    return;
  }

  _torrentCache[fileId].state = 'loading';
  const panel = document.getElementById(`torrent-tree-panel-${fileId}`);
  if (panel) panel.innerHTML = `<div class="tt-loading"><span class="hm-spinner"></span> ${esc(t('common.loadingData'))}</div>`;

  try {
    const res = await fetch(`/api/files/${fileId}/torrent-tree`);
    if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
    const data = await res.json();
    _torrentCache[fileId] = { state: 'done', data, open: true, filter: '' };
    _refreshTorrentPanel(fileId);
  } catch (err) {
    _torrentCache[fileId] = { state: 'error', error: String(err), open: true };
    const panel2 = document.getElementById(`torrent-tree-panel-${fileId}`);
    if (panel2) panel2.innerHTML = `<div class="tt-error">⚠ ${esc(String(err))}</div>`;
  }
}

function _refreshTorrentPanel(fileId) {
  const panel = document.getElementById(`torrent-tree-panel-${fileId}`);
  if (!panel) return;
  const c = _torrentCache[fileId];
  if (!c || c.state !== 'done') return;
  panel.innerHTML = _buildTorrentPanelHtml(fileId, c.data, c.filter || '');
}

function filterTorrentTree(fileId, term) {
  const c = _torrentCache[fileId];
  if (!c || c.state !== 'done') return;
  c.filter = term;
  const panel = document.getElementById(`torrent-tree-panel-${fileId}`);
  if (!panel) return;
  const listEl = panel.querySelector('.torrent-tree-list');
  if (listEl) listEl.outerHTML = _buildTorrentListHtml(c.data?.tree || [], term);
}

// Auto-expand torrent rows in the current page that matched via content search.
// `matches` comes from /api/torrents/search; `term` is the active name filter.
function _autoExpandTorrentMatches(matches, term) {
  // User preference: don't auto-open the tree even when a search match lives
  // inside a parsed torrent. We still:
  //   • Pre-fetch the tree data in the background, so the first manual click
  //     renders instantly (no API round-trip wait).
  //   • Stash the active filter term so the panel is pre-filtered when opened.
  //   • Mark the toggle button with a "has match" hint class so it's
  //     discoverable.
  if (!matches || !matches.length) return;
  const matchMap = new Map(matches.map(m => [m.torrent_file_id, m]));

  for (const [fileId, match] of matchMap) {
    const row = document.getElementById(`torrent-tree-${fileId}`);
    if (!row) continue;  // not on this page

    const btn = document.querySelector(`.torrent-toggle[onclick*="toggleTorrentTree(event,${fileId})"]`);
    if (btn) btn.classList.add('has-match');

    // If already cached from a previous load, just update the filter term so
    // a subsequent click renders with the current search highlighted.
    if (_torrentCache[fileId]?.state === 'done') {
      _torrentCache[fileId].filter = term;
      // Keep `open` state untouched — respects user's prior manual choice.
      continue;
    }

    // Background pre-fetch (no DOM mutation, panel stays closed).
    _torrentCache[fileId] = {
      state:  'loading',
      open:   false,
      filter: term,
      // Preview from matched_paths in case the fetch fails — used the moment
      // user opens the tree.
      preview: {
        torrent_name: match.torrent_name || match.file_name || '',
        total_size:   match.content_size || 0,
        file_count:   (match.matched_paths || []).length,
        tree:         match.matched_paths || [],
      },
    };

    fetch(`/api/files/${fileId}/torrent-tree`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const prev = _torrentCache[fileId] || {};
        _torrentCache[fileId] = { state: 'done', data, open: prev.open || false, filter: term };
      })
      .catch(() => {
        const cur = _torrentCache[fileId];
        if (cur?.state === 'loading') {
          _torrentCache[fileId] = {
            state:  'done',
            data:   cur.preview,
            open:   false,
            filter: term,
          };
        }
      });
  }
}

// ── Torrent parse controls ────────────────────────────────────────────────────

let _torrentCtrlVisible = false;

function torrentCtrlToggle() {
  _torrentCtrlVisible ? torrentCtrlClose() : torrentCtrlOpen();
}

async function torrentCtrlOpen() {
  _torrentCtrlVisible = true;
  await _torrentCtrlRefresh();
}

function torrentCtrlClose() {
  _torrentCtrlVisible = false;
}

async function _torrentCtrlRefresh() {
  try {
    const res  = await fetch('/api/torrents/status');
    const data = await res.json();
    const area = document.getElementById('tpc-stats-area');
    if (!area) return;
    const counts  = data.counts || {};
    const worker  = data.worker  || {};
    area.innerHTML = `
      <div class="tpc-stat-row"><span data-i18n="torrent.statTotal">${esc(t('torrent.statTotal'))}</span><b>${(counts.total||0).toLocaleString()}</b></div>
      <div class="tpc-stat-row"><span data-i18n="torrent.statParsed">${esc(t('torrent.statParsed'))}</span><b>${(counts.parsed||0).toLocaleString()}</b></div>
      <div class="tpc-stat-row"><span data-i18n="torrent.statPending">${esc(t('torrent.statPending'))}</span><b>${(counts.pending||0).toLocaleString()}</b></div>
      ${counts.errors ? `<div class="tpc-stat-row"><span>${esc(t('torrent.statErrors'))}</span><b style="color:var(--danger)">${counts.errors.toLocaleString()}</b></div>` : ''}`;
    const startBtn = document.getElementById('tpc-start-btn');
    if (startBtn) startBtn.disabled = worker.running || counts.pending === 0;
  } catch (_) {}
}

async function startTorrentParse() {
  const conc = parseInt(document.getElementById('tpc-concurrency')?.value || '5', 10);
  try {
    await fetch('/api/torrents/parse-all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ concurrency: conc }),
    });
    _torrentPollStart();
  } catch (err) {
    showToast(String(err), 3000);
  }
}

async function cancelTorrentParse() {
  try {
    await fetch('/api/torrents/cancel', { method: 'POST' });
  } catch (_) {}
}

function _torrentPollStart() {
  if (_torrentPollTimer) return;
  _torrentPollTimer = setInterval(_torrentPollTick, 1200);
  _torrentPollTick();
}

function _torrentPollStop() {
  if (_torrentPollTimer) { clearInterval(_torrentPollTimer); _torrentPollTimer = null; }
  const bar = document.getElementById('torrent-parse-bar');
  if (bar) bar.style.display = 'none';
}

async function _torrentPollTick() {
  try {
    const res  = await fetch('/api/torrents/status');
    const data = await res.json();
    const w    = data.worker || {};
    if (!w.running && !_torrentPollTimer) return;
    const bar  = document.getElementById('torrent-parse-bar');
    const txt  = document.getElementById('tpb-text');
    const fill = document.getElementById('tpb-fill');
    const pct  = document.getElementById('tpb-pct');
    if (!bar) return;

    const startBtn  = document.getElementById('tpc-start-btn');
    const cancelBtn = document.getElementById('tpc-cancel-btn');
    if (w.running) {
      bar.style.display = 'flex';
      if (startBtn)  startBtn.disabled = true;
      if (cancelBtn) cancelBtn.style.display = '';
      const done  = w.done  || 0;
      const total = w.total || 0;
      const errs  = w.errors || 0;
      const ratio = total > 0 ? done / total : 0;
      if (txt)  txt.textContent  = t('torrent.parseProgress', { done, total, errors: errs });
      if (fill) fill.style.width = `${Math.round(ratio * 100)}%`;
      if (pct)  pct.textContent  = total > 0 ? `${Math.round(ratio * 100)}%` : '';
    } else {
      const done = w.done  || 0;
      const errs = w.errors || 0;
      if (startBtn)  startBtn.disabled = false;
      if (cancelBtn) cancelBtn.style.display = 'none';
      if (txt)  txt.textContent  = t('torrent.parseDone', { done, errors: errs });
      if (fill) fill.style.width = '100%';
      if (pct)  pct.textContent  = '100%';
      setTimeout(_torrentPollStop, 4000);
      _torrentPollTimer && clearInterval(_torrentPollTimer);
      _torrentPollTimer = null;
      await _torrentCtrlRefresh();
    }
  } catch (_) {}
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
  // Always show the modal so the forward-to-Saved-Messages toggle is reachable
  // even when there are no transfer destinations configured.
  const result = await _showDlDestModal(dests, fileIds);
  if (result === null) return; // cancelled
  const { destIds, scheduledAt, forwardSelf } = result;
  if (forwardSelf) {
    await _forwardFilesToSelf(fileIds);
  }
  // If the user picked ONLY "forward to me" (no destinations, no later
  // schedule), skip downloading to disk entirely — forward is free.
  const shouldDownload = destIds.length > 0 || scheduledAt || !forwardSelf;
  if (shouldDownload) {
    for (const fileId of fileIds) {
      await _doDownload(fileId, destIds, scheduledAt);
    }
  }
  S.selectedFiles.clear();
  updateBulkFileBtn();
  document.querySelectorAll('.row-chk').forEach(c => c.checked=false);
}

async function _forwardFilesToSelf(fileIds) {
  let ok = 0, failed = 0;
  for (const fid of fileIds) {
    try {
      await api(`/api/files/${fid}/forward-to-me`, { method: 'POST' });
      ok++;
    } catch (e) {
      failed++;
    }
  }
  if (ok)     showToast(t('ddm.forwardOk',   { n: ok })     || `${ok} dosya Saved Messages'a yönlendirildi.`, 3500);
  if (failed) showToast(t('ddm.forwardFail', { n: failed }) || `${failed} dosya yönlendirilemedi.`, 4500);
}

// ── Download state ────────────────────────────────────────────────────────────
function dlState(f) {
  if (f.local_path)  return `<span class="dl-done">${esc(t("table.dlDone"))}</span>`;
  if (f.downloading) return `<span class="dl-prog">${Math.round(f.download_progress*100)}%</span>`;
  if (S.dlQueue[f.id]!==undefined) return `<span class="dl-prog">${S.dlQueue[f.id]}%</span>`;
  return `<span class="dl-link" onclick="triggerDownload(${f.id})">${esc(t("table.dlLink"))}</span>`;
}

async function _doDownload(fileId, destinationIds, scheduledAt = null) {
  const body = { destination_ids: destinationIds || [] };
  if (scheduledAt) body.scheduled_at = scheduledAt;
  const r = await api(`/api/files/${fileId}/download`, {
    method: 'POST',
    json: body,
  });
  if (r.status === 'already_downloaded') { loadFiles(); return; }
  if (r.status === 'transfer_started') {
    showToast(t('dl.transferStarted'));
    loadFiles();
    return;
  }
  if (r.status === 'scheduled') {
    showToast(t('ddm.scheduledToast'));
    if (S.activeTab === 'downloads') loadDownloadsList();
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
  // Always show the modal so the forward-to-self toggle stays reachable
  // even when no transfer destinations exist.
  const result = await _showDlDestModal(dests, [fileId]);
  if (result === null) return; // cancelled
  const { destIds, scheduledAt, forwardSelf } = result;
  if (forwardSelf) await _forwardFilesToSelf([fileId]);
  const shouldDownload = destIds.length > 0 || scheduledAt || !forwardSelf;
  if (shouldDownload) {
    await _doDownload(fileId, destIds, scheduledAt);
  }
}

async function _getEnabledDests() {
  try {
    const dests = await api('/api/transfer-destinations');
    return (dests || []).filter(d => d.enabled);
  } catch { return []; }
}

function _ddmLoadPrefs() {
  try { return JSON.parse(localStorage.getItem('dl_dest_prefs') || 'null'); } catch { return null; }
}

function _ddmSavePrefs(checkedIds, forwardSelf, when) {
  try { localStorage.setItem('dl_dest_prefs', JSON.stringify({ checked_ids: checkedIds, forward_self: !!forwardSelf, when: when || 'now' })); } catch {}
}

function _showDlDestModal(dests, fileIds) {
  return new Promise(resolve => {
    _dlDestPending = { fileIds, resolve };
    const wrap = document.getElementById('ddm-options');
    wrap.innerHTML = '';
    const prefs = _ddmLoadPrefs();
    dests.forEach(d => {
      const desc = _destPathLabel(d);
      const wasChecked = prefs ? prefs.checked_ids.includes(d.id) : true;
      const row = document.createElement('div');
      row.className = 'ddm-option' + (wasChecked ? ' selected' : '');
      row.dataset.id = d.id;
      row.innerHTML = `
        <input type="checkbox" ${wasChecked ? 'checked' : ''} onchange="this.closest('.ddm-option').classList.toggle('selected',this.checked)">
        <span class="td-badge ${d.type}">${_typeLabelShort(d.type)}</span>
        <span class="ddm-opt-name">${esc(d.name)}</span>
        <span class="ddm-opt-path">${esc(desc)}</span>`;
      wrap.appendChild(row);
    });
    // Restore schedule section from prefs (default: now)
    const savedWhen = prefs?.when || 'now';
    const nowRadio = document.querySelector('input[name="ddm-when"][value="now"]');
    const laterRadio2 = document.querySelector('input[name="ddm-when"][value="later"]');
    if (nowRadio) nowRadio.checked = savedWhen !== 'later';
    if (laterRadio2) laterRadio2.checked = savedWhen === 'later';
    const form = document.getElementById('ddm-schedule-form');
    if (form) form.style.display = savedWhen === 'later' ? '' : 'none';
    const dtInput = document.getElementById('ddm-schedule-at');
    if (dtInput) dtInput.value = '';
    // Restore forward-to-self from last session
    const fwd = document.getElementById('ddm-forward-self');
    if (fwd) fwd.checked = prefs ? !!prefs.forward_self : false;
    ddmLoadPresets();
    document.getElementById('dl-dest-overlay').classList.add('open');
  });
}

function ddmToggleSchedule() {
  const later = document.querySelector('input[name="ddm-when"][value="later"]');
  const form  = document.getElementById('ddm-schedule-form');
  if (!form) return;
  form.style.display = later && later.checked ? '' : 'none';
  if (later && later.checked) {
    const dtInput = document.getElementById('ddm-schedule-at');
    if (dtInput && !dtInput.value) {
      // Default to 1 hour from now
      const d = new Date(Date.now() + 3600000);
      d.setSeconds(0, 0);
      dtInput.value = d.toISOString().slice(0, 16);
    }
  }
}

async function ddmLoadPresets() {
  const container = document.getElementById('ddm-sched-presets');
  if (!container) return;
  container.innerHTML = '';
  try {
    const schedules = await api('/api/bandwidth/schedules');
    if (!schedules || !schedules.length) return;
    const label = document.createElement('span');
    label.className = 'ddm-sched-label';
    label.setAttribute('data-i18n', 'ddm.presets');
    label.textContent = t('ddm.presets');
    container.appendChild(label);
    schedules.filter(s => s.enabled).forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'ddm-sched-preset';
      btn.textContent = s.name + ' (' + s.start_time + ')';
      btn.onclick = () => {
        const laterRadio = document.querySelector('input[name="ddm-when"][value="later"]');
        if (laterRadio) { laterRadio.checked = true; ddmToggleSchedule(); }
        const dt = _nextOccurrenceOf(s);
        const dtInput = document.getElementById('ddm-schedule-at');
        if (dtInput && dt) dtInput.value = dt.toISOString().slice(0, 16);
      };
      container.appendChild(btn);
    });
  } catch (_) {}
}

function _nextOccurrenceOf(schedule) {
  const now = new Date();
  const [sh, sm] = (schedule.start_time || '02:00').split(':').map(Number);
  for (let dayOffset = 0; dayOffset < 8; dayOffset++) {
    const d = new Date(now);
    d.setDate(d.getDate() + dayOffset);
    d.setHours(sh, sm, 0, 0);
    if (d <= now) continue;
    if (schedule.rule_type === 'weekly') {
      const jsDay = d.getDay(); // 0=Sun
      const pyDay = (jsDay + 6) % 7; // 0=Mon
      if ((schedule.days || []).includes(pyDay)) return d;
    } else if (schedule.rule_type === 'specific_date') {
      const dateStr = d.toISOString().slice(0, 10);
      if (schedule.specific_date === dateStr) return d;
    }
  }
  // Fallback: tonight at start_time
  const d = new Date(now);
  d.setHours(sh, sm, 0, 0);
  if (d <= now) d.setDate(d.getDate() + 1);
  return d;
}

function closeDlDestModal() {
  document.getElementById('dl-dest-overlay').classList.remove('open');
  if (_dlDestPending) { _dlDestPending.resolve(null); _dlDestPending = null; }
}

function confirmDlDestModal() {
  document.getElementById('dl-dest-overlay').classList.remove('open');
  if (!_dlDestPending) return;
  const destIds = [];
  document.querySelectorAll('#ddm-options .ddm-option').forEach(row => {
    if (row.querySelector('input').checked) destIds.push(parseInt(row.dataset.id, 10));
  });
  let scheduledAt = null;
  const laterRadio = document.querySelector('input[name="ddm-when"][value="later"]');
  if (laterRadio && laterRadio.checked) {
    const dtVal = (document.getElementById('ddm-schedule-at') || {}).value;
    if (dtVal) scheduledAt = new Date(dtVal).toISOString();
  }
  const forwardSelf = !!document.getElementById('ddm-forward-self')?.checked;
  const whenVal = laterRadio?.checked ? 'later' : 'now';
  _ddmSavePrefs(destIds, forwardSelf, whenVal);
  const pending = _dlDestPending;
  _dlDestPending = null;
  pending.resolve({ destIds, scheduledAt, forwardSelf });
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

let _scheduledDownloads = [];

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
      api('/api/downloads/scheduled').then(sched => {
        _scheduledDownloads = sched || [];
      }).catch(() => { _scheduledDownloads = []; }),
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

  const scheduledRows = (_scheduledDownloads || []).map(s => ({
    id: s.file_id,
    name: s.file_name || '',
    size: s.file_size || 0,
    group: s.group_name || '',
    pct: 0,
    status: 'scheduled',
    downloaded_at: null,
    queued_at: s.queued_at,
  }));

  const completedRows = _serverDownloads.map(d => ({
    id: d.id,
    name: d.file_name || t('common.fileId', { n: d.id }),
    size: d.file_size || 0,
    group: d.group_name || '',
    pct: 100,
    status: 'done',
    downloaded_at: d.downloaded_at || null,
    local_path: d.local_path || null,
  }));

  let all = [...inFlight, ...scheduledRows, ...completedRows];

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
  const _active    = all.filter(e => e.status === 'queued' || e.status === 'downloading');
  const _scheduled = all.filter(e => e.status === 'scheduled');
  const _done      = all.filter(e => e.status === 'done');
  all = _active.concat(_scheduled).concat(_done);
  _updateDlSortArrows();

  const notice = document.getElementById('dl-space-notice');
  const completedSize = _serverDownloads.reduce((s, d) => s + (d.file_size || 0), 0);
  if (notice) notice.style.display = completedSize > 0 ? 'inline-flex' : 'none';

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
    let stCls, stTxt;
    if (e.status === 'done')        { stCls = 'dl-st-done';   stTxt = t('downloads.completed'); }
    else if (e.status === 'downloading') { stCls = 'dl-st-active'; stTxt = t('downloads.downloading'); }
    else if (e.status === 'scheduled') { stCls = 'dl-scheduled-badge'; stTxt = '⏱ ' + t('downloads.scheduled'); }
    else                            { stCls = 'dl-st-queued'; stTxt = t('downloads.queued'); }
    const progHtml = (e.status !== 'done' && e.status !== 'scheduled')
      ? `<div style="display:flex;align-items:center;gap:6px">
           <div class="dl-bar" style="flex:1;width:auto"><div class="dl-bar-fill" style="width:${e.pct}%"></div></div>
           <span style="font-size:.71rem;color:var(--text-3);width:32px;text-align:right">${e.pct}%</span>
         </div>`
      : '—';
    const isDone = e.status === 'done';
    const isScheduled = e.status === 'scheduled';
    const checked = S.selectedDownloads.has(e.id) ? ' checked' : '';
    const chkCell = `<input type="checkbox" class="dl-row-chk" data-status="${e.status}"${checked} onchange="toggleDownloadSelect(${e.id},this.checked)">`;
    const actions = isDone
      ? `<button class="dl-act dl-act-dl" onclick="downloadBlob(${e.id})" title="${esc(t('dl.downloadTitle'))}">⬇</button>
         <button class="dl-act dl-act-del" onclick="deleteLocalFile(${e.id})" title="${esc(t('dl.deleteTitle'))}">🗑</button>`
      : isScheduled
      ? `<button class="dl-act dl-act-del" onclick="cancelScheduledDownload(${e.id})" title="${esc(t('dl.cancelTitle'))}">✕</button>`
      : `<button class="dl-act dl-act-del" onclick="cancelDownload(${e.id})" title="${esc(t('dl.cancelTitle'))}">✕</button>`;
    const fnameTooltip = e.local_path ? esc(e.local_path) : esc(e.name||'');
    const fnameHtml = e.local_path
      ? `<span class="dl-fname-wrap" data-path="${esc(e.local_path)}">${esc(e.name || t('common.fileId', { n: e.id }))}<span class="dl-path-tip">${esc(e.local_path)}</span></span>`
      : esc(e.name || t('common.fileId', { n: e.id }));
    return `<tr>
      <td class="chk-cell">${chkCell}</td>
      <td title="${fnameTooltip}">${fnameHtml}</td>
      <td>${fmtSize(e.size)}</td>
      <td>${esc(plainName(e.group || ''))}</td>
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

async function deleteAllLocalFiles() {
  const all = _serverDownloads.map(d => d.id);
  if (!all.length) return;
  if (!confirm(t('dl.deleteAllConfirm', { n: all.length }))) return;
  let failed = 0;
  for (const id of all) {
    try { await api(`/api/files/${id}/local`, { method: 'DELETE' }); }
    catch (_) { failed++; }
  }
  await loadDownloadsList();
  loadFiles();
  if (failed) showToast(t('dl.deletedSome', { ok: all.length - failed, fail: failed }), 4000);
  else showToast(t('dl.deletedAll', { n: all.length }), 2500);
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

// ── Magnet backfill ───────────────────────────────────────────────────────────
let _backfillPollTimer = null;

async function startMagnetBackfill() {
  const btn = document.getElementById('magnet-backfill-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/links/backfill-magnets', { method: 'POST' });
    if (r.status === 409) {
      // already running — just start polling
    } else if (!r.ok) {
      if (btn) btn.disabled = false;
      return;
    }
    _pollMagnetBackfill();
  } catch {
    if (btn) btn.disabled = false;
  }
}

function _pollMagnetBackfill() {
  clearTimeout(_backfillPollTimer);
  _backfillPollTimer = setTimeout(async () => {
    try {
      const r = await fetch('/api/links/backfill-magnets/status');
      if (!r.ok) return;
      const s = await r.json();
      _updateBackfillBar(s);
      if (s.running) _pollMagnetBackfill();
      else if (s.done_groups > 0 || s.new_magnets > 0) loadLinks(true);
    } catch { /* ignore */ }
  }, 1500);
}

function _updateBackfillBar(s) {
  const txt = document.getElementById('magnet-backfill-status');
  const btn = document.getElementById('magnet-backfill-btn');
  if (!txt) return;
  if (s.running) {
    if (btn) btn.disabled = true;
    let msg;
    if (s.enrich_phase) {
      // Phase 2: metadata fetch via aria2c/DHT
      const cur = s.current_magnet ? ` — ${s.current_magnet}` : '';
      msg = t('backfill.enrichProgress', {
        done: s.enrich_done || 0,
        total: s.enrich_total || 0,
        ok: s.enrich_success || 0,
      }) + cur;
    } else {
      // Phase 1: scanning Telegram groups for magnet messages
      const grp = s.current_group ? ` — ${s.current_group}` : '';
      msg = t('backfill.progress', {
        done: s.done_groups, total: s.total_groups, found: s.new_magnets,
      }) + grp;
    }
    txt.textContent = msg;
    txt.className = 'backfill-progress';
  } else if (s.error) {
    if (btn) btn.disabled = false;
    txt.textContent = t('backfill.error', { msg: s.error });
    txt.className = 'backfill-progress';
  } else if (s.done_groups > 0 || s.new_magnets > 0 || s.enrich_done > 0) {
    if (btn) btn.disabled = false;
    txt.textContent = t('backfill.done', { found: s.new_magnets })
      + (s.enrich_done ? ' · ' + t('backfill.enrichDone', {ok: s.enrich_success || 0, total: s.enrich_done}) : '');
    txt.className = 'backfill-progress backfill-done';
  } else {
    txt.textContent = '';
    if (btn) btn.disabled = false;
  }
}

async function _initBackfillBar() {
  try {
    const r = await fetch('/api/links/backfill-magnets/status');
    if (!r.ok) return;
    const s = await r.json();
    _updateBackfillBar(s);
    if (s.running) _pollMagnetBackfill();
  } catch { /* ignore */ }
}

// ── Links ─────────────────────────────────────────────────────────────────────
let _debounceLinksTimer;
function debouncedLoadLinks() {
  clearTimeout(_debounceLinksTimer);
  _debounceLinksTimer = setTimeout(loadLinks, 280);
}

async function loadLinks(silent = false, mode = 'replace') {
  const v  = (id) => (document.getElementById(id)?.value || '').trim();
  const offset = (mode === 'append') ? _currentLinks.length : 0;
  const p  = new URLSearchParams({
    q:        v('link-search'),
    platform: v('lcol-platform'),
    sort_by:  S.linkSortBy,
    sort_dir: S.linkSortDir,
    limit:    S.linkLimit,
    offset:   offset,
  });
  if (S.activeGroupId != null) p.set('group_id', S.activeGroupId);
  // Per-column filters (sent only when non-empty so the API treats them as absent)
  const urlF  = v('lcol-url');         if (urlF)  p.set('url_filter', urlF);
  const ctxF  = v('lcol-context');     if (ctxF)  p.set('context_filter', ctxF);
  const grpF  = v('lcol-group');       if (grpF)  p.set('group_filter', grpF);
  const fnameF = v('lcol-files-name'); if (fnameF) p.set('file_name_filter', fnameF);
  const dfrom = v('lcol-date-from');   if (dfrom) p.set('date_from', dfrom);
  const dto   = v('lcol-date-to');     if (dto)   p.set('date_to',   dto);

  if (mode === 'replace' && !silent) _paintGridLoading('links-body', 8);
  try {
    const data = await api('/api/links?' + p);
    renderLinks(data.links, mode);
    _linksTotal = data.total || 0;
    const lc = document.getElementById('link-flt-count');
    if (lc) lc.textContent = t("filter.linkCount", {n: (data.total || 0).toLocaleString()});
    _updateLinkSortArrows();
    renderLinksFooter(data);
    _mountLinksInfiniteScroll();
  } finally {
    _linksLoadingMore = false;
  }
}

let _linksTotal = 0;

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
  // Magnet URIs: preserve as-is. Their `tr=` parameters embed http(s)://
  // tracker URLs (often mangled by Twitter's t.co shortener); the http-finder
  // below would otherwise promote one of those trackers to the canonical URL,
  // making the grid render a magnet as if it were a t.co link and breaking
  // magnet expansion / file-list paths.
  if (/^magnet:/i.test(String(raw))) return String(raw).trim();
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

async function retryDeadMagnets() {
  const btn = document.getElementById('link-retry-dead-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳…'; }
  try {
    const r = await api('/api/links/retry-dead-magnets', { method: 'POST' });
    showToast(t('links.retryDeadOk', { n: r.cleared }) ||
              `${r.cleared} ölü magnet sıfırlandı. Sonraki magnet backfill turunda yeniden denenirler.`,
              5000);
  } catch (e) {
    showToast((t('links.retryDeadFail') || 'Toplu sıfırlama başarısız') + ': ' + e.message, 4000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = `<span data-i18n="links.retryDeadBtn">↻ ${esc(t('links.retryDeadBtn') || 'Ölü magnetleri yeniden dene')}</span>`; }
  }
}

// ── Magnet file tree (inline expand in links grid) ────────────────────────────

function toggleMagnetTree(lid, btn) {
  const row   = document.getElementById(`magnet-tree-${lid}`);
  const panel = document.getElementById(`magnet-tree-panel-${lid}`);
  if (!row || !panel) return;
  const wasOpen = row.style.display !== 'none';
  if (wasOpen) {
    row.style.display = 'none';
    if (btn) btn.classList.remove('open');
    return;
  }
  row.style.display = '';
  if (btn) btn.classList.add('open');
  const link = _currentLinks.find(l => l.id === lid);
  if (!link) return;
  let files = link.files_json;
  if (typeof files === 'string') { try { files = JSON.parse(files); } catch(e) { files = []; } }
  // Re-render every time so the highlight from the current file-name filter
  // stays in sync. (Previously cached innerHTML froze a stale highlight.)
  const hl = (document.getElementById('lcol-files-name')?.value || '').trim();
  panel.innerHTML = _buildMagnetPanelHtml(link, Array.isArray(files) ? files : [], hl);
}

// After renderLinks, auto-open the magnet trees whose file list contains the
// active file-name filter so the user sees what they're searching for. Same
// pattern as the Files-tab torrent auto-expand.
function _autoExpandMagnetMatches() {
  const term = (document.getElementById('lcol-files-name')?.value || '').trim().toLowerCase();
  if (!term) return;
  for (const l of _currentLinks) {
    if (!/^magnet:/i.test(l.url || '')) continue;
    let files = l.files_json;
    if (typeof files === 'string') { try { files = JSON.parse(files); } catch (e) { files = []; } }
    if (!Array.isArray(files) || !files.length) continue;
    if (!files.some(f => (f.name || '').toLowerCase().includes(term))) continue;
    const row   = document.getElementById(`magnet-tree-${l.id}`);
    const panel = document.getElementById(`magnet-tree-panel-${l.id}`);
    const btn   = document.querySelector(`.magnet-toggle[data-mlid="${l.id}"]`);
    if (!row || !panel) continue;
    row.style.display = '';
    if (btn) btn.classList.add('open');
    panel.innerHTML = _buildMagnetPanelHtml(l, files, term);
  }
}

function _buildMagnetPanelHtml(link, files, highlight) {
  const totalSz = +(link.file_size_total || 0);
  const hm = (link.url || '').match(/xt=urn:btih:([a-zA-Z0-9]+)/i);
  const infohash = hm ? hm[1].toUpperCase() : '';
  const name = (files[0] && cleanText(files[0].name)) || infohash || 'Magnet';
  const countStr = files.length === 1
    ? `1 ${t('torrent.files')}`
    : `${files.length.toLocaleString()} ${t('torrent.files')}`;
  const sizeStr = totalSz > 0 ? ` · ${fmtSize(totalSz)}` : '';
  const hl = (highlight || '').trim().toLowerCase();
  // Build a regex once so the per-row highlighter doesn't recompile.
  let hlRe = null;
  if (hl) {
    const safe = hl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    hlRe = new RegExp('(' + safe + ')', 'ig');
  }
  // Put matching files first so the user sees what they searched for without
  // scrolling. Order is stable inside each bucket.
  const matched = [];
  const rest    = [];
  for (const f of files) {
    if (hl && (f.name || '').toLowerCase().includes(hl)) matched.push(f);
    else rest.push(f);
  }
  const ordered = [...matched, ...rest];
  let rows = '';
  for (const f of ordered) {
    const nm  = cleanText(f.name || '');
    const sz  = f.size ? fmtSize(f.size) : (files.length === 1 && totalSz ? fmtSize(totalSz) : '');
    let nmHtml = esc(nm);
    if (hlRe) nmHtml = nmHtml.replace(hlRe, '<mark>$1</mark>');
    rows += `<div class="tt-entry${hl && nm.toLowerCase().includes(hl) ? ' tt-entry-match' : ''}" style="padding-left:8px">
      <span class="tt-icon">📄</span>
      <span class="tt-path" title="${esc(nm)}">${nmHtml}</span>
      ${sz ? `<span class="tt-size">${sz}</span>` : ''}
    </div>`;
  }
  return `<div class="torrent-tree-header">
    <span class="torrent-tree-name" title="${esc(infohash)}">🧲 ${esc(name)}</span>
    <span class="torrent-tree-stats">${countStr}${sizeStr}</span>
    ${infohash ? `<span class="magnet-infohash" title="${esc(t('magnet.infohash'))}">📋 ${esc(infohash.substring(0, 16))}…</span>` : ''}
  </div><div class="torrent-tree-list">${rows}</div>`;
}

function _linkFilesCell(l) {
  // Probe states:
  //   probed_at IS NULL              → not yet visited (queued)
  //   available IS NOT NULL && false → confirmed dead (filtered out by API)
  //   probe_error LIKE 'magnet-enrich:*' → aria2c+DHT failed to fetch metadata
  //                                       (no peers / timeout) — clickable to retry
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
    const isMagnet = /^magnet:/i.test(l.url || '');
    if (isMagnet && typeof l.probe_error === 'string' && l.probe_error.startsWith('magnet-enrich:')) {
      const reason = l.probe_error.replace(/^magnet-enrich:/, '').trim() || 'no-metadata';
      const tip = `${t('links.magnetNoMeta') || 'DHT\'de peer bulunamadı, metadata çekilemedi'} (${reason}).\n${t('links.clickToRetry') || 'Yeniden denemek için tıkla.'}`;
      return `<span class="link-files-dead link-files-retry" data-lid="${l.id}" title="${esc(tip)}">❌ ${esc(t('links.magnetNoMetaShort') || 'metadata yok')} ↻</span>`;
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

function renderLinks(links, mode = 'replace') {
  // Normalize URLs once so display, copy, and external open all use the same cleaned form
  links.forEach(l => { l.url = cleanUrl(l.url); });
  const startIdx = (mode === 'append') ? _currentLinks.length : 0;
  if (mode === 'append') {
    _currentLinks = _currentLinks.concat(links);
  } else {
    _currentLinks = links;
  }
  const tbody = document.getElementById('links-body');
  if (mode === 'replace' && !links.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="no-data">${esc(t("table.noLinks"))}</td></tr>`;
    return;
  }
  const rowsHtml = links.map((l, i) => {
    const rowNum  = startIdx + i + 1;
    const checked = S.selectedLinks.has(l.id) ? ' checked' : '';
    // Same URL re-posted across multiple messages collapses into one row;
    // surface the underlying count so the user knows it's not a single shot.
    const dupBadge = (l.appearances && l.appearances > 1)
      ? ` <span class="link-dup-badge" title="${esc(t('table.appearances', { n: l.appearances }))}">×${l.appearances}</span>`
      : '';
    const isMagnet = l.url && /^magnet:/i.test(l.url);
    let urlTd;
    let mFiles = [];
    if (isMagnet) {
      let raw = l.files_json;
      if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch(e) { raw = []; } }
      if (Array.isArray(raw)) mFiles = raw;
      let magnetLabel = '';
      if (mFiles.length && mFiles[0].name) {
        magnetLabel = cleanText(mFiles[0].name).substring(0, 50);
      }
      if (!magnetLabel) {
        const hm = l.url.match(/xt=urn:btih:([a-zA-Z0-9]+)/i);
        magnetLabel = hm ? hm[1].substring(0, 12).toUpperCase() + '…' : 'Magnet';
      }
      // Expand button shown for any magnet whose file list we know about, so
      // the user can inspect the contents (and the file-name filter can
      // auto-expand matching magnets even when they hold a single file).
      const hasFiles = mFiles.length >= 1;
      const toggleBtn = hasFiles
        ? `<button class="torrent-toggle magnet-toggle" data-mlid="${l.id}" title="${esc(t('magnet.expand'))}">▶</button>`
        : '';
      urlTd = `${toggleBtn}<span class="magnet-name" title="${esc(l.url)}">${esc(magnetLabel)}</span><button class="magnet-copy-btn" data-lid="${l.id}" title="${esc(t('link.copyMagnet'))}">&#x2398;</button>${dupBadge}`;
    } else {
      const shortUrl = l.url.replace(/^https?:\/\//,'').substring(0,55);
      urlTd = `<a href="${esc(l.url)}" target="_blank" rel="noopener" style="color:#2563eb">${esc(shortUrl)}</a>${dupBadge}`;
    }
    // URL'ler zaten ASCII; group_name ve context Telegram'dan geldiği için
    // emoji/biçim temizliği uygulanır.
    const gName  = cleanText(l.group_name || '');
    const ctxRaw = cleanText(l.context || '');
    const mainRow = `<tr data-lid="${l.id}">
      <td class="chk-cell"><input type="checkbox" class="link-chk" data-lid="${l.id}"${checked}></td>
      <td class="num-cell">${rowNum}</td>
      <td title="${isMagnet ? '' : esc(l.url)}">${urlTd}</td>
      <td>${platBadge(l.platform)}</td>
      <td class="link-files-cell">${_linkFilesCell(l)}</td>
      <td>${esc(gName)}</td>
      <td>${fmtDate(l.date)}</td>
      <td class="ctx-cell" title="${esc(ctxRaw)}">${esc(ctxRaw.substring(0,40))}</td>
    </tr>`;
    const subRow = (isMagnet && mFiles.length >= 1)
      ? `<tr id="magnet-tree-${l.id}" class="magnet-tree-row" style="display:none">
           <td colspan="8"><div class="magnet-tree-panel" id="magnet-tree-panel-${l.id}"></div></td>
         </tr>`
      : '';
    return mainRow + subRow;
  }).join('');
  if (mode === 'append') {
    tbody.insertAdjacentHTML('beforeend', rowsHtml);
  } else {
    tbody.innerHTML = rowsHtml;
  }
  // Direct per-checkbox click listeners — gives us a real DOM event with shiftKey.
  document.querySelectorAll('#links-body .link-chk').forEach(cb => {
    cb.addEventListener('click', _linkCbClick);
  });
  document.querySelectorAll('#links-body .magnet-copy-btn').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const lid = parseInt(btn.getAttribute('data-lid'), 10);
      const link = _currentLinks.find(l => l.id === lid);
      if (!link) return;
      try {
        await navigator.clipboard.writeText(link.url);
        const orig = btn.innerHTML;
        btn.textContent = '✓';
        setTimeout(() => { btn.innerHTML = orig; }, 1500);
      } catch {}
    });
  });
  document.querySelectorAll('#links-body .magnet-toggle').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const lid = parseInt(btn.dataset.mlid, 10);
      toggleMagnetTree(lid, btn);
    });
  });
  // Click a "❌ metadata yok ↻" cell → re-run aria2c+DHT for that magnet.
  document.querySelectorAll('#links-body .link-files-retry').forEach(el => {
    el.addEventListener('click', async ev => {
      ev.stopPropagation();
      const lid = parseInt(el.dataset.lid, 10);
      if (!Number.isFinite(lid)) return;
      const orig = el.innerHTML;
      el.innerHTML = '⏳';
      el.style.cursor = 'wait';
      try {
        const r = await api(`/api/links/${lid}/retry-magnet-metadata`, { method: 'POST' });
        if (r.ok) {
          showToast(t('links.retryOk', { n: r.file_count }) ||
                    `Yeniden çekildi: ${r.file_count} dosya`, 3000);
        } else {
          showToast(t('links.retryFail') || 'Peer bulunamadı, yine başarısız.', 3500);
        }
        loadLinks(true);
      } catch (e) {
        el.innerHTML = orig;
        el.style.cursor = '';
        showToast((t('links.retryError') || 'Yeniden deneme hatası') + ': ' + e.message, 4000);
      }
    });
  });
  // When the user is filtering by a magnet's inner file name, auto-open the
  // matching magnet trees and highlight the matches inside.
  _autoExpandMagnetMatches();
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
    'OneDrive':'plat-onedrive','Dropbox':'plat-dropbox','YouTube':'plat-youtube',
    'GitHub':'plat-github','Magnet':'plat-magnet'}[p]||'plat-other';
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
  ['name','group','size','date','shares'].forEach(c => {
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
  // Legacy entry-point kept so old callers still work. Files + Links both use
  // infinite-scroll footers now; route there instead of rendering page-nav.
  if (S.activeTab === 'files') { renderFilesFooter({ total }); return; }
  if (S.activeTab === 'links') { renderLinksFooter({ total }); return; }
  // No other tab calls this currently — clear the footer if it gets called.
  const el = document.getElementById('pagination');
  if (el) el.innerHTML = '';
}

// Infinite-scroll footer for the Files tab. Keeps the bulk-download button
// + page-size selector + "loaded / total" indicator visible. No page-nav.
function renderFilesFooter(data) {
  const el = document.getElementById('pagination');
  if (!el) return;
  const loaded = _currentFiles.length;
  const total  = (data && data.total != null) ? data.total : _filesTotal;
  el.innerHTML = `
    <button id="bulk-dl-btn" onclick="bulkDownloadSelected()"></button>
    <select id="pag-limit" onchange="setPagLimit(this.value)" style="margin-left:auto">
      <option value="100" ${S.limit==100?'selected':''}>100</option>
      <option value="500" ${S.limit==500?'selected':''}>500</option>
      <option value="1000" ${S.limit==1000?'selected':''}>1000</option>
    </select>
    <span class="files-loaded-pill" title="${esc(t('files.loadedTip') || 'Şu ana kadar yüklenen / toplam')}">${loaded.toLocaleString()} / ${total.toLocaleString()}</span>`;
  applySyncStatusToUI();
  updateBulkFileBtn();
}

// Infinite-scroll observer for the Files grid. Same pattern as the hunter
// grid: a sentinel right after the table fires hourly load-more requests.
let _filesScrollObs   = null;
let _filesLoadingMore = false;
function _mountFilesInfiniteScroll() {
  if (_filesScrollObs) return;
  const sentinel = document.getElementById('files-grid-sentinel');
  const root     = document.getElementById('table-wrap');
  if (!sentinel || !root) return;
  _filesScrollObs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) filesLoadMore();
    }
  }, { root, rootMargin: '400px' });
  _filesScrollObs.observe(sentinel);
}

// Infinite-scroll footer for the Links tab. Mirrors renderFilesFooter but
// hosts the "copy to clipboard" bulk action instead of "download all".
function renderLinksFooter(data) {
  const el = document.getElementById('pagination');
  if (!el) return;
  const loaded = _currentLinks.length;
  const total  = (data && data.total != null) ? data.total : _linksTotal;
  el.innerHTML = `
    <button id="bulk-copy-btn" onclick="copyLinksToClipboard()"><span class="i18n-bcopy">📋 ${esc(t('table.linkCopy') || 'Panoya Kopyala')} (</span><span id="bulk-copy-count">0</span>)</button>
    <select id="pag-limit" onchange="setPagLimit(this.value)" style="margin-left:auto">
      <option value="100" ${S.linkLimit==100?'selected':''}>100</option>
      <option value="500" ${S.linkLimit==500?'selected':''}>500</option>
      <option value="1000" ${S.linkLimit==1000?'selected':''}>1000</option>
    </select>
    <span class="files-loaded-pill" title="${esc(t('files.loadedTip') || 'Şu ana kadar yüklenen / toplam')}">${loaded.toLocaleString()} / ${total.toLocaleString()}</span>`;
  applySyncStatusToUI();
  updateBulkLinkBtn();
}

let _linksScrollObs   = null;
let _linksLoadingMore = false;
function _mountLinksInfiniteScroll() {
  if (_linksScrollObs) return;
  // Files + Links tables share #table-wrap as their scroll container, so the
  // single sentinel placed below both tables is fine for either grid.
  const sentinel = document.getElementById('files-grid-sentinel');
  const root     = document.getElementById('table-wrap');
  if (!sentinel || !root) return;
  _linksScrollObs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) linksLoadMore();
    }
  }, { root, rootMargin: '400px' });
  _linksScrollObs.observe(sentinel);
}

async function linksLoadMore() {
  if (_linksLoadingMore) return;
  if (_currentLinks.length >= _linksTotal && _linksTotal > 0) return;
  if (S.activeTab !== 'links') return;
  _linksLoadingMore = true;
  const loader = document.getElementById('files-grid-loading');
  if (loader) loader.style.display = '';
  try {
    await loadLinks(true, 'append');
  } finally {
    if (loader) loader.style.display = 'none';
  }
}

async function filesLoadMore() {
  if (_filesLoadingMore) return;
  if (_currentFiles.length >= _filesTotal && _filesTotal > 0) return;
  // Anything that isn't the Files tab shouldn't pull files. Same idea as
  // hunter's load-more guard.
  if (S.activeTab !== 'files') return;
  _filesLoadingMore = true;
  const loader = document.getElementById('files-grid-loading');
  if (loader) loader.style.display = '';
  try {
    await loadFiles(true, false, 'append');
  } finally {
    if (loader) loader.style.display = 'none';
    // _filesLoadingMore is cleared inside _paintFilesResult after the new
    // rows are in the DOM; that way an immediately-visible sentinel can
    // re-trigger load-more without a tight loop.
  }
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

// Stronger cleaner for channel / group / publisher labels. On top of what
// cleanText strips we also:
//   - NFKC-normalize so Unicode "fancy fonts" (𝐀, 𝙰, 𝘈, ⒜, ＡＢＣ, ㎏, …)
//     collapse to their plain ASCII / regular forms.
//   - Drop \p{So} (Symbol, other — covers ⭐✨◆◇★☆⚠ etc.) and box-drawing /
//     block-element / geometric-shape / dingbat code points that survive
//     pictographic stripping.
// Real letters in Latin / Cyrillic / Greek / CJK / Turkish accented chars
// stay because they're \p{L} (Letter), not stripped here.
function plainName(s) {
  if (s == null || s === '') return '';
  return String(s)
    .normalize('NFKC')
    .replace(/\p{Extended_Pictographic}/gu, '')
    .replace(/\p{So}/gu, '')
    .replace(/[\u{1F1E6}-\u{1F1FF}]/gu, '')
    .replace(/[\u{1F3FB}-\u{1F3FF}]/gu, '')
    .replace(/[\u{2500}-\u{257F}\u{2580}-\u{259F}\u{25A0}-\u{25FF}\u{2700}-\u{27BF}]/gu, '')
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
function fmtSize(b){if(!b)return'—';const GB=1073741824;if(b>=1000*GB)return(b/(1024*GB)).toFixed(2)+' TB';if(b>=GB)return(b/GB).toFixed(1)+' GB';if(b>=1048576)return(b/1048576).toFixed(1)+' MB';if(b>=1024)return(b/1024).toFixed(0)+' KB';return b+' B';}
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

// ── Watch → Saved Messages push toggle ───────────────────────────────────────
async function loadNotifyPushSettings() {
  try {
    const s = await api('/api/notify/settings');
    const cb  = document.getElementById('notify-tg-push');
    const st  = document.getElementById('notify-tg-push-status');
    if (cb) cb.checked = !!s.tg_push_enabled;
    if (st) {
      if (s.last_push_at) {
        st.textContent = t('notify.lastPushAt', { when: fmtDate(s.last_push_at) }) ||
                         `Son gönderim: ${fmtDate(s.last_push_at)}`;
      } else if (s.tg_push_enabled) {
        st.textContent = t('notify.pushArmed') || 'Aktif — henüz eşleşme yok.';
      } else {
        st.textContent = '';
      }
    }
  } catch (e) { /* ignore */ }
}

async function saveNotifyPushToggle() {
  const cb = document.getElementById('notify-tg-push');
  if (!cb) return;
  const enabled = cb.checked;
  try {
    await api('/api/notify/settings', { method: 'PUT', json: { tg_push_enabled: enabled } });
    showToast(enabled
      ? (t('notify.pushOn') || 'Telegram bildirimleri aktif')
      : (t('notify.pushOff') || 'Telegram bildirimleri kapalı'), 2000);
    loadNotifyPushSettings();
  } catch (e) {
    showToast((t('notify.pushFail') || 'Bildirim ayarı kaydedilemedi') + ': ' + e.message, 3500);
    // Revert checkbox so UI matches server state.
    cb.checked = !enabled;
  }
}

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
    return `<div class="acc-row" id="acc-row-${a.id}">
      <span class="acc-name">${esc(a.name)}</span>
      <span class="acc-meta">${phone}${apiPart} · ${meta}</span>
      <span class="acc-status ${stCls}">${esc(stTxt)}</span>
      <span class="acc-actions">
        ${loginBtn}
        <button class="acc-btn acc-edit-btn" data-id="${a.id}" data-name="${esc(a.name||'')}" data-api-id="${a.api_id||''}" data-api-hash="${esc(a.api_hash||'')}" title="${esc(t('common.edit'))}">✏️</button>
        <button class="acc-btn acc-btn-danger" onclick="deleteAcc(${a.id})">${esc(t('accounts.delete'))}</button>
      </span>
    </div>
    <div id="acc-ef-${a.id}" style="display:none;margin:4px 0 10px;padding:12px;background:var(--bg-3);border-radius:8px">
      <div class="creds-form">
        <input id="acc-edit-name-${a.id}" type="text" placeholder="${esc(t('accounts.namePh'))}">
        <input id="acc-edit-api-id-${a.id}" type="text" placeholder="API ID">
        <input id="acc-edit-api-hash-${a.id}" type="text" placeholder="API Hash">
        <div style="display:flex;gap:6px;margin-top:6px;width:100%">
          <button class="creds-btn" onclick="submitEditAcc(${a.id})">${esc(t('common.save'))}</button>
          <button class="creds-btn creds-btn-alt" onclick="closeEditAcc(${a.id})">${esc(t('common.cancel'))}</button>
        </div>
      </div>
    </div>`;
  }).join('');

  // Wire up edit buttons via event delegation (avoids inline onclick quote issues)
  el.querySelectorAll('.acc-edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      openEditAcc(btn.dataset.id, btn.dataset.name, btn.dataset.apiId, btn.dataset.apiHash);
    });
  });
}

function openEditAcc(id, name, apiId, apiHash) {
  document.querySelectorAll('[id^="acc-ef-"]').forEach(el => el.style.display = 'none');
  const form = document.getElementById(`acc-ef-${id}`);
  if (!form) return;
  form.style.display = '';
  document.getElementById(`acc-edit-name-${id}`).value = name || '';
  document.getElementById(`acc-edit-api-id-${id}`).value = apiId || '';
  document.getElementById(`acc-edit-api-hash-${id}`).value = apiHash || '';
  document.getElementById(`acc-edit-name-${id}`).focus();
}

function closeEditAcc(id) {
  const form = document.getElementById(`acc-ef-${id}`);
  if (form) form.style.display = 'none';
}

async function submitEditAcc(id) {
  const name   = document.getElementById(`acc-edit-name-${id}`).value.trim();
  const apiIdV = document.getElementById(`acc-edit-api-id-${id}`).value.trim();
  const apiHash = document.getElementById(`acc-edit-api-hash-${id}`).value.trim();
  const body = {};
  if (name)   body.name    = name;
  if (apiIdV) body.api_id  = parseInt(apiIdV, 10);
  if (apiHash) body.api_hash = apiHash;
  if (!Object.keys(body).length) { closeEditAcc(id); return; }
  try {
    await api(`/api/accounts/${id}`, { method: 'PATCH', json: body });
    closeEditAcc(id);
    await loadAccountsList();
  } catch (e) {
    alert('Kaydedilemedi: ' + e.message);
  }
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
  // Polling now only refreshes status/log/total — the grid itself updates
  // incrementally via IntersectionObserver (load-more on scroll) plus
  // explicit reloads after run/filter/candidate-action.
  _hunterPollTimer = setInterval(() => { pollHunterStatus(); }, 1500);
  // Default the log drawer to collapsed; honor any saved "open" preference.
  const list = document.getElementById('hunter-log-list');
  const arr  = document.getElementById('hc-log-arrow');
  const open = (() => { try { return localStorage.getItem('tf_hunter_log_collapsed') === '0'; } catch (e) { return false; } })();
  if (list) list.classList.toggle('collapsed', !open);
  if (arr)  arr.classList.toggle('open', open);
}

// IntersectionObserver: when the sentinel scrolls into view, fetch the next
// page and append. Re-arms automatically after each append since the sentinel
// is preserved across reloads. Guarded against double-fires while a fetch is
// still in flight.
let _hunterScrollObs = null;
let _hunterLoadingMore = false;
function _hunterMountInfiniteScroll() {
  if (_hunterScrollObs) return;
  const sentinel = document.getElementById('hunter-grid-sentinel');
  const root     = document.getElementById('hunter-panel');
  if (!sentinel || !root) return;
  _hunterScrollObs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) hunterLoadMore();
    }
  }, { root, rootMargin: '300px' });
  _hunterScrollObs.observe(sentinel);
}

async function hunterLoadMore() {
  if (_hunterLoadingMore) return;
  if (_hunterCandidates.length >= S.hunterTotal && S.hunterTotal > 0) return;
  _hunterLoadingMore = true;
  const loader = document.getElementById('hunter-grid-loading');
  if (loader) loader.style.display = '';
  try {
    await hunterReloadCandidates(true, 'append');
  } finally {
    _hunterLoadingMore = false;
    if (loader) loader.style.display = 'none';
  }
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
    // Log her durumda (çalışıyor veya durmuş) render edilir — kalıcı log.
    _renderHunterLog(s.events || []);
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

let _hunterLogSig = '';
function _renderHunterLog(events) {
  const el = document.getElementById('hunter-log-list');
  if (!el) return;
  _updateHunterLogPulse(events);
  if (!events.length) {
    const sig = '__empty__';
    if (sig === _hunterLogSig) return;
    _hunterLogSig = sig;
    el.innerHTML = `<div class="hl-empty">${esc(t('hunter.logEmpty'))}</div>`;
    return;
  }
  // İçerik imzası: aynıysa yeniden çizme (seçimi/scroll'u koru).
  const sig = events.length + '|' + (events[0]?.ts || '') + '|' + (events[events.length-1]?.ts || '') + '|' + (events[events.length-1]?.msg || events[events.length-1]?.key || '');
  if (sig === _hunterLogSig) return;
  // Kullanıcı log içinde metin seçtiyse re-render etme (seçim kaybolmasın).
  const sel = window.getSelection && window.getSelection();
  if (sel && !sel.isCollapsed && sel.rangeCount > 0) {
    const range = sel.getRangeAt(0);
    if (el.contains(range.commonAncestorContainer)) return;
  }
  _hunterLogSig = sig;
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

function _updateHunterLogPulse(events) {
  // Shows the most-recent log line (truncated) on the drawer title bar, so
  // users can keep one eye on activity without expanding the drawer.
  const el = document.getElementById('hunter-log-pulse');
  if (!el) return;
  if (!events || !events.length) { el.textContent = ''; return; }
  const last = events[events.length - 1];
  const ts   = (last.ts || '').substring(11, 19);  // HH:MM:SS
  const txt  = _eventText(last);
  el.textContent = ts ? `${ts} · ${txt}` : txt;
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
    set('h-magnethunt', _hunterSettings.magnethunt_enabled);
    set('h-magnet-backfill', _hunterSettings.magnet_backfill_enabled);
    set('h-web-delay', _hunterSettings.web_request_delay_ms);
    set('h-web-conc',  _hunterSettings.web_concurrency);
    set('h-tg-delay',  _hunterSettings.tg_request_delay_ms);
    set('h-tg-cap',    _hunterSettings.tg_daily_lookup_cap);
    set('h-tg-sample', _hunterSettings.tg_messages_to_sample);
    set('h-tg-account',_hunterSettings.tg_account_id);
    set('h-temp-join', _hunterSettings.tg_temp_join_enabled);
    set('h-skip-old',  _hunterSettings.skip_old_channels !== false);
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
    magnethunt_enabled: get('h-magnethunt'),
    magnet_backfill_enabled: get('h-magnet-backfill'),
    web_request_delay_ms: get('h-web-delay'),
    web_concurrency: get('h-web-conc'),
    tg_request_delay_ms: get('h-tg-delay'),
    tg_daily_lookup_cap: get('h-tg-cap'),
    tg_messages_to_sample: get('h-tg-sample'),
    tg_account_id: get('h-tg-account'),
    tg_temp_join_enabled: get('h-temp-join'),
    skip_old_channels: get('h-skip-old'),
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

// ── Magnet Hunt (Google-dork search for magnet: URIs) ──────────────────────
let _magnetHuntPollTimer = null;

async function magnetHuntToggle() {
  // Toggle: if running → cancel; otherwise → start
  try {
    const s = await api('/api/hunter/magnet_hunt/status');
    if (s.running) {
      await api('/api/hunter/magnet_hunt/cancel', { method: 'POST' });
      showToast(t('magnetHunt.cancelled'), 2000);
    } else {
      const r = await api('/api/hunter/magnet_hunt/run', { method: 'POST' });
      if (r.ok) {
        showToast(t('magnetHunt.started'), 2000);
        _magnetHuntStartPolling();
      }
    }
  } catch (e) {
    if (e && e.status === 409) showToast(t('magnetHunt.alreadyRunning'), 3000);
    else showToast(t('common.error') + ' ' + esc(e.message || e), 4000);
  }
}

function _magnetHuntStartPolling() {
  if (_magnetHuntPollTimer) clearInterval(_magnetHuntPollTimer);
  _magnetHuntPollTimer = setInterval(_magnetHuntPoll, 2000);
  _magnetHuntPoll();
}

async function _magnetHuntPoll() {
  try {
    const s = await api('/api/hunter/magnet_hunt/status');
    _updateMagnetHuntBtn(s);
    if (!s.running) {
      clearInterval(_magnetHuntPollTimer);
      _magnetHuntPollTimer = null;
      if (s.magnets_new > 0) showToast(t('magnetHunt.summaryNew', {n: s.magnets_new}), 4000);
    }
  } catch (e) { /* ignore */ }
}

function _updateMagnetHuntBtn(s) {
  const btn  = document.getElementById('magnet-hunt-btn');
  const meta = document.getElementById('magnet-hunt-meta');
  if (!btn || !meta) return;
  if (s && s.running) {
    btn.classList.add('active');
    const eng = s.current_engine ? `· ${esc(s.current_engine)} ` : '';
    meta.textContent = `${eng}${s.engines_done}/${s.engines_total} · ${s.magnets_new || 0} ${t('magnetHunt.newShort')}`;
  } else {
    btn.classList.remove('active');
    meta.textContent = '';
  }
}

// Pick up state on page load (in case a hunt is already running from a previous session)
async function _magnetHuntInitOnSwitch() {
  try {
    const s = await api('/api/hunter/magnet_hunt/status');
    if (s.running) _magnetHuntStartPolling();
    else _updateMagnetHuntBtn(s);
  } catch (e) {}
}

async function hunterReloadCandidates(silent, mode = 'replace') {
  const status = document.getElementById('hunter-filter-status')?.value || '';
  const page   = S.hunterLimit || 200;
  const offset = mode === 'append' ? _hunterCandidates.length : 0;
  const params = new URLSearchParams({
    sort_by:  _hgSortBy,
    sort_dir: _hgSortDir,
    limit:    String(page),
    offset:   String(offset),
  });
  if (status) params.set('status', status);
  // Only paint the loading skeleton on a full replace from a user action —
  // not silent (polling) or appending more (infinite scroll), which would
  // wipe the grid.
  if (!silent && mode === 'replace') _paintGridLoading('hunter-grid-body', 11);
  try {
    const r = await api('/api/hunter/candidates?' + params);
    const newRows = r.candidates || [];
    S.hunterTotal = r.total || 0;
    if (mode === 'append') {
      _hunterCandidates = _hunterCandidates.concat(newRows);
    } else {
      _hunterCandidates = newRows;
    }
    renderHunterCandidates();
    _updateHunterTotalPill();
    if (mode === 'replace') _hunterMountInfiniteScroll();
  } catch(e) {
    if (!silent) console.warn(e);
  }
}

function _updateHunterTotalPill() {
  const el = document.getElementById('hunter-total-count');
  if (!el) return;
  const total = S.hunterTotal || 0;
  if (total <= 0) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.innerHTML = `<b>${total.toLocaleString()}</b> ${esc(t('hunter.totalCandidates'))}`;
}

// Old pager + per-page picker removed: the hunter grid is now infinite-scroll
// (see _hunterMountInfiniteScroll / hunterLoadMore). Total count moved into
// the toolbar pill (_updateHunterTotalPill).

// Status/sort changes shrink or shuffle the result set, so any non-zero
// offset becomes meaningless — reset to page 1.
function hunterFilterChange() {
  S.hunterOffset = 0;
  // Mirror the toolbar status into the column-header filter so the two stay
  // in sync no matter which one the user touches.
  const tb  = document.getElementById('hunter-filter-status')?.value || '';
  const col = document.getElementById('hg-flt-status');
  if (col && col.value !== tb) col.value = tb;
  _hgLastFetchedStatus = tb;
  _hgUpdateEventCol();
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
    _hgSortDir = (col === 'username' || col === 'status') ? 'asc' : 'desc';
  }
  hunterReloadCandidates();
}

function _hgUpdateSortArrows() {
  const map = {score:'hg-arr-score', username:'hg-arr-username', members:'hg-arr-members',
                estimated_files:'hg-arr-files', last_message_at:'hg-arr-last', discovered_at:'hg-arr-disc',
                status:'hg-arr-status', sources:'hg-arr-sources',
                type_video:'hg-arr-type-video', type_audio:'hg-arr-type-audio',
                type_image:'hg-arr-type-image', type_archive:'hg-arr-type-archive',
                type_document:'hg-arr-type-document', type_software:'hg-arr-type-software',
                type_other:'hg-arr-type-other'};
  for (const [k, id] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.textContent = _hgSortBy === k ? (_hgSortDir === 'asc' ? '▲' : '▼') : '▲▼';
    el.classList.toggle('active', _hgSortBy === k);
  }
}

const _HG_EV_MAP = {
  discovered:  { field: 'discovered_at', label: 'Keşif Tarihi' },
  enriched:    { field: 'enriched_at',   label: 'Zenginl. Tarihi' },
  joined:      { field: 'decided_at',    label: 'Katıldı Tarihi' },
  rejected:    { field: 'decided_at',    label: 'Reddedildi Tarihi' },
  blacklisted: { field: 'decided_at',    label: 'Kara Liste Tarihi' },
};

function _hgUpdateEventCol() {
  const st = document.getElementById('hg-flt-status')?.value || '';
  const grid = document.getElementById('hunter-grid');
  const th = document.getElementById('hg-th-event');
  const ev = _HG_EV_MAP[st];
  if (ev) {
    grid?.classList.add('hg-show-ev');
    if (th) th.textContent = ev.label;
  } else {
    grid?.classList.remove('hg-show-ev');
  }
}

let _hgLastFetchedStatus = '';

function hgFilterChange() {
  // The column-header status filter is the same control the user reaches for
  // most often. Keep it in sync with the toolbar dropdown AND re-fetch from
  // the API when status changes — otherwise statuses excluded from the
  // default backend filter (joined / rejected / blacklisted / failed) never
  // appear because they were never loaded in the first place.
  _hgUpdateEventCol();
  const colSt = document.getElementById('hg-flt-status')?.value || '';
  const tb = document.getElementById('hunter-filter-status');
  if (tb && tb.value !== colSt) tb.value = colSt;
  if (colSt !== _hgLastFetchedStatus) {
    _hgLastFetchedStatus = colSt;
    hunterReloadCandidates();
    return;
  }
  renderHunterCandidates();
}

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

  // Rows arrive pre-sorted from the server; only local-filter columns (sources,
  // username text filter) still run client-side here.
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
    const bd = c.file_type_breakdown || {};
    const _tc = (k, col) => { const v = bd[k]||0; return `<td class="hg-tc${v?'':' zero'}" style="color:${v?col:'var(--text-4)'}">${v||'—'}</td>`; };
    const evInfo = _HG_EV_MAP[document.getElementById('hg-flt-status')?.value || ''];
    const evDate = evInfo ? (c[evInfo.field] ? fmtDate(c[evInfo.field]).substring(0,16) : '—') : '';
    return `<tr class="${sel?'hg-row-selected':''}" onclick="hgRowClick(event, ${c.id})">
      <td class="hg-chk-cell"><input type="checkbox" data-hg-cid="${c.id}" ${sel?'checked':''}></td>
      <td><div class="hg-score${scoreCls}">${score.toFixed(0)}</div></td>
      <td><div class="hg-channel"><span class="hg-title" title="${esc(title)} · @${esc(c.username)}">${esc(title)}</span> <span class="hg-uname">@${esc(c.username)}</span>${queueBadge}</div></td>
      <td>${members}</td>
      <td>${files}</td>
      ${_tc('video','#ef4444')}${_tc('audio','#7c3aed')}${_tc('image','#059669')}${_tc('archive','#f59e0b')}${_tc('document','#2563eb')}${_tc('software','#374151')}${_tc('other','#9ca3af')}
      <td style="font-size:.72rem;color:var(--text-3)">${esc(last)}</td>
      <td style="font-size:.72rem;color:var(--text-3)">${esc(disc)}</td>
      <td class="hg-ev-col" style="font-size:.72rem;color:var(--text-3)">${esc(evDate)}</td>
      <td><span class="hg-status s-${status}">${esc(t('hunter.status' + status.charAt(0).toUpperCase() + status.slice(1)) || status)}</span></td>
      <td><span class="hg-sources" title="${esc((c.sources||[]).join(', '))}">${esc(sources || '—')}</span></td>
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
    c.kind = 'candidate';
    _renderDetailModal(c);
  } catch (e) {
    alert(e.message);
  }
}

// Shared modal renderer used by BOTH the Channel Hunter grid and the Channels
// grid. The only contextual differences are the header icon, the action set,
// and whether we wire up the deep-scan file list panel below the actions.
function _renderDetailModal(c) {
  const isChannel = c.kind === 'channel';
  const breakdown = c.file_type_breakdown || {};
  const types = Object.entries(breakdown).filter(([,v]) => v > 0)
    .map(([k,v]) => `<span class="hd-type-pill" style="border-left:3px solid ${_HUNTER_TYPE_COLORS[k]||'#9ca3af'};padding-left:8px">${esc(k)}: <b>${v}</b></span>`)
    .join('');
  const sources = (c.sources || []).join(', ');
  const headerIcon = isChannel ? '📋' : '🎯';

  // Action set depends on context. Channels reuse the same data-act dispatcher
  // pattern as hunter but with channel-management verbs (rescan/hide/excl/leave).
  let actionsHtml;
  if (isChannel) {
    actionsHtml = `
      <a href="https://t.me/${esc(c.username)}" target="_blank" rel="noopener" class="h-btn">↗ Telegram</a>
      <button class="h-btn" data-act="ch-files" data-i18n-title="channels.openFilesTip" title="Bu kanaldaki dosyaları Dosyalar sekmesinde aç">📁 ${esc(t('channels.openFiles'))}</button>
      <button class="h-btn" data-act="ch-rescan" data-i18n-title="channels.tipRescan" title="">${esc(t('groups.bulkRescan'))}</button>
      <button class="h-btn" data-act="ch-hide"   data-i18n-title="channels.tipHide"   title="">${esc(c.hidden ? t('groups.bulkShow') : t('groups.bulkHide'))}</button>
      <button class="h-btn" data-act="ch-excl"   data-i18n-title="channels.tipUntrack" title="">${esc(c.excluded ? t('groups.bulkTrack') : t('groups.bulkUntrack'))}</button>
      <button class="h-btn h-btn-reject" data-act="ch-leave" data-i18n-title="channels.tipLeave" title="">${esc(t('groups.bulkLeave'))}</button>
      <button class="h-btn" style="margin-left:auto" data-act="close">${esc(t('common.close'))}</button>`;
  } else {
    actionsHtml = `
      <a href="https://t.me/${esc(c.username)}" target="_blank" rel="noopener" class="h-btn">↗ Telegram</a>
      <button class="h-btn"               data-act="deepScan"  title="${esc(t('hd.deepScan'))}">${esc(t('hd.deepScan'))}</button>
      ${c.status !== 'joined' && c.status !== 'blacklisted' ? `<button class="h-btn h-btn-join" data-act="join"      title="${esc(t('hunter.actionHelpJoin'))}">${esc(t('hunter.join'))}</button>` : ''}
      ${c.status !== 'rejected' && c.status !== 'blacklisted' ? `<button class="h-btn"          data-act="reject"    title="${esc(t('hunter.actionHelpReject'))}">${esc(t('hunter.reject'))}</button>` : ''}
      ${c.status !== 'blacklisted' ? `<button class="h-btn h-btn-reject" data-act="blacklist" title="${esc(t('hunter.actionHelpBlacklist'))}">${esc(t('hunter.blacklist'))}</button>` : ''}
      ${c.status === 'blacklisted' || c.status === 'rejected' ? `<button class="h-btn" data-act="restore" title="${esc(t('hunter.restoreBtnTitle'))}">${esc(t('hunter.restoreBtn'))}</button>` : ''}
      <button class="h-btn" style="margin-left:auto" data-act="close">${esc(t('common.close'))}</button>`;
  }

  // For channels the file list comes from the main Files tab, so we hide the
  // hunter-specific file panel + deep-scan progress block. Everything else
  // (header / meta grid / type pills / actions) stays visually identical.
  document.getElementById('hunter-detail-body').innerHTML = `
    <div class="hd-head">
      <h2>${headerIcon} ${esc(c.title || c.username || ('id ' + (c.group_id || c.id || '')))} <code style="font-size:.75rem;background:var(--bg-info);color:var(--accent-h);padding:2px 8px;border-radius:5px">@${esc(c.username || '')}</code></h2>
      ${c.description ? `<div class="hd-desc">${esc(c.description)}</div>` : ''}
    </div>

    <div class="hd-meta">
      <div class="hd-grid">
        <b>${esc(t('hunter.score'))}:</b> <span><b style="color:var(--accent);font-size:1.05rem">${(c.score||0).toFixed(1)}</b></span>
        <b>${esc(t('hunter.members'))}:</b> <span>${c.members != null ? Number(c.members).toLocaleString() : '—'}</span>
        <b>${esc(t('hunter.totalFilesSampled'))}:</b> <span>${(c.file_count_sample||0).toLocaleString()} / ${(c.sampled_messages||0).toLocaleString()}</span>
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
      ${actionsHtml}
    </div>

    <div class="hd-files">
      <h4>${esc(t('hd.fileList'))}</h4>
      <div id="hd-files-area"></div>
    </div>`;

  document.getElementById('hunter-detail-overlay').classList.add('open');

  _hdFilesQ = ''; _hdFilesExt = ''; _hdFilesSortBy = 'date'; _hdFilesSortDir = 'desc';

  if (isChannel) {
    _currentChannelGid = c.group_id;
    _currentDetailCid  = null;
    _hdFilesBase       = `/api/channels/${c.group_id}`;
    _bindChDetailActions();
    refreshHdFiles();
  } else {
    _currentDetailCid      = c.id;
    _currentDetailUsername = c.username;
    _currentChannelGid     = null;
    _hdFilesBase           = `/api/hunter/candidates/${c.id}`;
    _bindHdActions();
    refreshHdFiles();
    pollDeepScan();
  }
}

// ── Detail modal: file list / deep-scan ─────────────────────────────────────
let _currentDetailCid = null;
let _currentDetailUsername = null;
let _hdFilesBase = null;   // '/api/hunter/candidates/{id}' or '/api/channels/{gid}'
let _hdFilesQ = '';
let _hdFilesExt = '';
let _hdFilesSortBy = 'date';
let _hdFilesSortDir = 'desc';
let _hdDeepPollTimer = null;
let _hdScanState = null;        // 'running' | 'done' | 'error' | 'cancelled' | null
let _hdScanProcessed = 0;       // processed-message count emitted by backend
let _hdScanTempJoined = false;
let _hdScanTempJoinErr = null;
let _hdRefreshSkip = 0;         // refresh files every other tick to keep UI snappy

async function pollDeepScan() {
  if (_hdDeepPollTimer) { clearInterval(_hdDeepPollTimer); _hdDeepPollTimer = null; }
  if (!_currentDetailCid) return;
  const tick = async () => {
    try {
      const s = await api(`/api/hunter/candidates/${_currentDetailCid}/deep_scan_status`);
      _hdScanState = s.state || null;
      _hdScanProcessed = s.processed || 0;
      _hdScanTempJoined = !!s.temp_joined;
      _hdScanTempJoinErr = s.temp_join_error || null;
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
  if (!_hdFilesBase) return;
  const area = document.getElementById('hd-files-area');
  if (!area) return;
  const params = new URLSearchParams({
    sort_by: _hdFilesSortBy, sort_dir: _hdFilesSortDir, limit: '500'
  });
  if (_hdFilesQ) params.set('q', _hdFilesQ);
  if (_hdFilesExt) params.set('ext', _hdFilesExt);
  let data;
  try { data = await api(`${_hdFilesBase}/files?${params}`); }
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
    } else if (_hdScanState === 'done') {
      // Scan finished but found 0 files. Show what we already tried so the
      // user knows there's no "join + retry" knob to pull anymore.
      let msg;
      if (_hdScanTempJoined) {
        msg = t('hd.noFilesAfterTempJoin') ||
              '✓ Üye olundu, tarandı, üyelikten ayrılındı — yine dosya bulunamadı. Kanal gerçekten boş ya da içerik kısıtlı.';
      } else if (_hdScanTempJoinErr) {
        msg = t('hd.tempJoinFailed', { err: _hdScanTempJoinErr }) ||
              `⚠ Otomatik üyelik başarısız (${_hdScanTempJoinErr}). Kanala manuel katılırsan Tam Tara tekrar denenebilir.`;
      } else {
        msg = t('hd.noFilesAfterScan');
      }
      body += `<div style="text-align:center;padding:20px;color:var(--text-3);font-size:.82rem">${esc(msg)}</div>`;
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

const _PREVIEWABLE_GROUPS = new Set(['image', 'video']);

function _isPreviewable(f) {
  return _PREVIEWABLE_GROUPS.has(f.file_group);
}

// Renders a single <li> for one candidate file. State for an in-flight
// download (if any) lives in _hdDlStatus[msg_id]; persisted "already
// downloaded" state lives in f.local_path.
function _renderHdFileRow(f) {
  const msgId = f.message_id;
  const dl    = _hdDlStatus[msgId];
  const dlState = dl ? dl.state : (f.local_path ? 'done' : 'idle');
  const canPreview = _isPreviewable(f);

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
  const previewBtn = canPreview
    ? `<button class="hf-btn hf-btn-preview" data-act="preview" data-msg="${msgId}" data-fname="${esc(f.file_name||'')}" title="${esc(t('hf.preview'))}">👁</button>`
    : '';
  const kindBadge = (f.is_named === false)
    ? `<span class="hf-kind hf-kind-ephem" title="${esc(t('hf.kindEphemTitle'))}">🎤</span>`
    : `<span class="hf-kind hf-kind-named" title="${esc(t('hf.kindNamedTitle'))}">📄</span>`;
  return `<li class="hf-row${liExtraClass}${f.is_named === false ? ' hf-ephem' : ''}" data-msg="${msgId}" data-group="${esc(f.file_group||'')}">
    ${kindBadge}
    <span class="hf-name" title="${esc(f.file_name||'')}">${esc(f.file_name || '—')}</span>
    <span class="hf-size">${fmtSize(f.file_size||0)}</span>
    <span class="hf-date">${f.date ? fmtDate(f.date).substring(0,16) : '—'}</span>
    <span class="hf-actions">${previewBtn}${actionsHtml}</span>
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
  if (!Number.isFinite(msgId) || (_currentDetailCid == null && _currentChannelGid == null)) return;
  switch (act) {
    case 'download':     hfStartDownload(msgId, false); break;
    case 'downloadJoin': hfDownloadWithTempJoin(msgId); break;
    case 'cancel':       hfCancelDownload(msgId); break;
    case 'open':         hfOpenDownloaded(msgId); break;
    case 'delete':       hfDeleteDownloaded(msgId); break;
    case 'preview':      hfOpenPreview(msgId, btn.dataset.fname || ''); break;
  }
}

async function hfStartDownload(msgId, withTempJoin) {
  if (_currentChannelGid != null) {
    // Channel files: delegate to main download flow (handles destinations, scheduling)
    triggerDownload(msgId);
    return;
  }
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
  const canPreview = _PREVIEWABLE_GROUPS.has(li.dataset.group || '');
  const previewBtn = canPreview
    ? `<button class="hf-btn hf-btn-preview" data-act="preview" data-msg="${msgId}" title="${esc(t('hf.preview'))}">👁</button>`
    : '';
  li.classList.remove('hf-downloading', 'hf-error');
  if (dlState === 'downloading') {
    li.classList.add('hf-downloading');
    const pct = dl.progress != null ? Math.round(dl.progress * 100) : 0;
    actions.innerHTML = `<span class="hf-progress">${pct}%</span>
      <button class="hf-btn hf-btn-del" data-act="cancel" data-msg="${msgId}" title="${esc(t('hf.cancel'))}">✕</button>`;
  } else if (dlState === 'done') {
    actions.innerHTML = `${previewBtn}<button class="hf-btn" data-act="open" data-msg="${msgId}" title="${esc(t('hf.open'))}">💾</button>
      <button class="hf-btn hf-btn-del" data-act="delete" data-msg="${msgId}" title="${esc(t('hf.delete'))}">🗑</button>`;
  } else if (dlState === 'error') {
    li.classList.add('hf-error');
    actions.innerHTML = `${previewBtn}<button class="hf-btn" data-act="download" data-msg="${msgId}" title="${esc(t('hf.retry'))}">↻</button>`;
  } else if (dlState === 'needs_temp_join') {
    actions.innerHTML = `${previewBtn}<button class="hf-btn" data-act="downloadJoin" data-msg="${msgId}" title="${esc(t('hf.needsJoin'))}">🔒</button>`;
  } else {
    actions.innerHTML = `${previewBtn}<button class="hf-btn" data-act="download" data-msg="${msgId}" title="${esc(t('hf.download'))}">📥</button>`;
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
  if (_currentChannelGid != null) {
    try { await api(`/api/files/${msgId}/cancel`, { method: 'POST' }); } catch(e) {}
  } else {
    const cid = _currentDetailCid;
    try { await api(`/api/hunter/candidates/${cid}/files/${msgId}/download/cancel`, { method: 'POST' }); } catch(e) {}
  }
  _stopFileDlPoller(msgId);
  _hdDlStatus[msgId] = { state: 'idle' };
  _refreshHdFileRow(msgId);
}

function hfOpenDownloaded(msgId) {
  const url = _currentChannelGid != null
    ? `/api/files/${msgId}/blob`
    : `/api/hunter/candidates/${_currentDetailCid}/files/${msgId}/blob`;
  const a = document.createElement('a');
  a.href = url; a.rel = 'noopener';
  document.body.appendChild(a); a.click(); a.remove();
}

async function hfDeleteDownloaded(msgId) {
  if (!confirm(t('hf.deleteConfirm'))) return;
  try {
    if (_currentChannelGid != null) {
      await api(`/api/files/${msgId}/local`, { method: 'DELETE' });
    } else {
      await api(`/api/hunter/candidates/${_currentDetailCid}/files/${msgId}/blob`, { method: 'DELETE' });
    }
  } catch(e) { showToast(`✗ ${esc(e.message || e)}`, 4000); return; }
  _hdDlStatus[msgId] = { state: 'idle' };
  _refreshHdFileRow(msgId);
}

// ── Media preview lightbox ────────────────────────────────────────────────────

async function hfOpenPreview(msgId, fname) {
  const isChannel = _currentChannelGid != null;
  const cid = _currentDetailCid;
  if (!isChannel && !cid) return;
  const overlay = document.getElementById('hf-preview-overlay');
  const spinner = document.getElementById('hf-preview-spinner');
  const content = document.getElementById('hf-preview-content');
  const label   = document.getElementById('hf-preview-label');
  if (!overlay) return;

  // Reset state
  content.innerHTML = '';
  label.textContent = fname || '';
  spinner.classList.remove('hidden');
  overlay.classList.add('open');

  // For channel files, the blob endpoint serves the file directly (already downloaded).
  // For hunter files, use the dedicated preview endpoint.
  const url = isChannel
    ? `/api/files/${msgId}/blob`
    : `/api/hunter/candidates/${cid}/files/${msgId}/preview`;
  const ext = (fname.split('.').pop() || '').toLowerCase();
  // Tarayıcıların güvenle oynatabildiği konteyner/codec'ler.
  // mkv/avi/wmv/flv/ts/mov genelde container-codec uyumsuzluğu nedeniyle
  // oynatılamaz — bu durumda indirilebilir bir fallback gösteriyoruz.
  const browserVideos = ['mp4','webm','m4v','3gp','ogv'];
  const allVideos = ['mp4','mkv','avi','mov','wmv','flv','webm','m4v','ts','3gp','ogv','m2ts'];
  const isVideo = allVideos.includes(ext);

  // Hata mesajını standart bir kapsayıcıyla render edip indirme bağlantısını da ekler.
  const _renderPreviewError = (msg, color = '#f87171') => {
    spinner.classList.add('hidden');
    const dlUrl = isChannel
      ? `/api/files/${msgId}/blob`
      : `/api/hunter/candidates/${cid}/files/${msgId}/blob`;
    content.innerHTML = `
      <div style="color:${color};padding:20px;text-align:center;max-width:520px">
        <div style="margin-bottom:14px">${esc(msg)}</div>
        <a href="${dlUrl}" download="${esc(fname || '')}"
           style="display:inline-block;padding:6px 16px;border-radius:6px;background:var(--accent);color:#fff;text-decoration:none;font-size:.82rem;font-weight:600">
          ${esc(t('hf.previewDownload'))}
        </a>
      </div>`;
  };

  const _diagnoseAndRender = async () => {
    try {
      const r = await fetch(url, { method: 'HEAD' });
      if (r.status === 403) return _renderPreviewError(t('hf.previewNeedsJoin'), '#fbbf24');
      if (r.status === 413) return _renderPreviewError(t('hf.previewTooLarge'), '#fbbf24');
    } catch {}
    _renderPreviewError(t('hf.previewError'));
  };

  if (isVideo) {
    // Tarayıcının zaten oynatamayacağı formatlar için ön kontrol — sunucudan
    // gereksiz yere büyük indirme yapmadan kullanıcıya net mesaj göster.
    if (!browserVideos.includes(ext)) {
      _renderPreviewError(t('hf.previewUnsupported', { ext: ext.toUpperCase() }), '#fbbf24');
      return;
    }
    const video = document.createElement('video');
    video.controls = true;
    video.autoplay = true;
    video.style.cssText = 'max-width:88vw;max-height:82vh;border-radius:6px;background:#000;outline:none';
    video.oncanplay = () => spinner.classList.add('hidden');
    video.onerror   = _diagnoseAndRender;
    video.src = url;
    content.appendChild(video);
  } else {
    const img = document.createElement('img');
    img.style.cssText = 'max-width:88vw;max-height:82vh;border-radius:6px;object-fit:contain;display:block';
    img.onload  = () => spinner.classList.add('hidden');
    img.onerror = _diagnoseAndRender;
    img.src = url;
    content.appendChild(img);
  }
}

function hfClosePreview() {
  const overlay = document.getElementById('hf-preview-overlay');
  if (!overlay) return;
  overlay.classList.remove('open');
  // Stop any playing video to release the connection
  const video = overlay.querySelector('video');
  if (video) { video.pause(); video.src = ''; }
  document.getElementById('hf-preview-content').innerHTML = '';
}

function hfPreviewOverlayClick(e) {
  if (e.target.id === 'hf-preview-overlay') hfClosePreview();
}

function closeHunterDetail() { closeDetailModal(); }  // compat alias

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
  if (e.target.id !== 'hunter-detail-overlay') return;
  // The same overlay is reused for the Channels-tab popup; close whichever
  // mode is active so we don't leave stale state behind.
  if (_currentChannelGid != null) closeChannelDetail();
  else                            closeHunterDetail();
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
  if (r.pending_approval) {
    showToast(
      t('hunter.joinPendingApprovalMsg', { username: esc(c.username) }) ||
      `@${c.username}: katılım isteği gönderildi, admin onayı bekleniyor.`,
      5000,
    );
  } else if (r.queued && r.wait_s > 0) {
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
  if (!bar || !cnt) return;
  const n = S.hunterSelected.size;
  if (n === 0) {
    bar.style.display = 'none';
  } else {
    bar.style.display = 'flex';
    cnt.textContent = t('hbb.selected', {n});
  }
  requestAnimationFrame(_hgUpdateStickyTop);
}

// Recalculate sticky offsets for the hunter grid header.
// Called on tab switch, bulk-bar show/hide, and window resize.
// Toolbar (always sticky at top:0) must be accounted for first;
// bulk bar (conditional) stacks below it.
function _hgUpdateStickyTop() {
  const panel = document.getElementById('hunter-panel');
  if (!panel || panel.style.display === 'none') return;
  const toolbar = panel.querySelector('.hunter-toolbar');
  const bar     = document.getElementById('hunter-bulk-bar');
  const wrap    = document.getElementById('hunter-grid-wrap');
  if (!wrap) return;
  const toolbarH  = toolbar ? toolbar.offsetHeight : 0;
  const barVisible = bar && bar.style.display !== 'none';
  const barH       = barVisible ? bar.offsetHeight : 0;
  // Bulk bar itself must clear the toolbar.
  if (bar) bar.style.top = toolbarH + 'px';
  // Thead must clear toolbar + bulk bar.
  wrap.style.setProperty('--hg-bar-h', (toolbarH + barH) + 'px');
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


// ── Bandwidth Scheduling ─────────────────────────────────────────────────────
let _bwSchedules = [];
let _bwSettings  = { enabled: false, min_size_mb: 0 };
let _bwClockTimer = null;
let _bwEditId = null;
let _bwOpenId = null;

async function loadBandwidthTab() {
  await Promise.all([loadBandwidthSettings(), loadBandwidthSchedules()]);
  bwStartClock();
}

async function loadBandwidthSettings() {
  try {
    _bwSettings = await api('/api/bandwidth/settings');
    const en = document.getElementById('bw-enabled');
    const ms = document.getElementById('bw-min-size');
    if (en) en.checked = !!_bwSettings.enabled;
    if (ms) ms.value = _bwSettings.min_size_mb ?? 0;
  } catch (e) {}
  bwUpdateStatus();
}

async function loadBandwidthSchedules() {
  try {
    _bwSchedules = await api('/api/bandwidth/schedules') || [];
  } catch (e) { _bwSchedules = []; }
  renderBandwidthSchedules();
}

function bwStartClock() {
  if (_bwClockTimer) clearInterval(_bwClockTimer);
  bwTickClock();
  _bwClockTimer = setInterval(bwTickClock, 1000);
}

async function bwTickClock() {
  const el = document.getElementById('bw-clock');
  if (!el) { clearInterval(_bwClockTimer); _bwClockTimer = null; return; }
  const now = new Date();
  el.textContent = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

async function bwUpdateStatus() {
  try {
    const st = await api('/api/bandwidth/status');
    const badge = document.getElementById('bw-status-badge');
    const next  = document.getElementById('bw-next-window');
    if (!badge) return;
    if (!st.enabled) {
      badge.className = 'bw-status-badge bw-status-off';
      badge.textContent = t('bw.statusOff');
      if (next) next.style.display = 'none';
    } else if (st.allowed) {
      badge.className = 'bw-status-badge bw-status-ok';
      badge.textContent = t('bw.statusActive');
      if (next) next.style.display = 'none';
    } else {
      badge.className = 'bw-status-badge bw-status-wait';
      badge.textContent = t('bw.statusWaiting');
      if (next && st.minutes_until_next != null) {
        const h = Math.floor(st.minutes_until_next / 60);
        const m = st.minutes_until_next % 60;
        const str = h > 0 ? `${h} sa ${m} dk` : `${m} dk`;
        next.textContent = t('bw.nextWindow', { time: str });
        next.style.display = '';
      } else if (next) {
        next.style.display = 'none';
      }
    }
  } catch (e) {}
}

async function bwToggleEnabled(checked) {
  _bwSettings.enabled = checked;
  await bwSaveSettings();
  bwUpdateStatus();
}

async function bwSaveSettings() {
  try {
    await api('/api/bandwidth/settings', {
      method: 'PUT',
      json: { enabled: !!_bwSettings.enabled, min_size_mb: parseInt(document.getElementById('bw-min-size')?.value || '0') || 0 },
    });
    bwUpdateStatus();
  } catch (e) { showToast(t('bw.saveError') + ' ' + esc(e.message), 4000); }
}

const _BW_DAYS_ALL = [0, 1, 2, 3, 4, 5, 6];
const _BW_PRESETS = {
  night:     { name: () => t('bw.presetNight'),     rule_type: 'weekly', days: _BW_DAYS_ALL, start_time: '00:00', end_time: '08:00' },
  latenight: { name: () => t('bw.presetLateNight'), rule_type: 'weekly', days: _BW_DAYS_ALL, start_time: '22:00', end_time: '06:00' },
  weekend:   { name: () => t('bw.presetWeekend'),   rule_type: 'weekly', days: [5, 6],       start_time: '00:00', end_time: '23:59' },
  offhours:  { name: () => t('bw.presetOffHours'),  rule_type: 'weekly', days: _BW_DAYS_ALL, start_time: '20:00', end_time: '08:00' },
};

async function bwAddPreset(key) {
  const p = _BW_PRESETS[key];
  if (!p) return;
  try {
    await api('/api/bandwidth/schedules', {
      method: 'POST',
      json: { name: p.name(), rule_type: p.rule_type, days: p.days, start_time: p.start_time, end_time: p.end_time, enabled: true },
    });
    await loadBandwidthSchedules();
    bwUpdateStatus();
    showToast(t('bw.ruleAdded'), 2000);
  } catch (e) { showToast(t('bw.saveError') + ' ' + esc(e.message), 4000); }
}

function _bwDayAbbr(d) {
  return [t('bw.day0'), t('bw.day1'), t('bw.day2'), t('bw.day3'), t('bw.day4'), t('bw.day5'), t('bw.day6')][d] || d;
}

function _bwRuleDesc(s) {
  const days = (s.days || []).map(_bwDayAbbr).join(', ') || '—';
  if (s.rule_type === 'specific_date') return `${s.specific_date} · ${s.start_time}–${s.end_time}`;
  if ((s.days || []).length === 7)     return `${t('bw.everyDay')} · ${s.start_time}–${s.end_time}`;
  if (JSON.stringify((s.days||[]).sort()) === JSON.stringify([5,6])) return `${t('bw.weekends')} · ${s.start_time}–${s.end_time}`;
  return `${days} · ${s.start_time}–${s.end_time}`;
}

function renderBandwidthSchedules() {
  const list = document.getElementById('bw-rules-list');
  if (!list) return;
  if (!_bwSchedules.length) {
    list.innerHTML = `<div style="font-size:.78rem;color:var(--text-4);padding:4px 0">${esc(t('bw.noRules'))}</div>`;
    return;
  }
  list.innerHTML = _bwSchedules.map(s => `
    <div class="bw-rule-item ${s.enabled ? '' : 'bw-rule-disabled'}" id="bw-rule-${s.id}">
      <div class="bw-rule-head" onclick="bwToggleRule(${s.id})">
        <span class="td-chevron" id="bw-chevron-${s.id}">›</span>
        <span class="bw-rule-name">${esc(s.name)}</span>
        <span class="bw-rule-meta">${esc(_bwRuleDesc(s))}</span>
        <div style="display:flex;gap:4px;flex-shrink:0" onclick="event.stopPropagation()">
          <button class="td-btn td-btn-danger" style="font-size:.75rem;padding:3px 9px" onclick="bwDeleteRule(${s.id})" data-i18n="common.delete">Sil</button>
        </div>
      </div>
      <div class="bw-rule-body" id="bw-body-${s.id}" style="display:none"></div>
    </div>`).join('');
}

function bwToggleRule(id) {
  if (_bwOpenId === id) { _bwCloseRuleItem(); return; }
  _bwOpenRuleItem(id);
}

function _bwOpenRuleItem(id) {
  _bwCloseRuleItem();
  const s = id === 'new' ? null : _bwSchedules.find(x => x.id === id);
  if (id !== 'new' && !s) return;
  _bwOpenId = id;
  _bwEditId = s ? id : null;

  if (s) {
    document.getElementById('bw-rule-name').value = s.name || '';
    document.getElementById('bw-rule-type').value = s.rule_type || 'weekly';
    document.getElementById('bw-rule-start').value = s.start_time || '02:00';
    document.getElementById('bw-rule-end').value = s.end_time || '06:00';
    document.getElementById('bw-rule-enabled').checked = !!s.enabled;
    document.getElementById('bw-rule-date').value = s.specific_date || '';
    const days = s.days || [];
    document.querySelectorAll('#bw-days-wrap input[type=checkbox]').forEach(c => {
      c.checked = days.includes(parseInt(c.dataset.day));
    });
  } else {
    document.getElementById('bw-rule-name').value = '';
    document.getElementById('bw-rule-type').value = 'weekly';
    document.getElementById('bw-rule-start').value = '02:00';
    document.getElementById('bw-rule-end').value = '06:00';
    document.getElementById('bw-rule-enabled').checked = true;
    document.getElementById('bw-rule-date').value = '';
    document.querySelectorAll('#bw-days-wrap input[type=checkbox]').forEach(c => { c.checked = true; });
  }
  bwOnRuleTypeChange();

  const body = document.getElementById(`bw-body-${id}`);
  const form = document.getElementById('bw-add-form');
  if (body && form) {
    body.style.display = '';
    body.appendChild(form);
    document.getElementById('bw-rule-name').focus();
  }
  const chevron = document.getElementById(`bw-chevron-${id}`);
  if (chevron) chevron.classList.add('open');
}

function _bwCloseRuleItem() {
  if (_bwOpenId === null) return;
  const prevId = _bwOpenId;
  _bwOpenId = null;
  _bwEditId = null;
  const form = document.getElementById('bw-add-form');
  const container = document.getElementById('bw-form-container');
  if (form && container) container.appendChild(form);
  if (prevId === 'new') {
    const el = document.getElementById('bw-rule-new');
    if (el) el.remove();
  } else {
    const body = document.getElementById(`bw-body-${prevId}`);
    if (body) body.style.display = 'none';
    const chevron = document.getElementById(`bw-chevron-${prevId}`);
    if (chevron) chevron.classList.remove('open');
  }
}

function bwToggleAddMenu(e) {
  if (e) { e.stopPropagation(); e.preventDefault(); }
  const menu = document.getElementById('bw-add-menu');
  if (!menu) return;
  const willOpen = !menu.classList.contains('open');
  menu.classList.toggle('open', willOpen);
  if (willOpen) setTimeout(() => document.addEventListener('click', _bwCloseAddMenuOutside), 0);
  else document.removeEventListener('click', _bwCloseAddMenuOutside);
}

function _bwCloseAddMenuOutside(e) {
  const menu = document.getElementById('bw-add-menu');
  if (!menu) return;
  if (!menu.contains(e.target)) {
    menu.classList.remove('open');
    document.removeEventListener('click', _bwCloseAddMenuOutside);
  }
}

function bwHandleAdd(key) {
  const menu = document.getElementById('bw-add-menu');
  if (menu) menu.classList.remove('open');
  document.removeEventListener('click', _bwCloseAddMenuOutside);
  if (key === 'custom') bwOpenAddRule();
  else bwAddPreset(key);
}

function bwOpenAddRule() {
  _bwCloseRuleItem();
  _bwOpenId = 'new';
  _bwEditId = null;
  const list = document.getElementById('bw-rules-list');
  if (!list) return;
  // clear "no rules" empty state if visible
  if (!_bwSchedules.length) list.innerHTML = '';
  const newItem = document.createElement('div');
  newItem.className = 'bw-rule-item';
  newItem.id = 'bw-rule-new';
  newItem.innerHTML = `
    <div class="bw-rule-head">
      <span class="td-chevron open" id="bw-chevron-new">›</span>
      <span class="bw-rule-name">${esc(t('bw.ruleFormTitle'))}</span>
    </div>
    <div class="bw-rule-body" id="bw-body-new"></div>`;
  list.appendChild(newItem);
  // reset form fields
  document.getElementById('bw-rule-name').value = '';
  document.getElementById('bw-rule-type').value = 'weekly';
  document.getElementById('bw-rule-start').value = '02:00';
  document.getElementById('bw-rule-end').value = '06:00';
  document.getElementById('bw-rule-enabled').checked = true;
  document.getElementById('bw-rule-date').value = '';
  document.querySelectorAll('#bw-days-wrap input[type=checkbox]').forEach(c => { c.checked = true; });
  bwOnRuleTypeChange();
  const body = document.getElementById('bw-body-new');
  const form = document.getElementById('bw-add-form');
  if (body && form) { body.appendChild(form); document.getElementById('bw-rule-name').focus(); }
}

function bwCancelRule() {
  _bwCloseRuleItem();
}

function bwOnRuleTypeChange() {
  const type = document.getElementById('bw-rule-type').value;
  const isWeekly = type === 'weekly';
  document.getElementById('bw-days-row').style.display  = isWeekly ? '' : 'none';
  document.getElementById('bw-days-wrap').style.display = isWeekly ? '' : 'none';
  document.getElementById('bw-date-label').style.display = isWeekly ? 'none' : '';
  document.getElementById('bw-rule-date').style.display  = isWeekly ? 'none' : '';
}

async function bwSaveRule() {
  const name = document.getElementById('bw-rule-name').value.trim();
  if (!name) { showToast(t('bw.errNameRequired')); return; }
  const type = document.getElementById('bw-rule-type').value;
  const days = type === 'weekly'
    ? [...document.querySelectorAll('#bw-days-wrap input[type=checkbox]')]
        .filter(c => c.checked).map(c => parseInt(c.dataset.day))
    : [];
  const body = {
    name,
    rule_type: type,
    days,
    start_time: document.getElementById('bw-rule-start').value,
    end_time:   document.getElementById('bw-rule-end').value,
    specific_date: type === 'specific_date' ? (document.getElementById('bw-rule-date').value || null) : null,
    enabled: document.getElementById('bw-rule-enabled').checked,
  };
  if (type === 'specific_date' && !body.specific_date) { showToast(t('bw.errDateRequired')); return; }
  try {
    if (_bwEditId) {
      await api(`/api/bandwidth/schedules/${_bwEditId}`, { method: 'PUT', json: body });
    } else {
      await api('/api/bandwidth/schedules', { method: 'POST', json: body });
    }
    bwCancelRule();
    await loadBandwidthSchedules();
    bwUpdateStatus();
    showToast(t('bw.ruleSaved'), 2000);
  } catch (e) { showToast(t('bw.saveError') + ' ' + esc(e.message), 4000); }
}

async function bwDeleteRule(id) {
  if (!confirm(t('bw.deleteConfirm'))) return;
  try {
    await api(`/api/bandwidth/schedules/${id}`, { method: 'DELETE' });
    await loadBandwidthSchedules();
    bwUpdateStatus();
    showToast(t('bw.ruleDeleted'), 2000);
  } catch (e) { showToast(t('bw.saveError') + ' ' + esc(e.message), 4000); }
}

async function cancelScheduledDownload(fileId) {
  try {
    await api(`/api/downloads/scheduled/${fileId}`, { method: 'DELETE' });
    _scheduledDownloads = _scheduledDownloads.filter(s => s.file_id !== fileId);
    renderDownloadsTab();
  } catch (e) { showToast(t('dl.cancelFail') + ' ' + esc(e.message)); }
}
