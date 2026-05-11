#!/usr/bin/env bash
# TelFiles tek-adım kurulum betiği — Debian/Ubuntu/Kali tabanlı sistemler.
#
# Kullanım:
#   curl -fsSL https://raw.githubusercontent.com/enseitankado/telfiles/main/install.sh | bash
#   # ya da klon edilmiş proje kökünde:
#   ./install.sh
#
# Ortam değişkenleri (opsiyonel):
#   TELEGRAM_API_ID, TELEGRAM_API_HASH   — interaktif sorulara cevap vermeden kurmak için
#   TELFILES_DIR                         — kurulum dizini (varsayılan: ./telfiles)
#   TELFILES_PORT                        — host portu (varsayılan: 8765)
#   TELFILES_BRANCH                      — git branch (varsayılan: main)
#   NONINTERACTIVE=1                     — soru sormaz; boş .env ile devam eder

set -euo pipefail

# ── Renkli log ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[34m'
  C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_R= C_G= C_Y= C_B= C_DIM= C_BOLD= C_RST=
fi
log()  { printf "%s==>%s %s\n" "$C_B" "$C_RST" "$*"; }
ok()   { printf "%s ✓%s %s\n"  "$C_G" "$C_RST" "$*"; }
warn() { printf "%s ! %s %s\n" "$C_Y" "$C_RST" "$*" >&2; }
err()  { printf "%s ✗%s %s\n"  "$C_R" "$C_RST" "$*" >&2; }
die()  { err "$*"; exit 1; }

# ── Sabitler ──────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/enseitankado/telfiles.git"
TELFILES_DIR="${TELFILES_DIR:-telfiles}"
TELFILES_PORT="${TELFILES_PORT:-8765}"
TELFILES_BRANCH="${TELFILES_BRANCH:-main}"
NONINTERACTIVE="${NONINTERACTIVE:-0}"

# ── Dil algılama ──────────────────────────────────────────────────────────────
# Sistemin locale ayarına bakar: tr_* ise Türkçe, aksi halde İngilizce. Manuel
# override için: TELFILES_LANG=tr veya TELFILES_LANG=en olarak çalıştırın.
_loc="${TELFILES_LANG:-${LC_ALL:-${LC_MESSAGES:-${LANG:-en_US.UTF-8}}}}"
case "${_loc%%.*}" in
  tr|tr_*) TF_LANG=tr ;;
  *)       TF_LANG=en ;;
esac

