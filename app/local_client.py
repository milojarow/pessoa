"""
Pessoa - Local client manager using WireGuard + Linux network namespaces.
"""
import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path.home() / ".pessoa" / "clients"
MAX_IFACE_LEN = 15  # Linux interface name limit


def _netns_name(slug: str) -> str:
    return f"pessoa-{slug}"


def _wg_iface(slug: str) -> str:
    name = f"wg-{slug}"
    if len(name) > MAX_IFACE_LEN:
        raise ValueError(f"Interface name '{name}' exceeds {MAX_IFACE_LEN} chars. Use a shorter slug.")
    return name


def _client_dir(slug: str) -> Path:
    return BASE_DIR / slug


def _wg_conf_path(slug: str) -> Path:
    return _client_dir(slug) / "wireguard" / "wg0.conf"


def _client_json_path(slug: str) -> Path:
    return _client_dir(slug) / "client.json"


async def _run(cmd: list[str], check: bool = True) -> asyncio.subprocess.Process:
    """Run a command asynchronously."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({' '.join(cmd)}): {stderr.decode().strip()}")
    proc._stdout_data = stdout  # stash for callers
    proc._stderr_data = stderr
    return proc


async def _sudo(cmd: list[str], check: bool = True) -> asyncio.subprocess.Process:
    """Run a command with sudo."""
    return await _run(["sudo", "--non-interactive"] + cmd, check=check)


def _parse_wg_config(config_str: str) -> dict:
    """Parse a wg-quick style .conf, separating wg-native fields from wg-quick fields."""
    address = None
    dns = None
    mtu = None
    native_lines = []

    for line in config_str.strip().splitlines():
        stripped = line.strip()
        lower = stripped.lower()

        # Skip wg-quick only fields
        if re.match(r'^\s*(PostUp|PreDown|PostDown|SaveConfig|Table)\s*=', stripped, re.IGNORECASE):
            continue

        if re.match(r'^\s*Address\s*=', stripped, re.IGNORECASE):
            address = stripped.split("=", 1)[1].strip()
            continue
        if re.match(r'^\s*DNS\s*=', stripped, re.IGNORECASE):
            dns = stripped.split("=", 1)[1].strip()
            continue
        if re.match(r'^\s*MTU\s*=', stripped, re.IGNORECASE):
            mtu = stripped.split("=", 1)[1].strip()
            continue

        native_lines.append(line)

    if not address:
        raise ValueError("No Address found in [Interface] section")

    return {
        "address": address,
        "dns": dns,
        "mtu": mtu,
        "native_config": "\n".join(native_lines) + "\n",
    }


def _read_client_json(slug: str) -> dict:
    path = _client_json_path(slug)
    if path.exists():
        return json.loads(path.read_text())
    return {"slug": slug, "created": datetime.now(timezone.utc).isoformat()}


def _write_client_json(slug: str, data: dict) -> None:
    path = _client_json_path(slug)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Client CRUD
# ---------------------------------------------------------------------------

def list_clients() -> list[dict]:
    """Scan client directories and return metadata + runtime status."""
    if not BASE_DIR.exists():
        return []

    clients = []
    for d in sorted(BASE_DIR.iterdir()):
        if not d.is_dir():
            continue
        slug = d.name
        # Auto-create client.json if missing (discovered directory)
        if not _client_json_path(slug).exists():
            _write_client_json(slug, {
                "slug": slug,
                "created": datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
        clients.append(_build_client_info(slug))
    return clients


def get_client(slug: str) -> Optional[dict]:
    if not _client_dir(slug).exists():
        return None
    return _build_client_info(slug)


def _build_client_info(slug: str) -> dict:
    """Build client info dict matching what templates expect."""
    data = _read_client_json(slug)
    has_config = _wg_conf_path(slug).exists()

    if not has_config:
        state = "pending_config"
        status = "Pending Config"
    else:
        vpn = _get_vpn_status_sync(slug)
        state = "ready"
        status = vpn  # "Active", "Idle", "Stopped", "Starting", "Error"

    return {
        "slug": slug,
        "name": slug.capitalize(),
        "state": state,
        "status": status,
        "created": data.get("created", ""),
    }


def _get_vpn_status_sync(slug: str) -> str:
    """Check VPN status synchronously (for list_clients)."""
    import subprocess

    netns = _netns_name(slug)
    iface = _wg_iface(slug)

    # Check if namespace exists
    result = subprocess.run(
        ["sudo", "--non-interactive", "ip", "netns", "list"],
        capture_output=True, text=True, timeout=5,
    )
    if netns not in result.stdout:
        return "Stopped"

    # Read peer handshake + transfer in a single call
    result = subprocess.run(
        ["sudo", "--non-interactive", "ip", "netns", "exec", netns, "wg", "show", iface, "dump"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        return "Error"

    # dump format: line 1 = interface, lines 2+ = peers
    # peer fields (tab-separated): public-key, preshared-key, endpoint,
    # allowed-ips, latest-handshake, rx-bytes, tx-bytes, persistent-keepalive
    lines = result.stdout.strip().splitlines()
    if len(lines) < 2:
        return "Starting"

    peer_parts = lines[1].split("\t")
    if len(peer_parts) < 7:
        return "Error"

    try:
        ts = int(peer_parts[4])
        rx = int(peer_parts[5])
        tx = int(peer_parts[6])
    except ValueError:
        return "Error"

    if ts == 0:
        return "Starting"

    age = int(datetime.now().timestamp()) - ts
    if age < 180:
        return "Active"

    # Stale handshake: WireGuard renegotiates on demand when traffic flows, so
    # a stale handshake after past transfer is an idle tunnel, not a broken one.
    if rx > 0 or tx > 0:
        return "Idle"
    return "Error"


def create_client(slug: str) -> dict:
    """Create a new client directory structure."""
    client_dir = _client_dir(slug)
    if client_dir.exists():
        raise ValueError(f"Client '{slug}' already exists")

    # Validate interface name length
    _wg_iface(slug)

    # Create directories
    (client_dir / "wireguard").mkdir(parents=True)
    (client_dir / "browser" / "profile").mkdir(parents=True)
    (client_dir / "browser" / "downloads").mkdir(parents=True)

    # Write metadata
    data = {"slug": slug, "created": datetime.now(timezone.utc).isoformat()}
    _write_client_json(slug, data)

    return {"slug": slug, "state": "pending_config"}


def delete_client(slug: str) -> None:
    """Stop VPN if running and delete client directory."""
    client_dir = _client_dir(slug)
    if not client_dir.exists():
        raise ValueError(f"Client '{slug}' not found")

    # Stop VPN synchronously if running
    import subprocess
    netns = _netns_name(slug)
    result = subprocess.run(
        ["sudo", "--non-interactive", "ip", "netns", "list"],
        capture_output=True, text=True, timeout=5,
    )
    if netns in result.stdout:
        _stop_vpn_sync(slug)

    shutil.rmtree(client_dir)


def save_wireguard_config(slug: str, config_content: str) -> dict:
    """Validate and save a WireGuard config file."""
    if not _client_dir(slug).exists():
        raise ValueError(f"Client '{slug}' not found")

    # Validate by parsing
    parsed = _parse_wg_config(config_content)

    # Extract endpoint for display
    endpoint = None
    for line in config_content.splitlines():
        if re.match(r'^\s*Endpoint\s*=', line, re.IGNORECASE):
            endpoint = line.split("=", 1)[1].strip().split(":")[0]
            break

    # Save config
    wg_dir = _client_dir(slug) / "wireguard"
    wg_dir.mkdir(parents=True, exist_ok=True)
    conf_path = _wg_conf_path(slug)
    conf_path.write_text(config_content)
    conf_path.chmod(0o600)

    return {"address": parsed["address"], "endpoint": endpoint}


# ---------------------------------------------------------------------------
# VPN operations
# ---------------------------------------------------------------------------

async def start_vpn(slug: str) -> None:
    """Create network namespace and bring up WireGuard."""
    if not _wg_conf_path(slug).exists():
        raise RuntimeError(f"No WireGuard config for client '{slug}'")

    netns = _netns_name(slug)
    iface = _wg_iface(slug)
    config_str = _wg_conf_path(slug).read_text()
    parsed = _parse_wg_config(config_str)
    tmp_conf = f"/tmp/{netns}-wg.conf"

    # Write wg-native config to temp file
    tmp_path = Path(tmp_conf)
    tmp_path.write_text(parsed["native_config"])
    tmp_path.chmod(0o600)

    try:
        # Create namespace
        await _sudo(["ip", "netns", "add", netns])

        # Create WireGuard interface on HOST (socket stays here for endpoint reachability)
        await _sudo(["ip", "link", "add", iface, "type", "wireguard"])
        await _sudo(["wg", "setconf", iface, tmp_conf])

        # Move interface into namespace
        await _sudo(["ip", "link", "set", iface, "netns", netns])

        # Configure inside namespace
        await _sudo(["ip", "netns", "exec", netns, "ip", "link", "set", "lo", "up"])

        # Handle multiple addresses (comma-separated)
        for addr in parsed["address"].split(","):
            addr = addr.strip()
            await _sudo(["ip", "netns", "exec", netns, "ip", "addr", "add", addr, "dev", iface])

        if parsed["mtu"]:
            await _sudo(["ip", "netns", "exec", netns, "ip", "link", "set", "mtu", parsed["mtu"], "dev", iface])

        await _sudo(["ip", "netns", "exec", netns, "ip", "link", "set", iface, "up"])
        await _sudo(["ip", "netns", "exec", netns, "ip", "route", "add", "default", "dev", iface])

        # DNS
        if parsed["dns"]:
            dns_dir = f"/etc/netns/{netns}"
            await _sudo(["mkdir", "-p", dns_dir])
            # Write resolv.conf with all DNS servers
            dns_servers = [s.strip() for s in parsed["dns"].split(",")]
            resolv_content = "\n".join(f"nameserver {s}" for s in dns_servers) + "\n"
            proc = await asyncio.create_subprocess_exec(
                "sudo", "--non-interactive", "tee", f"{dns_dir}/resolv.conf",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(resolv_content.encode())

        # Trigger handshake by sending traffic through the tunnel
        await _sudo(["ip", "netns", "exec", netns, "ping", "-c", "1", "-W", "5", "1.1.1.1"], check=False)

    except Exception:
        # Cleanup on failure
        await _sudo(["ip", "netns", "del", netns], check=False)
        await _sudo(["ip", "link", "del", iface], check=False)
        await _sudo(["rm", "-rf", f"/etc/netns/{netns}"], check=False)
        raise
    finally:
        # Always clean temp file
        tmp_path.unlink(missing_ok=True)


async def stop_vpn(slug: str) -> None:
    """Tear down VPN namespace and interface."""
    netns = _netns_name(slug)
    iface = _wg_iface(slug)

    # Kill all processes inside the namespace
    proc = await _sudo(["ip", "netns", "pids", netns], check=False)
    if proc.returncode == 0 and proc._stdout_data.strip():
        pids = proc._stdout_data.decode().strip().split()
        for pid in pids:
            await _sudo(["kill", pid], check=False)
        await asyncio.sleep(0.5)
        # Force kill stragglers
        proc = await _sudo(["ip", "netns", "pids", netns], check=False)
        if proc.returncode == 0 and proc._stdout_data.strip():
            for pid in proc._stdout_data.decode().strip().split():
                await _sudo(["kill", "-9", pid], check=False)
            await asyncio.sleep(0.3)

    # Delete interface and namespace
    await _sudo(["ip", "netns", "exec", netns, "ip", "link", "del", iface], check=False)
    await _sudo(["ip", "netns", "del", netns], check=False)

    # Cleanup
    await _sudo(["rm", "-rf", f"/etc/netns/{netns}"], check=False)


def _stop_vpn_sync(slug: str) -> None:
    """Synchronous VPN teardown (for use in delete_client)."""
    import subprocess

    netns = _netns_name(slug)
    iface = _wg_iface(slug)

    result = subprocess.run(
        ["sudo", "--non-interactive", "ip", "netns", "pids", netns],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0 and result.stdout.strip():
        for pid in result.stdout.strip().split():
            subprocess.run(["sudo", "--non-interactive", "kill", pid], timeout=5)
        import time
        time.sleep(0.5)

    subprocess.run(
        ["sudo", "--non-interactive", "ip", "netns", "exec", netns, "ip", "link", "del", iface],
        capture_output=True, timeout=5,
    )
    subprocess.run(
        ["sudo", "--non-interactive", "ip", "netns", "del", netns],
        capture_output=True, timeout=5,
    )
    subprocess.run(
        ["sudo", "--non-interactive", "rm", "-rf", f"/etc/netns/{netns}"],
        capture_output=True, timeout=5,
    )


async def launch_browser(slug: str) -> int:
    """Launch Firefox inside the client's network namespace."""
    netns = _netns_name(slug)

    # Verify VPN is running
    status = _get_vpn_status_sync(slug)
    if status not in ("Active", "Idle", "Starting"):
        raise RuntimeError(f"VPN is not running for '{slug}' (status: {status}). Start VPN first.")

    profile_dir = _client_dir(slug) / "browser" / "profile"
    if not profile_dir.exists():
        raise RuntimeError(f"Browser profile not found for '{slug}'")

    user_js = profile_dir / "user.js"
    if user_js.exists():
        lines = user_js.read_text().splitlines()
        cleaned = [l for l in lines if "network.proxy." not in l]
        cleaned.append('user_pref("network.proxy.type", 0);')
        user_js.write_text("\n".join(cleaned) + "\n")

    # Kill existing browser for this profile
    import subprocess
    subprocess.run(
        ["pkill", "-f", f"firefox.*--profile.*{profile_dir}"],
        capture_output=True, timeout=5,
    )
    await asyncio.sleep(0.3)

    # Remove stale lock
    lock_file = profile_dir / "lock"
    if lock_file.is_symlink():
        lock_file.unlink(missing_ok=True)

    # Detect Wayland display
    import glob
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    if not wayland_display:
        sockets = [s for s in glob.glob(f"{runtime_dir}/wayland-*") if not s.endswith(".lock")]
        if sockets:
            wayland_display = os.path.basename(sockets[0])

    uid = os.getuid()
    user = os.environ.get("USER") or os.getlogin()

    proc = await asyncio.create_subprocess_exec(
        "sudo", "--non-interactive",
        "ip", "netns", "exec", netns,
        "sudo", "-u", user,
        "env",
        f"HOME={Path.home()}",
        f"WAYLAND_DISPLAY={wayland_display}",
        f"XDG_RUNTIME_DIR={runtime_dir}",
        f"DISPLAY={os.environ.get('DISPLAY', '')}",
        "firefox",
        "--profile", str(profile_dir),
        "--new-instance",
        "--no-remote",
        "--name", f"Pessoa-{slug}",
        "--class", f"Pessoa-{slug}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    return proc.pid
