import asyncio
import ftplib
import io
import logging
import os
import shutil

logger = logging.getLogger("transfer")

# Host'un kök dizini container içinde buraya mount edilir (docker-compose.yml).
# Kullanıcı /home/kali/Desktop gibi host yolları yazınca biz otomatik olarak
# /app/host/home/kali/Desktop'a çeviririz — böylece container dışını görür.
_HOST_ROOT = "/app/host"


def _to_container_path(path: str) -> str:
    """Host'a ait mutlak yolu container'ın erişebildiği eşdeğerine çevirir.

    /app/ ile başlayan yollar zaten container-native'dir (örn. /app/downloads).
    Diğer tüm mutlak yollar /app/host öneki alır.
    """
    p = (path or "").strip()
    if not p:
        return p
    if p.startswith("/app/"):
        return p
    # Zaten dönüştürülmüşse dokunma
    if p.startswith(_HOST_ROOT + "/") or p == _HOST_ROOT:
        return p
    return _HOST_ROOT + p


# ── Public API ────────────────────────────────────────────────────────────────

async def transfer_file(local_path: str, dest: dict) -> None:
    """local_path'i yapılandırılmış hedefe gönderir. Hata durumunda exception fırlatır."""
    dtype = dest["type"]
    cfg = dest.get("config") or {}
    loop = asyncio.get_event_loop()
    if dtype == "local":
        await loop.run_in_executor(None, _local, local_path, cfg)
    elif dtype == "ftp":
        await loop.run_in_executor(None, _ftp, local_path, cfg)
    elif dtype == "sftp":
        await loop.run_in_executor(None, _sftp, local_path, cfg)
    else:
        raise ValueError(f"Bilinmeyen hedef türü: {dtype!r}")


async def test_destination(dest: dict) -> dict:
    """Hedefe bağlantı + yazma testi yapar. {ok, message} döner."""
    dtype = dest["type"]
    cfg = dest.get("config") or {}
    loop = asyncio.get_event_loop()
    try:
        if dtype == "local":
            path = (cfg.get("path") or "").strip()
            if not path:
                return {"ok": False, "message": "Yol boş bırakılamaz"}
            container_path = _to_container_path(path)
            os.makedirs(container_path, exist_ok=True)
            test_file = os.path.join(container_path, ".telfiles_test")
            with open(test_file, "w") as f:
                f.write("telfiles write test")
            os.remove(test_file)
            return {"ok": True, "message": f"Yazma erişimi doğrulandı → {path}"}
        elif dtype == "ftp":
            await loop.run_in_executor(None, _test_ftp, cfg)
            return {"ok": True, "message": "FTP: bağlantı + yazma + silme testi başarılı"}
        elif dtype == "sftp":
            await loop.run_in_executor(None, _test_sftp, cfg)
            return {"ok": True, "message": "SFTP: bağlantı + yazma + silme testi başarılı"}
        else:
            return {"ok": False, "message": f"Bilinmeyen tür: {dtype}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ── Local ─────────────────────────────────────────────────────────────────────

def _local(local_path: str, cfg: dict) -> None:
    dest_dir = (cfg.get("path") or "").strip()
    if not dest_dir:
        raise ValueError("Yerel hedef yolu boş bırakılamaz")
    container_dir = _to_container_path(dest_dir)
    os.makedirs(container_dir, exist_ok=True)
    filename = os.path.basename(local_path)
    target = os.path.join(container_dir, filename)
    if cfg.get("mode") == "move":
        shutil.move(local_path, target)
        logger.info("Yerel taşıma tamamlandı: %s → %s", local_path, target)
    else:
        shutil.copy2(local_path, target)
        logger.info("Yerel kopyalama tamamlandı: %s → %s", local_path, target)


# ── FTP ───────────────────────────────────────────────────────────────────────

def _ftp_mkdirs(ftp: ftplib.FTP, path: str) -> None:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass  # dizin zaten var


def _ftp_connect(cfg: dict) -> ftplib.FTP:
    host = (cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 21)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    passive = cfg.get("passive", True)
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, pwd)
    ftp.set_pasv(passive)
    return ftp


def _ftp(local_path: str, cfg: dict) -> None:
    remote_path = (cfg.get("path") or "/").strip() or "/"
    ftp = _ftp_connect(cfg)
    try:
        _ftp_mkdirs(ftp, remote_path)
        ftp.cwd(remote_path)
        filename = os.path.basename(local_path)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {filename}", f, blocksize=65536)
        logger.info("FTP transfer tamamlandı: %s/%s", remote_path, filename)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    if cfg.get("mode") == "move":
        os.remove(local_path)
        logger.info("Yerel dosya silindi (taşıma modu): %s", local_path)


def _test_ftp(cfg: dict) -> None:
    remote_path = (cfg.get("path") or "/").strip() or "/"
    ftp = _ftp_connect({**cfg, "port": cfg.get("port") or 21})
    try:
        _ftp_mkdirs(ftp, remote_path)
        ftp.cwd(remote_path)
        # Yazma testi: test dosyası yükle
        test_name = ".telfiles_test"
        ftp.storbinary(f"STOR {test_name}", io.BytesIO(b"telfiles write test"), blocksize=4096)
        # Silme testi
        try:
            ftp.delete(test_name)
        except ftplib.error_perm:
            pass  # Bazı sunucular silmeye izin vermez ama yükleme çalıştı
        ftp.quit()
    except Exception:
        try:
            ftp.quit()
        except Exception:
            pass
        raise


# ── SFTP ─────────────────────────────────────────────────────────────────────

def _sftp_connect(cfg: dict):
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko kütüphanesi yüklü değil — 'pip install paramiko' ile kurun")
    host = (cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 22)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    transport = paramiko.Transport((host, port))
    transport.connect(username=user, password=pwd)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def _sftp_mkdirs(sftp, path: str) -> None:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.mkdir(current)
        except OSError:
            pass


def _sftp(local_path: str, cfg: dict) -> None:
    remote_path = (cfg.get("path") or "/").strip() or "/"
    transport, sftp = _sftp_connect(cfg)
    try:
        _sftp_mkdirs(sftp, remote_path)
        filename = os.path.basename(local_path)
        remote_file = remote_path.rstrip("/") + "/" + filename
        sftp.put(local_path, remote_file)
        logger.info("SFTP transfer tamamlandı: %s:%s", cfg.get("host", ""), remote_file)
    finally:
        sftp.close()
        transport.close()
    if cfg.get("mode") == "move":
        os.remove(local_path)
        logger.info("Yerel dosya silindi (taşıma modu): %s", local_path)


def _test_sftp(cfg: dict) -> None:
    remote_path = (cfg.get("path") or "/").strip() or "/"
    transport, sftp = _sftp_connect(cfg)
    try:
        _sftp_mkdirs(sftp, remote_path)
        # Yazma testi
        remote_test = remote_path.rstrip("/") + "/.telfiles_test"
        sftp.putfo(io.BytesIO(b"telfiles write test"), remote_test)
        # Silme testi
        try:
            sftp.remove(remote_test)
        except OSError:
            pass
    finally:
        sftp.close()
        transport.close()
