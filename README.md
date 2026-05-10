# TelFiles

Telegram kanallarında paylaşılan **dosya ve bağlantıları** kendi hesabınız üzerinden indeksleyen, arar ve indirir hâle getiren, kendi kendine barındırılan bir araç.

Üye olduğunuz tüm grup ve kanalları arka planda tarar; dosya adları, boyutları, MIME tipleri ile bağlantıları yerel bir PostgreSQL veritabanında saklar ve hepsini tek bir web arayüzünden gezilebilir kılar.

---

## ✨ Özellikler

- **Çoklu hesap** — birden fazla Telegram hesabı ekleyip aynı veritabanı altında birleştirilmiş bir görünüm elde edin.
- **Dosya & bağlantı indeksleme** — geçmiş mesajlardan tüm dosya ve linkleri arka planda toplar; yeni gelenler gerçek zamanlı olarak yakalanır.
- **Hızlı arama** — ad, tip, boyut, kanal, tarih bazlı filtre + sıralama.
- **Kanal Avcısı** — TGStat, Telemetr.io, Combot, tdirectory, tlgrm, çeşitli search engine ve Telegram dizinleri üzerinden dosya bakımından zengin yeni kanalları otomatik keşfeder, skorlar ve "katıl" kuyruğuna ekler.
- **İzleme kelimeleri** — dosya adlarında belirli kelime kombinasyonları görüldüğünde bildirim üretir (AND mantığı).
- **İndirici** — UI'dan tek tıkla dosyaları yerel diske indirir; eş zamanlı indirme kuyruğu ve duraklat/devam et desteği.
- **Çok dilli arayüz** — Türkçe, İngilizce, Almanca, Rusça, Çince.
- **Tek dosya Docker Compose dağıtımı** — `up -d` yeter.

---

## 🧱 Teknoloji yığını

| Katman | Teknoloji |
|---|---|
| Backend | Python 3.12 · FastAPI · Uvicorn |
| Telegram | [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto client) |
| DB | PostgreSQL 16 · asyncpg |
| HTTP istemci | aiohttp (brotli destekli) |
| Frontend | Vanilla JS · CSS · HTML (build adımı yok) |
| Dağıtım | Docker Compose |

---

## 🚀 Hızlı başlangıç

### Önkoşullar

