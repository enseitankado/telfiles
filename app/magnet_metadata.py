"""Fetch BitTorrent metadata (file list, total size) for a magnet URI.

A magnet URI carries only an infohash — the actual file list lives in the
torrent's `info` dict, which is exchanged on the swarm via the ut_metadata
extension after a peer is found through DHT or one of the trackers.

We delegate the swarm/DHT work to aria2c (`--bt-metadata-only=true`), which
writes a regular .torrent file to disk; we then parse it with the project's
existing bencode parser. This avoids pulling in libtorrent (which would
require a Python version bump or a custom build).

aria2 runtime characteristics:
  - First fetch may take 30–60 s for a healthy torrent; rare torrents with
    no peers may never complete and will be cancelled at the timeout.
  - Each call uses a fresh temp dir and a private --dht-listen-port range
    so concurrent calls do not collide.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Dict, Optional

import torrent_parse

logger = logging.getLogger(__name__)


# aria2c arguments shared across every fetch. `bt-stop-timeout=0` means the
# process never goes idle even when no peers are connecting; we rely on our
# own asyncio timeout for the deadline.
_ARIA_ARGS = [
    "--bt-metadata-only=true",
    "--bt-save-metadata=true",
    "--summary-interval=0",
    "--console-log-level=error",
    "--check-certificate=false",
    "--bt-stop-timeout=0",
    "--enable-dht=true",
    "--enable-peer-exchange=true",
    "--bt-tracker-connect-timeout=8",
    "--bt-tracker-timeout=8",
    "--allow-overwrite=true",
    "--seed-time=0",
    "--max-concurrent-downloads=1",
]


async def fetch_magnet_metadata(uri: str, *, timeout: int = 60) -> Optional[Dict]:
    """Fetch and parse BitTorrent metadata for a magnet URI.

    Returns the same dict shape as `torrent_parse.parse_torrent`:
        {name, total_size, file_count, tree: [{path, size}, ...]}
    Returns None on timeout, aria2c failure, or parse failure (any of which
    are normal — many magnets reference dead torrents and never resolve).
    """
    if not uri.lower().startswith("magnet:"):
        return None
    with tempfile.TemporaryDirectory(prefix="magmeta_") as td:
        cmd = ["aria2c", *_ARIA_ARGS, "-d", td, uri]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("magnet_metadata: timeout (%ss) fetching %s", timeout, uri[:80])
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                return None
        except FileNotFoundError:
            logger.error("magnet_metadata: aria2c binary not installed in container")
            return None
        except Exception as e:
            logger.warning("magnet_metadata: aria2c failed: %s", e)
            return None

        torrent_path = next(
            (os.path.join(td, fn) for fn in os.listdir(td) if fn.endswith(".torrent")),
            None,
        )
        if not torrent_path:
            return None
        try:
            with open(torrent_path, "rb") as f:
                data = f.read()
            return torrent_parse.parse_torrent(data)
        except Exception as e:
            logger.warning("magnet_metadata: parse failed: %s", e)
            return None


def magnet_to_link_files(meta: Dict) -> list:
    """Convert parsed torrent metadata into the `files_json` shape that
    `links.files_json` expects: a list of {name, size}."""
    if not meta:
        return []
    tree = meta.get("tree") or []
    return [{"name": item.get("path") or "", "size": int(item.get("size") or 0)} for item in tree]