# msg <key> [args] — printf-tarzı %s argümanları destekler.
msg() {
  local _k="$1"; shift
  local _v=""
  case "$TF_LANG:$_k" in
    # ── Hatalar ──
    tr:linux_only) _v="Bu betik Linux üzerinde çalışmak için tasarlandı." ;;
    en:linux_only) _v="This installer is intended for Linux." ;;
    tr:apt_needed) _v="apt-get bulunamadı — yalnızca Debian tabanlı dağıtımlar (Debian/Ubuntu/Kali/Mint) desteklenir." ;;
    en:apt_needed) _v="apt-get not found — only Debian-based distributions (Debian/Ubuntu/Kali/Mint) are supported." ;;
    tr:sudo_prompt) _v="sudo gerektiren adımlar için parola istenebilir." ;;
    en:sudo_prompt) _v="Steps that need root may prompt for your sudo password." ;;
    tr:need_sudo) _v="Root değilsiniz ve sudo da yüklü değil. Root olarak çalıştırın veya sudo kurun." ;;
    en:need_sudo) _v="Not running as root and sudo is not installed. Run as root or install sudo first." ;;
    # ── apt / kaynaklar ──
    tr:bad_docker_list) _v="Bozuk docker.list bulundu (codename=%s) — askıya alınıyor." ;;
    en:bad_docker_list) _v="Stale docker.list found (codename=%s) — suspending it." ;;
    tr:bad_apt_src) _v="Bozuk apt kaynağı askıya alındı: %s (içeriği: %s)" ;;
    en:bad_apt_src) _v="Suspended broken apt source: %s (URL: %s)" ;;
    tr:apt_cleaned_retry) _v="Bozuk kaynaklar temizlendi, apt-get update tekrar deneniyor…" ;;
    en:apt_cleaned_retry) _v="Broken sources cleaned up; retrying apt-get update…" ;;
    tr:apt_updating) _v="Sistem paket dizini güncelleniyor…" ;;
    en:apt_updating) _v="Updating system package index…" ;;
    tr:apt_failed) _v="apt-get update başarısız. Yukarıdaki hatalara bakın; bozuk depo dosyaları /etc/apt/sources.list.d/ altında olabilir." ;;
    en:apt_failed) _v="apt-get update failed. Check the errors above; broken repo files may exist under /etc/apt/sources.list.d/." ;;
    tr:installing_base) _v="Temel araçlar kuruluyor (curl, git, ca-certificates, gnupg)…" ;;
    en:installing_base) _v="Installing base tools (curl, git, ca-certificates, gnupg)…" ;;
    tr:base_ready) _v="Temel araçlar hazır." ;;
    en:base_ready) _v="Base tools ready." ;;
    # ── Docker ──
    tr:docker_no_compose) _v="Docker var ama 'docker compose' plugin'i yok — kurulum yapılacak." ;;
    en:docker_no_compose) _v="Docker is present but 'docker compose' plugin is missing — installing it." ;;
    tr:installing_docker) _v="Docker Engine + Compose plugin kuruluyor (docker.com resmi repo)…" ;;
    en:installing_docker) _v="Installing Docker Engine + Compose plugin (docker.com official repo)…" ;;
    tr:unknown_dist) _v="Bilinmeyen dağıtım: %s — Debian repo'su deneniyor." ;;
    en:unknown_dist) _v="Unknown distribution: %s — trying the Debian repo." ;;
    tr:dist_detected) _v="Algılanan dağıtım: %s → %s/%s" ;;
    en:dist_detected) _v="Detected distribution: %s → %s/%s" ;;
    tr:codename_missing) _v="Docker repo'sunda %s/%s yok — yedek codename'ler deneniyor." ;;
    en:codename_missing) _v="Docker repo has no %s/%s — trying fallback codenames." ;;
    tr:using_fallback) _v="Yedek codename kullanılıyor: %s/%s" ;;
    en:using_fallback) _v="Using fallback codename: %s/%s" ;;
    tr:no_codename) _v="Docker resmi repo'sunda uyumlu codename bulunamadı (%s). Manuel kurulum için: https://docs.docker.com/engine/install/" ;;
    en:no_codename) _v="No compatible codename in Docker's official repo (%s). Manual install: https://docs.docker.com/engine/install/" ;;
    tr:docker_ce_failed) _v="apt-get update başarısız — Docker resmi repo satırı kaldırılıp dağıtımın docker.io paketi deneniyor." ;;
    en:docker_ce_failed) _v="apt-get update failed — removing Docker's official repo line and trying the distribution's docker.io package." ;;
    tr:apt_retry_failed) _v="apt-get update tekrar başarısız oldu." ;;
    en:apt_retry_failed) _v="apt-get update failed again." ;;
    tr:docker_failed) _v="Docker kurulumu başarısız. Manuel kurulum: https://docs.docker.com/engine/install/" ;;
    en:docker_failed) _v="Docker installation failed. Manual install: https://docs.docker.com/engine/install/" ;;
    tr:docker_failed_short) _v="Docker kurulumu başarısız." ;;
    en:docker_failed_short) _v="Docker installation failed." ;;
    tr:docker_ce_fallback) _v="docker-ce paketleri kurulamadı — dağıtımın docker.io paketine düşülüyor." ;;
    en:docker_ce_fallback) _v="docker-ce packages couldn't be installed — falling back to the distribution's docker.io." ;;
    tr:docker_group_hint) _v="Mevcut shell'de docker grubu henüz aktif değil. Yeni oturumda sudo'suz 'docker' kullanılabilir." ;;
    en:docker_group_hint) _v="The docker group isn't active in this shell yet. After a fresh login you can run docker without sudo." ;;
    tr:docker_installed) _v="Docker %s kuruldu." ;;
    en:docker_installed) _v="Docker %s installed." ;;
    tr:docker_present) _v="Docker zaten yüklü: %s." ;;
    en:docker_present) _v="Docker already installed: %s." ;;
    tr:starting_docker) _v="Docker daemon başlatılıyor…" ;;
    en:starting_docker) _v="Starting Docker daemon…" ;;
    tr:docker_start_failed) _v="Docker daemon başlatılamadı. 'sudo systemctl status docker' ile bakın." ;;
    en:docker_start_failed) _v="Couldn't start Docker daemon. Check 'sudo systemctl status docker'." ;;
    # ── Repo / .env ──
    tr:existing_update) _v="Mevcut TelFiles kopyası bulundu: %s — güncelleniyor…" ;;
    en:existing_update) _v="Existing TelFiles clone at %s — updating…" ;;
    tr:in_project_root) _v="Mevcut proje kökünde çalışıyor: %s" ;;
    en:in_project_root) _v="Running from the existing project root: %s" ;;
    tr:cloning) _v="TelFiles deposu klonlanıyor → %s" ;;
    en:cloning) _v="Cloning TelFiles repository → %s" ;;
    tr:working_dir) _v="Çalışma dizini: %s" ;;
    en:working_dir) _v="Working directory: %s" ;;
    tr:api_blank_warn) _v="TELEGRAM_API_ID / TELEGRAM_API_HASH boş. Servis çalışacak ama hesap eklenemeyecek." ;;
    en:api_blank_warn) _v="TELEGRAM_API_ID / TELEGRAM_API_HASH are blank. The service will start but no account is bound yet." ;;
    tr:api_blank_hint) _v="Bilgileri kurulum sonrası web arayüzünden girebilirsiniz." ;;
    en:api_blank_hint) _v="You can enter these from the web UI after installation." ;;
    tr:env_updated) _v=".env güncellendi." ;;
    en:env_updated) _v=".env updated." ;;
    tr:creds_prompt_title) _v="Telegram API kimlik bilgileri" ;;
    en:creds_prompt_title) _v="Telegram API credentials" ;;
    tr:creds_prompt_skip) _v="(Şimdi boş bırakıp sonra web arayüzünden girebilirsiniz.)" ;;
    en:creds_prompt_skip) _v="(Leave blank now; you can enter them in the web UI later.)" ;;
    # ── Port ──
    tr:compose_port_detected) _v="docker-compose.yml host port'u %s olarak algılandı." ;;
    en:compose_port_detected) _v="Host port in docker-compose.yml detected as %s." ;;
    tr:port_busy_check) _v="%s portu zaten kullanımda — eski TelFiles container'ı mı diye bakılıyor." ;;
    en:port_busy_check) _v="Port %s is busy — checking whether it's a previous TelFiles container." ;;
    tr:stopping_old) _v="Eski telfiles-app container'ı durduruluyor (sağlıklı yeniden başlatma için)…" ;;
    en:stopping_old) _v="Stopping the previous telfiles-app container (for a clean restart)…" ;;
    tr:removing_orphan) _v="telfiles-app adındaki eski container kaldırılıyor…" ;;
    en:removing_orphan) _v="Removing the orphaned container named telfiles-app…" ;;
    tr:port_freed) _v="%s portu serbest bırakıldı; orijinal port korunuyor." ;;
    en:port_freed) _v="Port %s released; keeping the original port." ;;
    tr:port_held_other) _v="Port hâlâ bizim olmayan başka bir servis tarafından kullanılıyor." ;;
    en:port_held_other) _v="Port is still held by an external service." ;;
    tr:port_swapped) _v="Port → %s olarak değiştirildi." ;;
    en:port_swapped) _v="Port changed to %s." ;;
    # ── Build / up ──
    tr:building) _v="Container'lar inşa ediliyor (ilk kurulumda 2-5 dk sürebilir)…" ;;
    en:building) _v="Building containers (2–5 min on first install)…" ;;
    tr:starting_service) _v="Servis başlatılıyor…" ;;
    en:starting_service) _v="Starting the service…" ;;
    tr:containers_up) _v="Containerlar ayakta." ;;
    en:containers_up) _v="Containers are up." ;;
    tr:waiting_app) _v="Uygulamanın açılması bekleniyor…" ;;
    en:waiting_app) _v="Waiting for the application to come up…" ;;
    *) _v="$_k" ;;
  esac
  # shellcheck disable=SC2059
  printf -- "$_v" "$@"
}

