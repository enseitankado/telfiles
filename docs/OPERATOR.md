# TelFiles — Operatör Notları

Programı çalıştırırken işine yarayacak "yapıştırıp kullan" türü bilgiler. Tam mimari dokümantasyon değil — operasyonel hatırlatmalar.

## Çalıştırma

```bash
docker compose up -d --build telfiles-app
```

Web arayüzü: <http://localhost:8765>

## Arabirim parolası

- **Varsayılan parola:** `admin`
- Değiştirmek: arayüzde **Ayarlar → Hesap → Arabirim Parolası** kartından.
- Greeter'da **"Bu bilgisayarda hatırla"** seçilirse oturum 30 gün, seçilmezse 12 saat geçerli.
- Parola değiştirildiği an tüm tarayıcı oturumları geçersizleşir.

### Parolayı unuttum / kilitlendim

```bash
docker exec telfiles-app rm -f /app/data/ui_auth.json
```

Sayfayı yenile, `admin` ile gir, sonra yeni parolayı belirle.

> Bu dosya pbkdf2_sha256 hash'i + aktif session token listesi taşıyor; silmek tek kullanıcılı bu uygulamada güvenlidir.

## Veritabanı erişimi

```bash
docker exec -it telfiles-postgres psql -U telfiles -d telfiles
```

Hızlı sağlık kontrolü:

```sql
SELECT COUNT(*) FROM files;
SELECT COUNT(*) FROM links WHERE available = TRUE;
SELECT id, name, COUNT(f.id) AS files FROM groups g
  LEFT JOIN files f ON f.group_id = g.id
  WHERE g.excluded = TRUE GROUP BY g.id, g.name
  HAVING COUNT(f.id) > 0;
```

## Kanal Avcısı kaynakları

`hunter_settings.sources` alanı **boş** ise tüm kayıtlı adapter'lar çalışır:

TGStat, Telemetr.io, Combot, tdirectory, tlgrm, telegramic, tgchannels, searchtg, t-do.ru, telega.io, telegramly, Bing, Mojeek, Startpage, Brave, DuckDuckGo, Yandex, Google, Ecosia, Reddit, HackerNews, GitHub.

Belirli kaynakları devre dışı bırakmak için listeyi virgüllerle özelleştir.

Çalışmayan kaynaklar run sırasında `Skipped N registered source(s): ...` warning'iyle status event'ine düşer.

## Tarama aralığı

UI'dan **Ayarlar → Tarama Sıklığı**. Backend `[900s, 86400s]` aralığına clamp eder. Realtime handler yeni mesajları zaten anında yakaladığı için periyodik tarama "backfill" görevi görür; 1–2 saat üstü FloodWait riski olmaksızın gayet iyi çalışır.

## Yaygın sorun → çözüm

**"Method Not Allowed" yeni bir endpoint'te**
Container build edilmemiştir. `docker compose up -d --build telfiles-app` çalıştır.

**Tarama çok sık oluşuyor**
`hunter_settings.next_run_at` NULL olabilir. DB'de manuel:

```sql
UPDATE hunter_settings SET next_run_at = NOW() + INTERVAL '24 hours';
```

**Bir grup gerçekte var ama dosya sayısı düşük gözüküyor**
Realtime handler watermark'ı atlatma bug'ı geçmişte oldu, düzeltildi. Etkilenen grup için **Ayarlar → Gruplar → 🔄 Yeniden Tara** watermark'ı sıfırlar.

**`docker compose up --build` cache'lenmiş layer kullanıyor**
`--no-cache` ekle veya source'ları değiştirip tekrar dene.

## Loglar

```bash
docker logs -f telfiles-app           # uygulama logları
docker logs -f telfiles-postgres      # postgres logları
```

UI içinde **Durum** sekmesi son 200 log satırını gösteriyor (rolling deque).

## Member count populating

Üye sayıları (`groups.member_count`) sync sırasında `iter_dialogs()` üzerinden, hiç `ResolveUsernameRequest` çağırılmadan toplanır. Eğer `0/N` ise henüz bir sync tamamlanmamıştır — Ayarlar → Hesap → 🔄 Şimdi Senkronize Et ile tetikle.

## Telemetri loop'u

`app/telemetry.py` 5 dakikada bir uyanır, `telemetry_settings.enabled = TRUE` ve `next_send_at <= NOW()` ise sessizce POST eder. Hata olursa 1 saat sonra tekrar dener. UI'da hata göstergesi yoktur (tasarım gereği sessiz).
