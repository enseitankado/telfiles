import asyncio
import ftplib
import logging
import os
import shutil

logger = logging.getLogger("transfer")


async def transfer_file(local_path: str, dest: dict) -> None:
    """Transfer local_path to a configured destination. Raises on failure."""
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


def _local(local_path: str, cfg: dict) -> None:
    dest_dir = (cfg.get("path") or "").strip()
    if not dest_dir:
        raise ValueError("Yerel hedef yolu boş bırakılamaz")
    os.makedirs(dest_dir, exist_ok=True)
    filename = os.path.basename(local_path)
    target = os.path.join(dest_dir, filename)
    if cfg.get("mode") == "move":
        shutil.move(local_path, target)
    else:
        shutil.copy2(local_path, target)
    logger.info("Yerel transfer tamamlandı: %s", target)


def _ftp_mkdirs(ftp: ftplib.FTP, path: str) -> None:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass  # dizin zaten var


def _ftp(local_path: str, cfg: dict) -> None:
    host = (cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 21)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    remote_path = (cfg.get("path") or "/").strip() or "/"
    passive = cfg.get("passive", True)

    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, pwd)
    ftp.set_pasv(passive)
    _ftp_mkdirs(ftp, remote_path)
    ftp.cwd(remote_path)
    filename = os.path.basename(local_path)
    with open(local_path, "rb") as f:
        ftp.storbinary(f"STOR {filename}", f, blocksize=65536)
    ftp.quit()
    logger.info("FTP transfer tamamlandı: %s%s/%s", host, remote_path, filename)


def _sftp(local_path: str, cfg: dict) -> None:
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko kütüphanesi yüklü değil — 'pip install paramiko' ile kurun")

    host = (cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 22)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    remote_path = (cfg.get("path") or "/").strip() or "/"

    transport = paramiko.Transport((host, port))
    transport.connect(username=user, password=pwd)
    sftp = paramiko.SFTPClient.from_transport(transport)
    try:
        # Uzak dizinleri oluştur
        parts = [p for p in remote_path.replace("\\", "/").split("/") if p]
        current = ""
        for part in parts:
            current += "/" + part
            try:
                sftp.mkdir(current)
            except OSError:
                pass
        filename = os.path.basename(local_path)
        remote_file = remote_path.rstrip("/") + "/" + filename
        sftp.put(local_path, remote_file)
        logger.info("SFTP transfer tamamlandı: %s:%s/%s", host, remote_path, filename)
    finally:
        sftp.close()
        transport.close()


async def test_destination(dest: dict) -> dict:
    """Hedefe bağlantıyı test eder. {ok, message} döner."""
    dtype = dest["type"]
    cfg = dest.get("config") or {}
    loop = asyncio.get_event_loop()
    try:
        if dtype == "local":
            path = (cfg.get("path") or "").strip()
            if not path:
                return {"ok": False, "message": "Yol boş bırakılamaz"}
            os.makedirs(path, exist_ok=True)
            test_file = os.path.join(path, ".telfiles_test")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            return {"ok": True, "message": "Yazma erişimi doğrulandı"}
        elif dtype == "ftp":
            await loop.run_in_executor(None, _test_ftp, cfg)
            return {"ok": True, "message": "FTP bağlantısı başarılı"}
        elif dtype == "sftp":
            await loop.run_in_executor(None, _test_sftp, cfg)
            return {"ok": True, "message": "SFTP bağlantısı başarılı"}
        else:
            return {"ok": False, "message": f"Bilinmeyen tür: {dtype}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _test_ftp(cfg: dict) -> None:
    host = (cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 21)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    passive = cfg.get("passive", True)
    ftp = ftplib.FTP()
    ftp.connect(host, port, timeout=10)
    ftp.login(user, pwd)
    ftp.set_pasv(passive)
    ftp.quit()


def _test_sftp(cfg: dict) -> None:
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko yüklü değil")
    host = (cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 22)
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    transport = paramiko.Transport((host, port))
    transport.connect(username=user, password=pwd)
    transport.close()