# ── Ön kontroller ─────────────────────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || die "$(msg linux_only)"

if ! command -v apt-get >/dev/null 2>&1; then
  die "$(msg apt_needed)"
fi

# Sudo'yu root değilse zorla. Root isek sudo'suz çalış.
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  SUDO=""
else
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
    log "$(msg sudo_prompt)"
  else
    die "$(msg need_sudo)"
  fi
fi

# Interaktif soru sormak için TTY bul (curl | bash durumunda stdin pipe).
if [ -e /dev/tty ] && [ "$NONINTERACTIVE" != "1" ]; then
  TTY=/dev/tty
else
  TTY=""
fi

ask() {
  # ask <var_name> <prompt>
  local _var="$1" _prompt="$2" _val=""
  if [ -z "$TTY" ]; then return 0; fi
  printf "%s" "$_prompt" >/dev/tty
  IFS= read -r _val <"$TTY" || true
  if [ -n "$_val" ]; then printf -v "$_var" '%s' "$_val"; fi
}

# ── 1) Sistem bağımlılıkları ──────────────────────────────────────────────────

# Bir önceki başarısız kurulumdan kalmış bozuk docker.list dosyaları varsa
# (örneğin Pardus'ta upstream Debian codename'i yerine 'etap-yirmiuc' yazılmış),
# apt-get update bu satır yüzünden 404 alıp betiği daha başlangıçta çökertir.
# Docker repo satırından (host, repo_dist, codename) üçlüsünü çıkarıp Release
# URL'sini probe ediyoruz; 200 dönmüyorsa dosyayı askıya alıyoruz. Docker
# kurulum aşamasında doğrusu zaten yeniden yazılacak.
if [ -f /etc/apt/sources.list.d/docker.list ]; then
  # `[...]` opsiyonlarını at, sonra `deb URL CODENAME` formatından field'ları çıkar.
  _docker_url="$(sed 's/\[[^]]*\]//' /etc/apt/sources.list.d/docker.list | awk '/^deb /{print $2; exit}')"
  _docker_codename="$(sed 's/\[[^]]*\]//' /etc/apt/sources.list.d/docker.list | awk '/^deb /{print $3; exit}')"
  if [ -n "$_docker_url" ] && [ -n "$_docker_codename" ]; then
    if ! curl -fsIL --max-time 8 \
        "${_docker_url%/}/dists/${_docker_codename}/Release" >/dev/null 2>&1; then
      warn "$(msg bad_docker_list "$_docker_codename")"
      $SUDO mv /etc/apt/sources.list.d/docker.list \
                "/etc/apt/sources.list.d/docker.list.broken.$(date +%s)" \
        2>/dev/null || $SUDO rm -f /etc/apt/sources.list.d/docker.list
    fi
  fi
  unset _docker_url _docker_codename
