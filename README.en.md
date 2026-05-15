<p align="center">
  <img src="docs/banner.png" alt="TelFiles — Telegram file and link indexer" width="100%">
</p>

<p align="center">
  <a href="README.md">🇹🇷 Türkçe</a> &nbsp;|&nbsp;
  <a href="README.en.md">🇬🇧 English</a> &nbsp;|&nbsp;
  <a href="README.de.md">🇩🇪 Deutsch</a> &nbsp;|&nbsp;
  <a href="README.ru.md">🇷🇺 Русский</a> &nbsp;|&nbsp;
  <a href="README.zh.md">🇨🇳 中文</a>
</p>

# TelFiles

Uses **your own Telegram account** to crawl groups and channels you have joined in the background; indexes every file and every link it encounters into a local PostgreSQL database. Search, sort, filter, and download anything with a single click from one browser interface.

Bonus: **Channel Hunter** — discovers new file-rich channels, scores them, and surfaces the best ones.

```bash
curl -fsSL https://raw.githubusercontent.com/enseitankado/telfiles/main/install.sh | bash
```

> Debian / Ubuntu / Kali / Pardus / Mint. One line; installs Docker if missing, brings up the containers, and prints the access URL.

---

## ✨ Highlights

- **Multi-account** — combines multiple Telegram accounts into a single view.
- **Full archive access** — paginates through history and captures new messages in real time.
- **Separate grids for Files & Links** — per-column sorting + filters, narrow by channel / type / size / date.
- **Channel Hunter** — 3-stage discovery: (1) mining from internal links, (2) 22 web sources (TGStat, Telemetr.io, Combot, t-do.ru, telega.io + 8 search engines + Reddit / HN / GitHub), (3) enrichment & scoring with sample messages from Telegram.
- **Try before you commit** — preview and download a specific file from a candidate channel **without joining**; only performs "temp-join → download → leave" when you explicitly approve.
- **Watch keywords** — define term sets like `invoice 2025`; a notification is created when a matching file arrives (AND logic, filename-based).
- **Anonymous telemetry** — optional; only channel username + member count + file count. No messages, IPs, or identities. One click to disable.
- **5 languages** — Türkçe, English, Deutsch, Русский, 中文.
- **Single `up -d`** — Docker Compose. Data lives in host volumes; deleting the container leaves your data intact.

---

## 📸 Screenshots

<table>
<tr>
<td width="50%"><a href="docs/screenshots/en/02-files.png"><img src="docs/screenshots/en/02-files.png" alt="Files"></a><br><b>📁 Files</b> — unified search across all accounts, type categories, channel filter, size slider.</td>
<td width="50%"><a href="docs/screenshots/en/03-hunter.png"><img src="docs/screenshots/en/03-hunter.png" alt="Channel Hunter"></a><br><b>📡 Channel Hunter</b> — discovery pipeline, per-column sorting, file preview in the detail lightbox.</td>
</tr>
<tr>
<td><a href="docs/screenshots/en/04-links.png"><img src="docs/screenshots/en/04-links.png" alt="Links"></a><br><b>🔗 Links</b> — URLs parsed from Google Drive / Mega / MediaFire etc., with accessibility checks.</td>
<td><a href="docs/screenshots/en/06-status.png"><img src="docs/screenshots/en/06-status.png" alt="Status"></a><br><b>📊 Status</b> — sync metrics, file type distribution, platform-based link stats, RAM / disk usage.</td>
</tr>
<tr>
<td colspan="2" align="center"><a href="docs/screenshots/en/05-settings.png"><img src="docs/screenshots/en/05-settings.png" alt="Settings" width="72%"></a><br><b>⚙️ Settings</b> — group management, watch keywords, language & theme, password.</td>
</tr>
</table>

---

## 🚀 Quick start