- Docker ≥ 24 ve Docker Compose v2
- [my.telegram.org](https://my.telegram.org) üzerinden alınmış bir `API_ID` + `API_HASH`

### Kurulum

```bash
git clone https://github.com/enseitankado/telfiles.git
cd telfiles

# Telegram API kimlik bilgilerini .env içine yazın:
cp .env.example .env
$EDITOR .env

# Çalıştır
docker compose up -d --build
```

Web arayüzü: <http://localhost:8765>

**İlk giriş**: `admin` parolasıyla. Ayarlar → Hesap → Arabirim Parolası ekranından değiştirin (zorunlu).

### Telegram hesabı ekleme

1. Ayarlar → Hesap → ➕ Hesap Ekle
2. Telefon numarası → SMS kodu → (varsa) 2FA parolası
3. Sync otomatik başlar; ilk tam tarama hesap büyüklüğüne göre dakikalar sürebilir.

---

## ⚙️ Yapılandırma

Tüm runtime durumu host'taki `data/` ve `pgdata/` dizinlerinde tutulur — container'ı silseniz veriniz korunur.

### Ortam değişkenleri (`.env`)

| Değişken | Zorunlu | Açıklama |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | my.telegram.org API ID |
| `TELEGRAM_API_HASH` | ✅ | my.telegram.org API Hash |
| `TELEMETRY_SECRET` | hayır | Anonim istatistik gönderimi için paylaşılan secret (bkz. [Mahremiyet](#-mahremiyet--telemetri)) |

### Volume'lar

| Host dizini | Container yolu | İçerik |
|---|---|---|
| `./data/` | `/app/data` | Telegram session'ları, UI parolası, ayarlar |
| `./downloads/` | `/app/downloads` | İndirilen dosyalar |
| `./pgdata/` | `/var/lib/postgresql/data` | PostgreSQL ana veritabanı |

### Önemli ayar dosyaları (`data/` altında)

| Dosya | İçerik | Sıfırlama |
|---|---|---|
| `ui_auth.json` | Arabirim parolası hash'i + aktif token'lar | sil → `admin` döner |
| `credentials.json` | Telegram API kimlikleri (env'den önce gelir) | sil → `.env`'e geri düşer |
| `settings.json` | Sync periyodu vb. | sil → varsayılan |
| `accounts/{id}/telfiles.session` | Telethon oturumu | sil → o hesap için yeniden giriş |

### Tarama sıklığı

UI'dan **Ayarlar → Tarama Sıklığı**. Backend `[900s, 86400s]` aralığına clamp eder. Realtime handler yeni mesajları anında yakaladığı için periyodik tarama "backfill" rolündedir; pratikte **1–2 saat üstü** ideal.

---

## 🖥️ Kullanım — Sekmeler

| Sekme | İşlev |
|---|---|
| **📁 Dosyalar** | Tüm hesaplardaki tüm grupların tüm dosyaları, çoklu filtreyle |
| **🔗 Bağlantılar** | Mesajlardan parse edilmiş linkler + erişilebilirlik durumu |
| **📡 Kanal Avcısı** | Yeni kanal keşfi pipeline'ı + skor/sırala/derin tarama |
| **⬇️ İndirilenler** | İndirme kuyruğu ve geçmişi |
| **📊 Durum** | Sync durumu, son log satırları, hesap istatistikleri |
| **⚙️ Ayarlar** | Hesaplar, gruplar, izleme kelimeleri, dil, parola |

Detaylı operatör notları (DB sorguları, sorun giderme, kanal avcısı kaynakları) için: [docs/OPERATOR.md](docs/OPERATOR.md)

---

## 🗂️ Proje yapısı

```
telfiles/
├── app/
│   ├── main.py              # FastAPI uygulaması + tüm API endpoint'leri
│   ├── database.py          # asyncpg veri katmanı + şema
│   ├── telegram_client.py   # Çoklu hesap Telethon yönetimi
│   ├── sync.py              # Geçmiş mesaj tarayıcısı
│   ├── hunter.py            # Kanal avcısı pipeline'ı
│   ├── link_prober.py       # Bağlantı erişilebilirlik kontrolcüsü
│   ├── telemetry.py         # Anonim istatistik gönderici (sessiz)
│   ├── ui_auth.py           # Web arayüzü parolası + oturum
│   ├── static/              # index.html, app.js, i18n.js
│   └── Dockerfile
├── docs/
│   └── OPERATOR.md          # Operasyonel rehber (DB sorguları, sorun giderme)
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 🛠️ Geliştirme

```bash
# Source'ta değişiklik sonrası container'ı yeniden inşa:
docker compose up -d --build telfiles-app

# Logları izle:
docker logs -f telfiles-app

# Postgres'e direkt eriş:
docker exec -it telfiles-postgres psql -U telfiles -d telfiles
```

`app/static/` altındaki HTML/CSS/JS bind-mount edildiği için frontend değişiklikleri rebuild gerektirmez — sayfa yenilemesi yeterlidir.

---

## 🔒 Mahremiyet & Telemetri

TelFiles **opsiyonel ve anonim** kullanım istatistiği gönderir. Etkin olduğunda, **24 saatte bir** şu üç alanı gönderir:

- Takip ettiğiniz kanalların **username**'i (zaten herkese açık bir Telegram bilgisi)
- Her kanalın **üye sayısı** (yine herkese açık)
- O kanaldan indekslediğiniz **dosya sayısı**

**Gönderilmeyenler:** mesajlar, dosya adları, dosya içerikleri, hesap bilgisi, telefon numarası, IP. Tek tanımlayıcı, kurulumda yerel olarak üretilen rastgele bir UUID'dir (sizinle ilişkilendirilemez).

Kapatmak için: **Ayarlar → Hesap → Kullanıcı istatistiklerini gönder** checkbox'ını işaretsiz bırakın.

Alıcı endpoint sabiti `app/telemetry.py` içinde tanımlıdır; kendi alıcı sunucunuzu kullanmak için bu değeri değiştirebilirsiniz.

---

## 🤝 Sorun bildirimi

Bir hata bulduysanız ya da geliştirme önerisi varsa GitHub Issues üzerinden bildirin.

---

## ⚖️ Lisans

Henüz lisans atanmadı. Bu projeyi çatallamak, değiştirmek veya yeniden dağıtmak isterseniz lütfen iletişime geçin.

---

## ⚠️ Sorumluluk reddi

Bu araç **kendi Telegram hesabınızla** zaten erişimi olduğunuz içeriği yerel olarak indekslemenizi sağlar. Telegram'ın [Hizmet Şartları](https://telegram.org/tos)'na uygun şekilde kullanılması kullanıcının sorumluluğundadır. Yazar(lar), aracın kötüye kullanımından doğacak sonuçlardan sorumlu değildir.