fi

# apt-get update'i dirençli yap: ilk başarısızlıkta, çıktı içinde 404/NoRelease
# hatası veren üçüncü-taraf sources.list.d/* dosyalarını yedekleyip tekrar dener.
_apt_update() {
  local out rc urls
  # -qq locale'e bağlı olarak W: satırlarını bastırabilir; verbose çalışıp
  # çıktıdan URL'leri çıkarmamız gerek.
  out="$($SUDO apt-get update 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then return 0; fi

  # Çıktıdaki tüm http/https URL'lerini topla. Hem W: satırlarındaki tam
  # `/dists/codename/Release` formunu, hem E: satırlarındaki `'<url> <codename>
  # Release'` formunu yakalamak için.
  urls="$(printf '%s\n' "$out" \
    | grep -oE "https?://[^ ']+" \
    | sed -E 's|/dists/[^/]+/Release$||; s|/?$||' \
    | sort -u || true)"

  local cleaned=0
  if [ -n "$urls" ]; then
    local url f
    while IFS= read -r url; do
      [ -n "$url" ] || continue
      # Bu URL'yi (prefix olarak) içeren her sources.list.d dosyasını askıya al.
      for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
        [ -f "$f" ] || continue
        if grep -qF "$url" "$f" 2>/dev/null; then
          warn "$(msg bad_apt_src "$f" "$url")"
          $SUDO mv "$f" "${f}.broken.$(date +%s)" 2>/dev/null || true
          cleaned=1
        fi
      done
    done <<< "$urls"
  fi

  if [ $cleaned -eq 1 ]; then
    log "$(msg apt_cleaned_retry)"
    $SUDO apt-get update -qq && return 0
  fi
  printf '%s\n' "$out" >&2
  return 1
}

log "$(msg apt_updating)"
_apt_update || die "$(msg apt_failed)"

log "$(msg installing_base)"
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ca-certificates curl git gnupg lsb-release iproute2 >/dev/null
ok "$(msg base_ready)"

# ── 2) Docker + Compose plugin ───────────────────────────────────────────────
need_docker_install=0
if ! command -v docker >/dev/null 2>&1; then
  need_docker_install=1
elif ! docker compose version >/dev/null 2>&1; then
  warn "$(msg docker_no_compose)"
  need_docker_install=1
fi

