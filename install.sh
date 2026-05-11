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

# ── Ön kontroller ─────────────────────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || die "Bu betik Linux üzerinde çalışmak için tasarlandı."

if ! command -v apt-get >/dev/null 2>&1; then
  die "apt-get bulunamadı — yalnızca Debian tabanlı dağıtımlar (Debian/Ubuntu/Kali/Mint) desteklenir."
fi

# Sudo'yu root değilse zorla. Root isek sudo'suz çalış.
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  SUDO=""
else
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
    log "sudo gerektiren adımlar için parola istenebilir."
  else
    die "Root değilsiniz ve sudo da yüklü değil. Root olarak çalıştırın veya sudo kurun."
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
      warn "Bozuk docker.list bulundu (codename=${_docker_codename}) — askıya alınıyor."
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
          warn "Bozuk apt kaynağı askıya alındı: $f (içeriği: $url)"
          $SUDO mv "$f" "${f}.broken.$(date +%s)" 2>/dev/null || true
          cleaned=1
        fi
      done
    done <<< "$urls"
  fi

  if [ $cleaned -eq 1 ]; then
    log "Bozuk kaynaklar temizlendi, apt-get update tekrar deneniyor…"
    $SUDO apt-get update -qq && return 0
  fi
  printf '%s\n' "$out" >&2
  return 1
}

log "Sistem paket dizini güncelleniyor…"
_apt_update || die "apt-get update başarısız. Yukarıdaki hatalara bakın; bozuk depo dosyaları /etc/apt/sources.list.d/ altında olabilir."

log "Temel araçlar kuruluyor (curl, git, ca-certificates, gnupg)…"
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ca-certificates curl git gnupg lsb-release iproute2 >/dev/null
ok "Temel araçlar hazır."

# ── 2) Docker + Compose plugin ───────────────────────────────────────────────
need_docker_install=0
if ! command -v docker >/dev/null 2>&1; then
  need_docker_install=1
elif ! docker compose version >/dev/null 2>&1; then
  warn "Docker var ama 'docker compose' plugin'i yok — kurulum yapılacak."
  need_docker_install=1
fi