**Requirements:** Debian-based Linux + `API_ID` & `API_HASH` from [my.telegram.org](https://my.telegram.org).

```bash
# 1) One-liner install
curl -fsSL https://raw.githubusercontent.com/enseitankado/telfiles/main/install.sh | bash

# 2) Scripted (CI / pre-configured env)
TELEGRAM_API_ID=12345 TELEGRAM_API_HASH=abcdef… NONINTERACTIVE=1 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/enseitankado/telfiles/main/install.sh)"

# 3) Manual
git clone https://github.com/enseitankado/telfiles.git && cd telfiles
cp .env.example .env && $EDITOR .env       # API_ID + API_HASH
docker compose up -d --build
```

The access URL is printed to the terminal (default: `http://<host>:8765`). If the port is taken the installer automatically picks the next available one.

### First login — two steps

1. **Interface password** — log in with `admin`, then change it under **Settings → Account → Interface Password**.
2. **Telegram account** — Settings → Account → ➕ Add Account → phone → code sent to Telegram → (if enabled) 2FA. Scanning starts automatically once connected.

> If `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` are empty, "Send code" will not work. Fill in `.env` and run `docker compose restart telfiles-app`.

### Updating

Run the same install command again. The installer updates itself, pulls the latest code, rebuilds the container; **`data/` and `pgdata/` are preserved**.

On startup the app checks the HEAD on GitHub and notifies you in the UI if a new version is available.

---

## ⚙️ Configuration

| Location | Contents | Reset |
|---|---|---|
| `data/ui_auth.json` | UI password hash + session tokens | delete → reverts to `admin` |
| `data/credentials.json` | Telegram API credentials (takes precedence over env) | delete → falls back to `.env` |
| `data/settings.json` | `sync_interval_seconds` (clamped to `[900, 86400]`) | delete → 7200s |
| `data/accounts/{id}/telfiles.session` | Telethon account session | delete → re-login required for that account |
| `data/hunter_events.jsonl` | Hunter detail log (restart-safe) | delete → log cleared |
| `downloads/` | Downloaded files (`<group>/...` and `_hunter/<channel>/...`) | each file can be deleted independently |
| `pgdata/` | PostgreSQL main database | do not delete |

### Environment variables (`.env`)

| Variable | Required | Note |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | my.telegram.org → API Development Tools |
| `TELEGRAM_API_HASH` | ✅ | same page |
| `TELEMETRY_SECRET` | ❌ | Only if you run your own telemetry server |

---

## 🧱 Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12 · FastAPI · Uvicorn · asyncio |
| Telegram | [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto) |
| Data | PostgreSQL 16 · asyncpg |
| Web scraping | aiohttp + [CloakBrowser](https://github.com/cloakbrowser) (stealth Chromium, Stage 2) |
| Frontend | Vanilla JS · CSS · HTML (no build step) |
| Deployment | Docker Compose |

Container image **~302 MB**. All runtime state is in host volumes.

---

## 🗂️ Project structure

```
app/
├── main.py              # FastAPI + endpoints + 4 background loops
├── database.py          # asyncpg data layer + schema migrations
├── telegram_client.py   # Multi-account Telethon management
├── sync.py              # History + realtime message scanner
├── hunter.py            # Channel Hunter pipeline + per-file download
├── link_prober.py       # Link accessibility checker
├── telemetry.py         # Anonymous stats sender
├── ui_auth.py           # Web password + session
└── static/              # index.html, app.js, i18n.js — single-page UI

docs/
├── banner.png           # README header
├── screenshots/         # UI screenshots (language folders: tr/en/de/ru/zh)
└── OPERATOR.md          # DB queries, troubleshooting, hunter sources
```

---

## 🛠️ Development

```bash
# Backend (Python) change → rebuild required
docker compose up -d --build telfiles-app

# Frontend (HTML/JS/CSS) → bind-mount; just refresh the browser
# app/static/* is served live from the host

# Logs / DB
docker logs -f telfiles-app
docker exec -it telfiles-postgres psql -U telfiles -d telfiles
```

More: [docs/OPERATOR.md](docs/OPERATOR.md) — DB queries, hunter source list, common issue → fix table.

---

## 🔒 Privacy & Telemetry

When enabled, **once every 24 hours**, only these three fields are sent:

- The **username** of channels you have joined (already public Telegram info)
- Each channel's **member count** (also public)
- The **number of files** you have indexed from that channel

**Never sent:** messages, filenames, file contents, phone number, account details, IP.

Identifier: a random UUID generated locally at install time. To disable: Settings → Account → uncheck "Send usage statistics".

To use your own receiver endpoint, change `ENDPOINT_URL` in `app/telemetry.py`.

---

## 🤝 Issues & contributions

Via [GitHub Issues](https://github.com/enseitankado/telfiles/issues).

---

## ⚖️ License

This project is open source; all rights reserved by the author until a license file is added. Please get in touch for fork / modification / redistribution.

---

## ⚠️ Disclaimer

TelFiles only indexes content you **already have access to via your own Telegram account**. Complying with Telegram's [Terms of Service](https://telegram.org/tos) is the user's responsibility. The author(s) are not liable for any consequences arising from misuse of this tool.