if [ "$need_docker_install" -eq 1 ]; then
  log "$(msg installing_docker)"

  # /etc/debian_version değerinden upstream Debian kod adına eşle — Pardus, Kali,
  # Deepin gibi türevlerde VERSION_CODENAME Debian'a uymaz, ama Debian sürüm
  # numarası uyar.
  _debian_base_codename() {
    local v
    [ -f /etc/debian_version ] || { echo ""; return; }
    v="$(cat /etc/debian_version 2>/dev/null)"
    case "$v" in
      13*|trixie*|*sid*)        echo "trixie";;
      12*|bookworm*)            echo "bookworm";;
      11*|bullseye*)            echo "bullseye";;
      10*|buster*)              echo "buster";;
      *)                        echo "";;
    esac
  }

  # Verilen (repo_dist, codename) çiftiyle Docker repo'sunda Release dosyası
  # var mı? 0 = var, 1 = yok.
  _docker_repo_ok() {
    local d="$1" cn="$2" code
    code="$(curl -sIL -o /dev/null -w '%{http_code}' \
              "https://download.docker.com/linux/${d}/dists/${cn}/Release" \
              --max-time 8 2>/dev/null || echo 000)"
    [ "$code" = "200" ]
  }

  source /etc/os-release
  DIST_ID="${ID:-unknown}"
  DIST_LIKE="${ID_LIKE:-}"
  DIST_CODENAME="${VERSION_CODENAME:-}"

  case "$DIST_ID" in
    ubuntu)
      DOCKER_REPO_DIST=ubuntu
      ;;
    debian|raspbian)
      DOCKER_REPO_DIST=debian
      ;;
    kali)
      DOCKER_REPO_DIST=debian
      DIST_CODENAME="$(_debian_base_codename)"
      [ -n "$DIST_CODENAME" ] || DIST_CODENAME=bookworm
      ;;
    linuxmint|elementary|pop|zorin|neon)
      DOCKER_REPO_DIST=ubuntu
      DIST_CODENAME="${UBUNTU_CODENAME:-jammy}"
      ;;
    pardus|deepin|mx|parrot|devuan|antix)
      # Debian türevi. Upstream Debian sürümünden codename'i çıkar.
      DOCKER_REPO_DIST=debian
      DIST_CODENAME="$(_debian_base_codename)"
      [ -n "$DIST_CODENAME" ] || DIST_CODENAME=bookworm
      ;;
    *)
      # ID_LIKE alanına bak — "debian" veya "ubuntu" geçiyorsa ona göre.
      if echo " $DIST_LIKE " | grep -q "ubuntu"; then
        DOCKER_REPO_DIST=ubuntu
        DIST_CODENAME="${UBUNTU_CODENAME:-${DIST_CODENAME:-jammy}}"
      else
        warn "$(msg unknown_dist "$DIST_ID")"
        DOCKER_REPO_DIST=debian
        DIST_CODENAME="$(_debian_base_codename)"
        [ -n "$DIST_CODENAME" ] || DIST_CODENAME=bookworm
      fi
      ;;
  esac

  log "$(msg dist_detected "$DIST_ID" "$DOCKER_REPO_DIST" "$DIST_CODENAME")"

  # Docker'da bu codename gerçekten var mı? Yoksa stable fallback'lerini sırayla
  # dene (bookworm, bullseye, jammy, focal).
  if ! _docker_repo_ok "$DOCKER_REPO_DIST" "$DIST_CODENAME"; then
    warn "$(msg codename_missing "$DOCKER_REPO_DIST" "$DIST_CODENAME")"
    _fallback_ok=0
    if [ "$DOCKER_REPO_DIST" = "ubuntu" ]; then
      _fallbacks="jammy focal noble"
    else
      _fallbacks="bookworm bullseye trixie"
    fi
    for _cn in $_fallbacks; do
      if _docker_repo_ok "$DOCKER_REPO_DIST" "$_cn"; then
        DIST_CODENAME="$_cn"
        ok "$(msg using_fallback "$DOCKER_REPO_DIST" "$DIST_CODENAME")"
        _fallback_ok=1
        break
      fi
    done
    [ "$_fallback_ok" -eq 1 ] || die "$(msg no_codename "$DOCKER_REPO_DIST")"
  fi

  $SUDO install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${DOCKER_REPO_DIST}/gpg" \
    | $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg

  ARCH="$(dpkg --print-architecture)"
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${DOCKER_REPO_DIST} ${DIST_CODENAME} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null

  if ! _apt_update; then
    warn "$(msg docker_ce_failed)"
    $SUDO rm -f /etc/apt/sources.list.d/docker.list
    _apt_update || die "$(msg apt_retry_failed)"
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null \
      || $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose >/dev/null \
      || die "$(msg docker_failed)"
  else
    if ! $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
           docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1; then
      warn "$(msg docker_ce_fallback)"
      $SUDO rm -f /etc/apt/sources.list.d/docker.list
      _apt_update
      $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null \
        || $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose >/dev/null \
        || die "$(msg docker_failed_short)"
    fi
  fi

  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true

  # Kullanıcıyı docker grubuna ekle — yeni shell'de sudo'suz kullanım için.
  if [ -n "${SUDO_USER:-${USER:-}}" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
    $SUDO usermod -aG docker "${SUDO_USER:-$USER}" 2>/dev/null || true
    warn "$(msg docker_group_hint)"
  fi

  ok "$(msg docker_installed "$(docker --version | awk '{print $3}' | tr -d ',')")"
else
  ok "$(msg docker_present "$(docker --version | awk '{print $3}' | tr -d ',')")"
fi

# Docker daemon erişimi
if ! $SUDO docker info >/dev/null 2>&1; then
  log "$(msg starting_docker)"
  $SUDO systemctl start docker || die "$(msg docker_start_failed)"
fi

# Kullanıcı docker grubunda mı? Değilse sonraki docker komutlarını sudo ile çalıştır.
if [ "${EUID:-$(id -u)}" -ne 0 ] && ! groups | grep -qE '(^| )docker( |$)'; then
  DOCKER_CMD="$SUDO docker"
else
  DOCKER_CMD="docker"
fi

# ── 3) Repo'yu çek veya güncelle ─────────────────────────────────────────────
if [ -d "$TELFILES_DIR/.git" ]; then
  log "$(msg existing_update "$TELFILES_DIR")"
  git -C "$TELFILES_DIR" fetch --depth=1 origin "$TELFILES_BRANCH"
  git -C "$TELFILES_DIR" reset --hard "origin/$TELFILES_BRANCH"
elif [ -f "docker-compose.yml" ] && [ -d "app" ]; then
  # Halihazırda proje kökündeyiz (örn. ./install.sh).
  TELFILES_DIR="."
  log "$(msg in_project_root "$(pwd)")"
else
  log "$(msg cloning "$TELFILES_DIR")"
  git clone --depth=1 --branch "$TELFILES_BRANCH" "$REPO_URL" "$TELFILES_DIR"
fi

cd "$TELFILES_DIR"
ok "$(msg working_dir "$(pwd)")"

# ── 4) .env hazırla ──────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env 2>/dev/null || cat > .env <<'EOF'
# Get these from https://my.telegram.org → API Development Tools
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
EOF
fi

api_id="${TELEGRAM_API_ID:-}"
api_hash="${TELEGRAM_API_HASH:-}"

# Mevcut .env içeriğini oku
if [ -z "$api_id" ]; then
  api_id="$(grep -E '^TELEGRAM_API_ID=' .env | head -n1 | cut -d= -f2-)"
fi
if [ -z "$api_hash" ]; then
  api_hash="$(grep -E '^TELEGRAM_API_HASH=' .env | head -n1 | cut -d= -f2-)"
fi

if [ -z "$api_id" ] || [ -z "$api_hash" ]; then
  if [ -n "$TTY" ]; then
    printf "\n%s%s%s — https://my.telegram.org → API Development Tools\n" "$C_BOLD" "$(msg creds_prompt_title)" "$C_RST" >/dev/tty
    printf "%s%s%s\n\n" "$C_DIM" "$(msg creds_prompt_skip)" "$C_RST" >/dev/tty
    ask api_id  "TELEGRAM_API_ID    : "
    ask api_hash "TELEGRAM_API_HASH  : "
  else
    warn "$(msg api_blank_warn)"
    warn "$(msg api_blank_hint)"
  fi
fi

# .env dosyasını yaz/güncelle
{
  echo "# Get these from https://my.telegram.org → API Development Tools"
  echo "TELEGRAM_API_ID=$api_id"
  echo "TELEGRAM_API_HASH=$api_hash"
} > .env
ok "$(msg env_updated)"

# ── 5) Port çakışmasını çöz ──────────────────────────────────────────────────
# Çoğu zaman porttaki "çakışma" aslında bir önceki TelFiles container'ı:
# update sırasında eski sürüm hâlâ ayakta. Önce bunu durdurmayı dene; port
# serbest kalırsa orijinal portu koruyup üzerine yenisini başlatırız.
# Yalnızca *gerçekten* bizim olmayan başka bir servis tutuyorsa yedek porta
# kayarız.

# Mevcut docker-compose.yml içindeki host portu = otorite. Önceki bir kurulum
# port-swap yaptıysa (örn. 8766:8765) TELFILES_PORT bunu yansıtmalı; aksi
# halde son mesajdaki URL yanlış porta işaret eder ve port kontrolü de
# yanlış portu sorgular.
_cur_host_port="$(awk -F'"' '/[0-9]+:8765"/{print $2; exit}' docker-compose.yml | cut -d: -f1)"
if [ -n "$_cur_host_port" ] && [ "$_cur_host_port" != "$TELFILES_PORT" ]; then
  TELFILES_PORT="$_cur_host_port"
  log "$(msg compose_port_detected "$TELFILES_PORT")"
fi

if ss -lnt "sport = :$TELFILES_PORT" 2>/dev/null | grep -q LISTEN; then
  warn "$(msg port_busy_check "$TELFILES_PORT")"

  # 5a) Bizim eski compose servisimiz çalışıyor mu? Çalışıyorsa durdur.
  if $DOCKER_CMD compose ps -q telfiles-app 2>/dev/null | grep -q .; then
    log "$(msg stopping_old)"
    $DOCKER_CMD compose stop telfiles-app >/dev/null 2>&1 || true
    # Compose-yönetimli olmayan ama aynı isimde yetim bir container varsa
    # (eski kurulum, --name flag'iyle elle başlatılmış vs.) onu da temizle.
    $DOCKER_CMD rm -f telfiles-app >/dev/null 2>&1 || true
  else
    # Compose tanımıyor olabilir ama yine de container adı eşleşebilir.
    if $DOCKER_CMD ps -a --filter "name=^telfiles-app$" --format '{{.ID}}' 2>/dev/null | grep -q .; then
      log "$(msg removing_orphan)"
      $DOCKER_CMD rm -f telfiles-app >/dev/null 2>&1 || true
    fi
  fi

  # Stop'tan sonra portun gerçekten boşalması bir saniye alabilir.
  for _i in 1 2 3 4 5; do
    if ! ss -lnt "sport = :$TELFILES_PORT" 2>/dev/null | grep -q LISTEN; then
      ok "$(msg port_freed "$TELFILES_PORT")"
      break
    fi
    sleep 1
  done

  # 5b) Hâlâ doluysa bizim olmayan bir servis bunu tutuyor — yedek porta kay.
  if ss -lnt "sport = :$TELFILES_PORT" 2>/dev/null | grep -q LISTEN; then
    warn "$(msg port_held_other)"
    if grep -qE "\"$TELFILES_PORT:8765\"" docker-compose.yml; then
      for try_port in 8766 8767 8768 8769 18765; do
        if ! ss -lnt "sport = :$try_port" 2>/dev/null | grep -q LISTEN; then
          sed -i -E "s|\"$TELFILES_PORT:8765\"|\"$try_port:8765\"|" docker-compose.yml
          TELFILES_PORT="$try_port"
          warn "$(msg port_swapped "$TELFILES_PORT")"
          break
        fi
      done
    fi
  fi
fi

# ── 5b) Sürüm damgası (in-app güncelleme denetleyicisi için) ─────────────────
# /api/version endpoint'i bu dosyayı okuyup GitHub'daki en son commit ile
# karşılaştırarak güncelleme banner'ı gösterir.
if [ -d .git ]; then
  _ver_sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  _ver_date="$(git log -1 --format=%cI 2>/dev/null || echo "")"
  cat > app/version.json <<EOF
{"commit": "$_ver_sha", "commit_date": "$_ver_date"}
EOF
fi

# ── 6) Build + up ────────────────────────────────────────────────────────────
log "$(msg building)"
$DOCKER_CMD compose build telfiles-app
log "$(msg starting_service)"
$DOCKER_CMD compose up -d
ok "$(msg containers_up)"

# Sağlık kontrolü — uygulama 8765'i dinlemeye başlayana kadar bekle.
log "$(msg waiting_app)"
for i in $(seq 1 60); do
  if curl -fsS "http://localhost:$TELFILES_PORT/api/uiauth/login" -o /dev/null \
     -X POST -H 'Content-Type: application/json' -d '{}' 2>/dev/null; then
    break
  fi
  # 401 de OK — auth endpoint cevap veriyor demektir.
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$TELFILES_PORT/api/uiauth/login" || true)"
  case "$code" in 200|400|401|405|422) break ;; esac
  sleep 1