if [ "$need_docker_install" -eq 1 ]; then
  log "Docker Engine + Compose plugin kuruluyor (docker.com resmi repo)…"

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
        warn "Bilinmeyen dağıtım: $DIST_ID — Debian repo'su deneniyor."
        DOCKER_REPO_DIST=debian
        DIST_CODENAME="$(_debian_base_codename)"
        [ -n "$DIST_CODENAME" ] || DIST_CODENAME=bookworm
      fi
      ;;
  esac

  log "Algılanan dağıtım: ${DIST_ID} → ${DOCKER_REPO_DIST}/${DIST_CODENAME}"

  # Docker'da bu codename gerçekten var mı? Yoksa stable fallback'lerini sırayla
  # dene (bookworm, bullseye, jammy, focal).
  if ! _docker_repo_ok "$DOCKER_REPO_DIST" "$DIST_CODENAME"; then
    warn "Docker repo'sunda ${DOCKER_REPO_DIST}/${DIST_CODENAME} yok — yedek codename'ler deneniyor."
    _fallback_ok=0
    if [ "$DOCKER_REPO_DIST" = "ubuntu" ]; then
      _fallbacks="jammy focal noble"
    else
      _fallbacks="bookworm bullseye trixie"
    fi
    for _cn in $_fallbacks; do
      if _docker_repo_ok "$DOCKER_REPO_DIST" "$_cn"; then
        DIST_CODENAME="$_cn"
        ok "Yedek codename kullanılıyor: ${DOCKER_REPO_DIST}/${DIST_CODENAME}"
        _fallback_ok=1
        break
      fi
    done
    [ "$_fallback_ok" -eq 1 ] || die "Docker resmi repo'sunda uyumlu codename bulunamadı (${DOCKER_REPO_DIST}). Manuel kurulum için: https://docs.docker.com/engine/install/"
  fi

  $SUDO install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${DOCKER_REPO_DIST}/gpg" \
    | $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg

  ARCH="$(dpkg --print-architecture)"
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${DOCKER_REPO_DIST} ${DIST_CODENAME} stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null

  if ! _apt_update; then
    warn "apt-get update başarısız — Docker resmi repo satırı kaldırılıp dağıtımın docker.io paketi deneniyor."
    $SUDO rm -f /etc/apt/sources.list.d/docker.list
    _apt_update || die "apt-get update tekrar başarısız oldu."
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null \
      || $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose >/dev/null \
      || die "Docker kurulumu başarısız. Manuel kurulum: https://docs.docker.com/engine/install/"
  else
    if ! $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
           docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1; then
      warn "docker-ce paketleri kurulamadı — dağıtımın docker.io paketine düşülüyor."
      $SUDO rm -f /etc/apt/sources.list.d/docker.list
      _apt_update
      $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 >/dev/null \
        || $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose >/dev/null \
        || die "Docker kurulumu başarısız."
    fi
  fi

  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true

  # Kullanıcıyı docker grubuna ekle — yeni shell'de sudo'suz kullanım için.
  if [ -n "${SUDO_USER:-${USER:-}}" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
    $SUDO usermod -aG docker "${SUDO_USER:-$USER}" 2>/dev/null || true
    warn "Mevcut shell'de docker grubu henüz aktif değil. Yeni oturumda sudo'suz 'docker' kullanılabilir."
  fi

  ok "Docker $(docker --version | awk '{print $3}' | tr -d ',') kuruldu."
else
  ok "Docker zaten yüklü: $(docker --version | awk '{print $3}' | tr -d ',')."
fi

# Docker daemon erişimi
if ! $SUDO docker info >/dev/null 2>&1; then
  log "Docker daemon başlatılıyor…"
  $SUDO systemctl start docker || die "Docker daemon başlatılamadı. 'sudo systemctl status docker' ile bakın."
fi

# Kullanıcı docker grubunda mı? Değilse sonraki docker komutlarını sudo ile çalıştır.
if [ "${EUID:-$(id -u)}" -ne 0 ] && ! groups | grep -qE '(^| )docker( |$)'; then
  DOCKER_CMD="$SUDO docker"
else
  DOCKER_CMD="docker"
fi

# ── 3) Repo'yu çek veya güncelle ─────────────────────────────────────────────
if [ -d "$TELFILES_DIR/.git" ]; then
  log "Mevcut TelFiles kopyası bulundu: $TELFILES_DIR — güncelleniyor…"
  git -C "$TELFILES_DIR" fetch --depth=1 origin "$TELFILES_BRANCH"
  git -C "$TELFILES_DIR" reset --hard "origin/$TELFILES_BRANCH"
elif [ -f "docker-compose.yml" ] && [ -d "app" ]; then
  # Halihazırda proje kökündeyiz (örn. ./install.sh).
  TELFILES_DIR="."
  log "Mevcut proje kökünde çalışıyor: $(pwd)"
else
  log "TelFiles deposu klonlanıyor → $TELFILES_DIR"
  git clone --depth=1 --branch "$TELFILES_BRANCH" "$REPO_URL" "$TELFILES_DIR"
fi

cd "$TELFILES_DIR"
ok "Çalışma dizini: $(pwd)"

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
    printf "\n%sTelegram API kimlik bilgileri%s — https://my.telegram.org → API Development Tools\n" "$C_BOLD" "$C_RST" >/dev/tty
    printf "%s(Şimdi boş bırakıp sonra .env dosyasını elle düzenleyebilirsiniz.)%s\n\n" "$C_DIM" "$C_RST" >/dev/tty
    ask api_id  "TELEGRAM_API_ID    : "
    ask api_hash "TELEGRAM_API_HASH  : "
  else
    warn "TELEGRAM_API_ID / TELEGRAM_API_HASH boş. Servis çalışacak ama hesap eklenemeyecek."
    warn "Düzenle: $TELFILES_DIR/.env  (sonra: docker compose restart telfiles-app)"
  fi
fi

# .env dosyasını yaz/güncelle
{
  echo "# Get these from https://my.telegram.org → API Development Tools"
  echo "TELEGRAM_API_ID=$api_id"
  echo "TELEGRAM_API_HASH=$api_hash"
} > .env
ok ".env güncellendi."

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
  log "docker-compose.yml host port'u $TELFILES_PORT olarak algılandı."
fi

if ss -lnt "sport = :$TELFILES_PORT" 2>/dev/null | grep -q LISTEN; then
  warn "$TELFILES_PORT portu zaten kullanımda — eski TelFiles container'ı mı diye bakılıyor."

  # 5a) Bizim eski compose servisimiz çalışıyor mu? Çalışıyorsa durdur.
  if $DOCKER_CMD compose ps -q telfiles-app 2>/dev/null | grep -q .; then
    log "Eski telfiles-app container'ı durduruluyor (sağlıklı yeniden başlatma için)…"
    $DOCKER_CMD compose stop telfiles-app >/dev/null 2>&1 || true
    # Compose-yönetimli olmayan ama aynı isimde yetim bir container varsa
    # (eski kurulum, --name flag'iyle elle başlatılmış vs.) onu da temizle.
    $DOCKER_CMD rm -f telfiles-app >/dev/null 2>&1 || true
  else
    # Compose tanımıyor olabilir ama yine de container adı eşleşebilir.
    if $DOCKER_CMD ps -a --filter "name=^telfiles-app$" --format '{{.ID}}' 2>/dev/null | grep -q .; then
      log "telfiles-app adındaki eski container kaldırılıyor…"
      $DOCKER_CMD rm -f telfiles-app >/dev/null 2>&1 || true
    fi
  fi

  # Stop'tan sonra portun gerçekten boşalması bir saniye alabilir.
  for _i in 1 2 3 4 5; do
    if ! ss -lnt "sport = :$TELFILES_PORT" 2>/dev/null | grep -q LISTEN; then
      ok "$TELFILES_PORT portu serbest bırakıldı; orijinal port korunuyor."
      break
    fi
    sleep 1
  done

  # 5b) Hâlâ doluysa bizim olmayan bir servis bunu tutuyor — yedek porta kay.
  if ss -lnt "sport = :$TELFILES_PORT" 2>/dev/null | grep -q LISTEN; then
    warn "Port hâlâ bizim olmayan başka bir servis tarafından kullanılıyor."
    if grep -qE "\"$TELFILES_PORT:8765\"" docker-compose.yml; then
      for try_port in 8766 8767 8768 8769 18765; do
        if ! ss -lnt "sport = :$try_port" 2>/dev/null | grep -q LISTEN; then
          sed -i -E "s|\"$TELFILES_PORT:8765\"|\"$try_port:8765\"|" docker-compose.yml
          TELFILES_PORT="$try_port"
          warn "Port → $TELFILES_PORT olarak değiştirildi."
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
log "Container'lar inşa ediliyor (ilk kurulumda 2-5 dk sürebilir)…"
$DOCKER_CMD compose build telfiles-app
log "Servis başlatılıyor…"
$DOCKER_CMD compose up -d
ok "Containerlar ayakta."

# Sağlık kontrolü — uygulama 8765'i dinlemeye başlayana kadar bekle.
log "Uygulamanın açılması bekleniyor…"
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

if [ -z "${api_id:-}" ] || [ -z "${api_hash:-}" ]; then
  warn "TELEGRAM_API_ID / TELEGRAM_API_HASH boş kaldı."
  warn "Şu adımları izleyin:"
  warn "    cd $(pwd)"
  warn "    \$EDITOR .env     # iki değişkene değerlerini girin"
  warn "    docker compose restart telfiles-app"
fi
