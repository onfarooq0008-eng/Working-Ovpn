#!/usr/bin/env python3
"""Fetch VPN Gate servers, test real OpenVPN connections, keep the fastest working ones."""
import base64
import csv
import json
import os
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

API = "https://www.vpngate.net/api/iphone/"
MAX_KEEP = int(os.getenv("MAX_KEEP", "15"))              # how many working servers to save
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "80"))  # how many to test
WORKERS = int(os.getenv("WORKERS", "8"))
TIMEOUT = int(os.getenv("TEST_TIMEOUT", "25"))           # seconds per server

OUT = Path("servers")
TMP = Path("/tmp/vpngate")
REPO = os.getenv("GITHUB_REPOSITORY", "USER/REPO")
BRANCH = os.getenv("GITHUB_REF_NAME", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/servers"


def fetch_rows():
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5)
    else:
        raise SystemExit(f"Could not fetch VPN Gate list: {last}")

    lines = [l for l in data.splitlines() if l.strip() and not l.startswith("*")]
    lines[0] = lines[0].lstrip("#")
    rows = list(csv.DictReader(lines))
    out, seen = [], set()
    for r in rows:
        try:
            r["_speed"] = int(r["Speed"])
        except (KeyError, ValueError):
            continue
        if not r.get("OpenVPN_ConfigData_Base64") or r["IP"] in seen:
            continue
        seen.add(r["IP"])
        out.append(r)
    out.sort(key=lambda r: r["_speed"], reverse=True)
    return out[:MAX_CANDIDATES]


def test_server(args):
    idx, row = args
    conf = TMP / f"{idx}.ovpn"
    log = TMP / f"{idx}.log"
    config = base64.b64decode(row["OpenVPN_ConfigData_Base64"]).decode("utf-8", "ignore")
    config = config.replace("\r\n", "\n").replace("\r", "\n")
    conf.write_text(config)
    log.write_text("")

    cmd = [
        "sudo", "openvpn", "--config", str(conf),
        "--dev", f"tun{idx}",
        "--route-nopull",            # don't touch the runner's routing
        "--connect-timeout", "10",
        "--connect-retry-max", "1",
        "--data-ciphers", "AES-256-GCM:AES-128-GCM:AES-256-CBC:AES-128-CBC",
        "--data-ciphers-fallback", "AES-128-CBC",
        "--verb", "3",
        "--log", str(log),
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
    print(f"{'OK  ' if ok else 'FAIL'} {row['IP']:15} {row['CountryShort']} {row['_speed']/1e6:.1f} Mbps ({elapsed:.1f}s)", flush=True)
    return ok, row, config, elapsed


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    rows = fetch_rows()
    print(f"Testing {len(rows)} candidates...")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(test_server, enumerate(rows)))

    good = [r for r in results if r[0]]
    good.sort(key=lambda r: r[1]["_speed"], reverse=True)
    good = good[:MAX_KEEP]

    if not good:
        print("No working servers found; keeping previous list.")
        return

    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.ovpn"):
        f.unlink()

    servers = []
    for _, row, config, elapsed in good:
        name = f"{row['CountryShort']}-{row['IP'].replace('.', '-')}"
        (OUT / f"{name}.ovpn").write_text(config)
        servers.append({
            "name": name,
            "ip": row["IP"],
            "country": row["CountryLong"],
            "speed_mbps": round(row["_speed"] / 1e6, 1),
            "ping_ms": row["Ping"],
            "connect_seconds": round(elapsed, 1),
            "sessions": row["NumVpnSessions"],
            "url": f"{RAW}/{name}.ovpn",
        })

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    Path("servers.json").write_text(json.dumps({"updated": now, "servers": servers}, indent=2))
    Path("servers.txt").write_text("\n".join(s["url"] for s in servers) + "\n")

    md = [f"# Working VPN Gate servers\n\nUpdated: {now}\n",
          "| Name | Country | Speed (Mbps) | Ping | Link |", "|---|---|---|---|---|"]
    for s in servers:
        md.append(f"| {s['name']} | {s['country']} | {s['speed_mbps']} | {s['ping_ms']} | [ovpn]({s['url']}) |")
    Path("servers.md").write_text("\n".join(md) + "\n")
    print(f"Saved {len(servers)} servers.")


if __name__ == "__main__":
    main()