done

# ── 7) Çıkış mesajı ──────────────────────────────────────────────────────────
# Birincil IP'yi tespit et — default route'un kullandığı arabirimin IP'si.
HOST_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
[ -z "${HOST_IP:-}" ] && HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "${HOST_IP:-}" ] && HOST_IP="localhost"

if [ "$TF_LANG" = "tr" ]; then
cat <<EOF

${C_G}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
${C_G}${C_BOLD}  ✓ TelFiles kurulumu tamamlandı${C_RST}
${C_G}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}

  ${C_BOLD}Web arayüzü ${C_RST}: http://${HOST_IP}:${TELFILES_PORT}
  ${C_BOLD}Yerel       ${C_RST}: http://localhost:${TELFILES_PORT}
  ${C_BOLD}Parola      ${C_RST}: ${C_Y}admin${C_RST}   ${C_DIM}(ilk girişte Ayarlar → Hesap → Arabirim Parolası'ndan değiştirin)${C_RST}

  ${C_DIM}Proje dizini${C_RST}: $(pwd)
  ${C_DIM}Loglar      ${C_RST}: docker compose logs -f telfiles-app
  ${C_DIM}Durdur      ${C_RST}: docker compose down
  ${C_DIM}Güncelle    ${C_RST}: aynı kurulum komutunu yeniden çalıştırın
                ${C_DIM}(ya da: git pull && docker compose up -d --build)${C_RST}

EOF
else
cat <<EOF

${C_G}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
${C_G}${C_BOLD}  ✓ TelFiles installation complete${C_RST}
${C_G}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}

  ${C_BOLD}Web UI    ${C_RST}: http://${HOST_IP}:${TELFILES_PORT}
  ${C_BOLD}Local     ${C_RST}: http://localhost:${TELFILES_PORT}
  ${C_BOLD}Password  ${C_RST}: ${C_Y}admin${C_RST}   ${C_DIM}(change it on first login: Settings → Account → UI Password)${C_RST}

  ${C_DIM}Project dir${C_RST}: $(pwd)
  ${C_DIM}Logs       ${C_RST}: docker compose logs -f telfiles-app
  ${C_DIM}Stop       ${C_RST}: docker compose down
  ${C_DIM}Update     ${C_RST}: re-run the same install command
               ${C_DIM}(or: git pull && docker compose up -d --build)${C_RST}

