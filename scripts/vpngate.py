#!/usr/bin/env python3
"""Fetch VPN Gate servers, test real OpenVPN connections, keep the fastest working ones."""
import base64
import csv
import json
import os
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

MAX_KEEP = int(os.getenv("MAX_KEEP", "100"))               # working servers to save
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "400"))  # servers to test
WORKERS = int(os.getenv("WORKERS", "20"))
TIMEOUT = int(os.getenv("TEST_TIMEOUT", "20"))            # seconds per server

OUT = Path("servers")
TMP = Path("/tmp/vpngate")
REPO = os.getenv("GITHUB_REPOSITORY", "USER/REPO")
BRANCH = os.getenv("GITHUB_REF_NAME", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/servers"
UA = {"User-Agent": "Mozilla/5.0"}


def http_get(url, headers=None, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"{url}: {last}")


def norm(config):
    return config.replace("\r\n", "\n").replace("\r", "\n")


# ---------- Source 1: VPN Gate (public API, no key) ----------
def from_vpngate():
    try:
        data = http_get("https://www.vpngate.net/api/iphone/")
    except Exception as e:  # noqa: BLE001
        print("VPN Gate failed:", e)
        return []
    lines = [l for l in data.splitlines() if l.strip() and not l.startswith("*")]
    lines[0] = lines[0].lstrip("#")
    out = []
    for r in csv.DictReader(lines):
        try:
            cfg = base64.b64decode(r["OpenVPN_ConfigData_Base64"]).decode("utf-8", "ignore")
            out.append({"source": "vpngate", "ip": r["IP"], "cc": r["CountryShort"],
                        "country": r["CountryLong"], "speed": int(r["Speed"]) / 1e6,
                        "ping": r["Ping"], "config": norm(cfg)})
        except Exception:  # noqa: BLE001
            continue
    print(f"VPN Gate: {len(out)} servers")
    return out


def candidates():
    allc = from_vpngate()
    seen, uniq = set(), []
    for c in sorted(allc, key=lambda c: c["speed"], reverse=True):
        if c["ip"] in seen:
            continue
        seen.add(c["ip"])
        uniq.append(c)
    return uniq[:MAX_CANDIDATES]


# ---------- Connection test ----------
def test_server(args):
    idx, c = args
    conf, log = TMP / f"{idx}.ovpn", TMP / f"{idx}.log"
    conf.write_text(c["config"])
    log.write_text("")
    cmd = [
        "sudo", "openvpn", "--config", str(conf), "--dev", f"tun{idx}",
        "--route-nopull", "--connect-timeout", "10", "--connect-retry-max", "1",
        "--data-ciphers", "AES-256-GCM:AES-128-GCM:AES-256-CBC:AES-128-CBC",
        "--data-ciphers-fallback", "AES-128-CBC", "--verb", "3", "--log", str(log),
    ]
    start = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    while time.time() - start < TIMEOUT:
        if "Initialization Sequence Completed" in log.read_text(errors="ignore"):
            ok = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    elapsed = time.time() - start
    subprocess.run(["sudo", "pkill", "-f", str(conf)], check=False)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"{'OK  ' if ok else 'FAIL'} {c['source']:13} {c['ip']:16} {c['cc']} {c['speed']:.1f} Mbps ({elapsed:.1f}s)", flush=True)
    return ok, c, elapsed


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    cands = candidates()
    print(f"Testing {len(cands)} candidates...")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(test_server, enumerate(cands)))

    good = sorted((r for r in results if r[0]), key=lambda r: r[1]["speed"], reverse=True)[:MAX_KEEP]
    if not good:
        print("No working servers found; keeping previous list.")
        return

    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.ovpn"):
        f.unlink()

    servers = []
    for _, c, elapsed in good:
        name = re.sub(r"[^A-Za-z0-9-]", "-", f"{c['cc']}-{c['ip']}")
        (OUT / f"{name}.ovpn").write_text(c["config"])
        servers.append({"name": name, "source": c["source"], "ip": c["ip"], "country": c["country"],
                        "speed_mbps": round(c["speed"], 1), "ping_ms": c["ping"],
                        "connect_seconds": round(elapsed, 1), "url": f"{RAW}/{name}.ovpn"})

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    Path("servers.json").write_text(json.dumps({"updated": now, "servers": servers}, indent=2))
    Path("servers.txt").write_text("\n".join(s["url"] for s in servers) + "\n")
    md = [f"# Working OpenVPN servers\n\nUpdated: {now}\n",
          "| Name | Source | Country | Speed (Mbps) | Link |", "|---|---|---|---|---|"]
    for s in servers:
        md.append(f"| {s['name']} | {s['source']} | {s['country']} | {s['speed_mbps']} | [ovpn]({s['url']}) |")
    Path("servers.md").write_text("\n".join(md) + "\n")
    print(f"Saved {len(servers)} servers.")


if __name__ == "__main__":
    main()