EOF
fi

if [ -z "${api_id:-}" ] || [ -z "${api_hash:-}" ]; then
  if [ "$TF_LANG" = "tr" ]; then
cat <<EOF
${C_Y}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
${C_Y}${C_BOLD}  ⚠  Bir adım daha kaldı — Telegram hesabı bağlanmadı${C_RST}
${C_Y}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}

  Uygulama ayağa kalktı; web arayüzüne ${C_BOLD}admin${C_RST} parolasıyla
  girebilirsiniz. Fakat ${C_BOLD}Telegram hesabınızı bağlamadan${C_RST} grup ve
  kanallarınız taranmaz, dolayısıyla dosya/link listeniz boş kalır.

  Bağlantı için iki değer gerekli: ${C_BOLD}API_ID${C_RST} ve ${C_BOLD}API_HASH${C_RST}.
  Bunlar size özeldir, ücretsizdir, Telegram tarafından üretilir.

  ${C_BOLD}1) Kimlikleri alın${C_RST}
       Tarayıcıda açın:  ${C_B}https://my.telegram.org${C_RST}
       Telefon numaranızla giriş yapın → ${C_BOLD}API Development Tools${C_RST}
       Bir uygulama oluşturun (App title / Short name'i istediğiniz
       gibi bırakabilirsiniz). Karşınıza şunlar gelir:
         ${C_BOLD}App api_id${C_RST}    (sadece rakam)
         ${C_BOLD}App api_hash${C_RST}  (32 karakter hex)

  ${C_BOLD}2) Web arayüzünden girin${C_RST}
       Giriş yaptığınızda karşınıza çıkan formdaki iki alana
       yukarıdaki değerleri yapıştırıp ${C_BOLD}Kaydet ve devam et${C_RST}
       deyin — ya da o adımı geçtiyseniz:
       ${C_BOLD}Ayarlar → Hesap & Tema → Telegram Hesapları${C_RST} altından
       aynı bilgileri girip ${C_BOLD}➕ Hesap Ekle${C_RST} ile Telegram
       bağlantısını başlatabilirsiniz.

  ${C_DIM}Not: .env dosyasını elle düzenlemenize ya da container'ı
        yeniden başlatmanıza gerek yok — her şey arayüzden hâllolur.${C_RST}

${C_Y}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}

EOF
  else
cat <<EOF
${C_Y}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}
${C_Y}${C_BOLD}  ⚠  One more step — no Telegram account linked yet${C_RST}
${C_Y}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}

  The app is running; you can log in to the web UI with the
  ${C_BOLD}admin${C_RST} password. But ${C_BOLD}without linking a Telegram account${C_RST}
  your groups and channels won't be scanned, so the file/link
  lists will stay empty.

  Two values are required: ${C_BOLD}API_ID${C_RST} and ${C_BOLD}API_HASH${C_RST}.
  They are personal, free, and issued by Telegram.

  ${C_BOLD}1) Obtain the credentials${C_RST}
       Open in your browser:  ${C_B}https://my.telegram.org${C_RST}
       Log in with your phone number → ${C_BOLD}API Development Tools${C_RST}
       Create an application (App title / Short name can be
       anything). You will see:
         ${C_BOLD}App api_id${C_RST}    (numeric only)
         ${C_BOLD}App api_hash${C_RST}  (32-char hex)

  ${C_BOLD}2) Enter them in the web UI${C_RST}
       After logging in, paste the values into the form that
       appears and click ${C_BOLD}Save and continue${C_RST} — or if you've
       already skipped that step:
       ${C_BOLD}Settings → Account & Theme → Telegram Accounts${C_RST}
       lets you fill in the same fields and use ${C_BOLD}+ Add account${C_RST}
       to start the Telegram link flow.

  ${C_DIM}Note: you do not need to edit .env or restart the container —
        everything is handled from the UI.${C_RST}

${C_Y}${C_BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${C_RST}

EOF
  fi
fi
