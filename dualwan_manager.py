#!/usr/bin/env python3
"""
Dual WAN interface tester and Cloudflare A-record updater.

Features
- Discovers candidate interfaces from /etc/systemd/network/*.network whose filenames do NOT end with "disabled".
- Tests each interface by pinging one or more external targets using that interface (-I iface).
  Metrics gathered: reachability, packet loss, average RTT.
- Chooses the best interface by lowest packet loss, then lowest latency.
- Switches the system default route to the selected interface using `ip route`.
- Detects the current public IPv4 of the selected interface (via curl --interface).
- Updates a Cloudflare A record for a configured FQDN with that IP.

Notes and assumptions
- Requires: Linux, iproute2 (ip), ping, curl, and systemd-networkd managing interfaces.
- Must be run with root privileges to change routes. Without root, it can operate in --dry-run mode.
- Interface discovery assumes filename pattern like "10-eth0.network" -> interface "eth0".
  If a [Match] Name=... is present in the file, it will be used instead.
- "External" reachability is approximated by pinging well-known targets via the interface.

Configuration via environment variables
- FQDN: required, the record to update (e.g., host.example.com)
- CF_API_TOKEN: required, Cloudflare API token with DNS edit on the zone
- CF_ZONE_ID: optional; if missing, the script will try to discover it from FQDN
- CF_ZONE_NAME: optional; if present, used to discover the zone id (e.g., example.com)
- TEST_TARGETS: optional; comma-separated list of IPv4/hostnames to ping (default: 1.1.1.1,8.8.8.8)
- PING_COUNT: optional; number of echo requests per target (default: 4)
- PING_TIMEOUT: optional; timeout per ping in seconds (default: 2)
- DRY_RUN: optional; set to 1 to avoid making changes (routes or DNS)
- VERBOSE: optional; set to 1 for more output

Usage
  sudo ./dualwan_manager.py [--dry-run] [--verbose]

Exit codes
  0 success, 1 generic failure, 2 config error, 3 no viable interfaces
"""

import json
import os
import re
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import ipaddress

NETWORK_DIR = Path('/etc/systemd/network')
HOSTS_FILE = Path('/etc/hosts')

@dataclass
class IfaceResult:
    iface: str
    reachable: bool
    packet_loss: float  # in percent (0-100)
    avg_rtt_ms: Optional[float]  # None if unavailable
    samples: int


def log(msg: str, verbose_only: bool = False):
    verbose = os.environ.get('VERBOSE') == '1' or '--verbose' in sys.argv
    if verbose_only and not verbose:
        return
    print(msg, flush=True)


def run_cmd(cmd: List[str], check: bool = False) -> Tuple[int, str, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate()
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return proc.returncode, out, err


def discover_interfaces() -> List[str]:
    if not NETWORK_DIR.exists():
        log(f"Network dir {NETWORK_DIR} not found", True)
        return []
    ifaces: List[str] = []
    for p in sorted(NETWORK_DIR.glob('*.network*')):
        # Skip filenames ending with 'disabled'
        if str(p).endswith('disabled'):
            continue
        # Determine iface name
        name = None
        # Try parse from filename pattern xx-<iface>.network*
        m = re.match(r'.*/\d+-(.+?)\.network', str(p))
        if m:
            name = m.group(1)
        # Try parse Name= from file
        if name is None:
            try:
                txt = p.read_text(errors='ignore')
                mm = re.search(r'(?m)^Name=([^\n]+)$', txt)
                if mm:
                    # May contain spaces or wildcards, take first token
                    name = mm.group(1).strip().split()[0]
            except Exception:
                pass
        if name:
            if name not in ifaces:
                ifaces.append(name)
    return ifaces


def get_iface_ipv4_addrs(iface: str) -> List[ipaddress.IPv4Address]:
    """Return list of IPv4 addresses assigned to the interface (non-deleted)."""
    rc, out, err = run_cmd(['ip', '-4', '-o', 'addr', 'show', 'dev', iface])
    if rc != 0:
        return []
    addrs: List[ipaddress.IPv4Address] = []
    for line in out.splitlines():
        # format: <idx>: <iface>    inet 203.0.113.5/24 brd ... scope global ...
        parts = line.split()
        if 'inet' in parts:
            try:
                ip_cidr = parts[parts.index('inet') + 1]  # e.g., 203.0.113.5/24
                ip_str = ip_cidr.split('/')[0]
                addrs.append(ipaddress.IPv4Address(ip_str))
            except Exception:
                continue
    return addrs


def has_public_ipv4(iface: str) -> bool:
    """True if interface has at least one globally-routable IPv4 (public)."""
    for ip in get_iface_ipv4_addrs(iface):
        # Use is_global which excludes private, loopback, link-local, multicast, reserved, and special ranges like 100.64/10
        if ip.is_global:
            return True
    return False


def ping_via_interface(iface: str, target: str, count: int = 4, timeout: int = 2) -> Tuple[bool, float, Optional[float]]:
    # Use system ping with -I iface, -c count, -W timeout
    cmd = ['ping', '-4', '-I', iface, '-c', str(count), '-W', str(timeout), target]
    rc, out, err = run_cmd(cmd)
    text = out + err
    # Determine packet loss and avg RTT from summary
    # Look for a line like: "3 packets transmitted, 3 received, 0% packet loss, time 2002ms"
    m = re.search(r'(\d+) packets transmitted, (\d+) (?:packets )?received, (\d+\.?\d*)% packet loss', text)
    if not m:
        # Busybox ping different format; try alternative
        m = re.search(r'transmitted, (\d+)[^,]*, received, (\d+)', text)
    if m:
        try:
            tx = int(m.group(1))
            rx = int(m.group(2))
            loss = max(0.0, float(100.0 * (tx - rx) / max(1, tx)))
        except Exception:
            loss = 100.0
    else:
        loss = 100.0
    # avg RTT line like: rtt min/avg/max/mdev = 7.808/8.317/8.744/0.361 ms
    rtt = None
    m2 = re.search(r'rtt [^=]*= ([\d.]+)/([\d.]+)/', text)
    if m2:
        try:
            rtt = float(m2.group(2))
        except Exception:
            rtt = None
    reachable = (loss < 100.0)
    return reachable, loss, rtt


def evaluate_interface(iface: str, targets: List[str], count: int, timeout: int) -> IfaceResult:
    total_tx = 0
    total_rx_loss = 0.0
    rtts: List[float] = []
    reachable_any = False
    for t in targets:
        reachable, loss, rtt = ping_via_interface(iface, t, count=count, timeout=timeout)
        reachable_any = reachable_any or reachable
        total_rx_loss += loss
        total_tx += 1
        if rtt is not None:
            rtts.append(rtt)
        log(f"Test {iface} -> {t}: reachable={reachable} loss={loss:.1f}% avg_rtt={rtt}", True)
    avg_loss = total_rx_loss / max(1, total_tx)
    avg_rtt = sum(rtts) / len(rtts) if rtts else None
    return IfaceResult(iface=iface, reachable=reachable_any, packet_loss=avg_loss, avg_rtt_ms=avg_rtt, samples=total_tx)


def score_iface(res: IfaceResult) -> Tuple[float, float]:
    # Lower is better: (loss, rtt or large)
    rtt = res.avg_rtt_ms if res.avg_rtt_ms is not None else 1e9
    return (res.packet_loss, rtt)


def get_default_gateway_for_iface(iface: str) -> Tuple[Optional[str], Optional[int]]:
    # Parse ip route for lines like: default via 192.0.2.1 dev eth0 proto dhcp metric 100
    rc, out, err = run_cmd(['ip', '-4', 'route', 'show', 'dev', iface])
    gw = None
    metric = None
    for line in out.splitlines():
        if not line.startswith('default '):
            continue
        parts = line.split()
        try:
            if 'via' in parts:
                gw = parts[parts.index('via') + 1]
            if 'metric' in parts:
                metric = int(parts[parts.index('metric') + 1])
        except Exception:
            pass
    return gw, metric


def ensure_default_route(iface: str, dry_run: bool = False) -> None:
    gw, metric = get_default_gateway_for_iface(iface)
    cmd: List[str]
    if gw:
        cmd = ['ip', 'route', 'replace', 'default', 'via', gw, 'dev', iface]
    else:
        # Fallback: default via device only
        cmd = ['ip', 'route', 'replace', 'default', 'dev', iface]
    if metric is not None:
        cmd += ['metric', str(metric)]
    log('Would run: ' + ' '.join(shlex.quote(c) for c in cmd))
    if not dry_run:
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            raise RuntimeError(f"Failed to set default route: {err or out}")


def get_public_ip_via_iface(iface: str) -> str:
    # Use curl to respect interface selection easily
    api = os.environ.get('IP_DISCOVERY_URL', 'https://api.ipify.org')
    cmd = ['curl', '--silent', '--show-error', '--max-time', '10', '--ipv4', '--interface', iface, api]
    rc, out, err = run_cmd(cmd)
    if rc != 0:
        raise RuntimeError(f"Failed to detect public IP via {iface}: {err or out}")
    ip = out.strip()
    # Strict IPv4 validation using ipaddress
    try:
        ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError:
        raise RuntimeError(f"Invalid IP returned by discovery service: {ip}")
    return ip


class CloudflareClient:
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base = 'https://api.cloudflare.com/client/v4'

    def _req(self, method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None) -> Dict:
        import urllib.request
        import urllib.parse
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        data = None
        headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
        }
        if body is not None:
            data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
        except Exception as e:
            raise RuntimeError(f"Cloudflare API error: {e}")
        try:
            j = json.loads(raw.decode('utf-8'))
        except Exception:
            raise RuntimeError(f"Cloudflare API returned non-JSON: {raw!r}")
        if not j.get('success', False):
            errs = j.get('errors')
            raise RuntimeError(f"Cloudflare API error response: {errs}")
        return j

    def get_zone_id(self, zone_name: str) -> Optional[str]:
        j = self._req('GET', '/zones', params={'name': zone_name, 'status': 'active', 'per_page': 1})
        res = j.get('result') or []
        if res:
            return res[0]['id']
        return None

    def find_best_zone_for_fqdn(self, fqdn: str) -> Tuple[Optional[str], Optional[str]]:
        parts = fqdn.rstrip('.').split('.')
        # Iterate suffixes from longest to shortest (min two labels)
        suffixes = ['.'.join(parts[i:]) for i in range(1, len(parts))]  # e.g., sub.example.com, example.com, com
        for zone in suffixes:
            if zone.count('.') < 1:  # skip TLD-only
                continue
            z_id = self.get_zone_id(zone)
            if z_id:
                return zone, z_id
        return None, None

    def find_record(self, zone_id: str, name: str, rtype: str = 'A') -> Optional[Dict]:
        j = self._req('GET', f'/zones/{zone_id}/dns_records', params={'type': rtype, 'name': name, 'per_page': 1})
        res = j.get('result') or []
        return res[0] if res else None

    def upsert_a_record(self, zone_id: str, name: str, ip: str, proxied: Optional[bool] = None, ttl: int = 120) -> Dict:
        existing = self.find_record(zone_id, name, 'A')
        payload = {'type': 'A', 'name': name, 'content': ip, 'ttl': ttl}
        if proxied is not None:
            payload['proxied'] = proxied
        if existing:
            rec_id = existing['id']
            return self._req('PUT', f'/zones/{zone_id}/dns_records/{rec_id}', body=payload)
        else:
            return self._req('POST', f'/zones/{zone_id}/dns_records', body=payload)


def parse_targets(env_value: Optional[str]) -> List[str]:
    # Build a list of safe targets (IPv4 or hostname) to ping.
    # Rules:
    # - Reject any target that starts with '-'
    # - From /etc/hosts, only consider lines with a global IPv4; prefer hostnames containing a dot; if none, fall back to the IP
    # - Cap total number of targets to 20
    # - Deduplicate while preserving order
    def _is_safe_target(t: str) -> bool:
        return bool(t) and not t.startswith('-')

    seen = set()
    targets: List[str] = []

    # Parse /etc/hosts to discover sane defaults
    if HOSTS_FILE.exists():
        try:
            with HOSTS_FILE.open('r') as file:
                for raw in file:
                    line = raw.split('#', 1)[0].strip()
                    if not line:
                        continue
                    toks = line.split()
                    if len(toks) < 2:
                        continue
                    ip_str = toks[0]
                    try:
                        ip = ipaddress.IPv4Address(ip_str)
                    except ipaddress.AddressValueError:
                        continue
                    if not ip.is_global:
                        continue
                    # Accept hostnames with a dot and not starting with '-'
                    hostnames = [h for h in toks[1:] if ('.' in h and _is_safe_target(h))]
                    candidates = hostnames if hostnames else [ip_str]
                    for c in candidates:
                        if c not in seen and _is_safe_target(c):
                            seen.add(c)
                            targets.append(c)
                            if len(targets) >= 20:
                                break
                    if len(targets) >= 20:
                        break
        except Exception as e:
            log(f"Warning: Failed to read {HOSTS_FILE}: {e}", True)

    # Add targets from environment if provided
    if env_value:
        for s in env_value.split(','):
            t = s.strip()
            if not _is_safe_target(t):
                continue
            # Accept either IPv4 (any), or hostname with a dot
            ok = False
            try:
                # Allow any syntactically valid IPv4; ping will decide reachability
                ipaddress.IPv4Address(t)
                ok = True
            except ipaddress.AddressValueError:
                ok = ('.' in t)
            if ok and t not in seen:
                seen.add(t)
                targets.append(t)
                if len(targets) >= 20:
                    break

    # Fallback to default targets if none found
    if not targets:
        targets = ['1.1.1.1', '8.8.8.8']

    # Final cap and return
    return targets[:20]


def main() -> int:
    dry_run = os.environ.get('DRY_RUN') == '1' or ('--dry-run' in sys.argv)
    verbose = os.environ.get('VERBOSE') == '1' or ('--verbose' in sys.argv)
    fqdn = os.environ.get('FQDN')
    if not fqdn:
        print('Error: FQDN environment variable is required', file=sys.stderr)
        return 2
    cf_token = os.environ.get('CF_API_TOKEN')
    if not cf_token:
        print('Error: CF_API_TOKEN environment variable is required', file=sys.stderr)
        return 2
    zone_id = os.environ.get('CF_ZONE_ID')
    zone_name = os.environ.get('CF_ZONE_NAME')
    targets = parse_targets(os.environ.get('TEST_TARGETS'))
    ping_count = int(os.environ.get('PING_COUNT', '4'))
    ping_timeout = int(os.environ.get('PING_TIMEOUT', '2'))

    # Optional: prioritize an interface whose detected public IP matches this value
    primary_public_ip = os.environ.get('PRIMARY_PUBLIC_IP')
    if primary_public_ip:
        try:
            socket.inet_aton(primary_public_ip)
            log(f"PRIMARY_PUBLIC_IP set: will prefer interface advertising {primary_public_ip}", True)
        except OSError:
            print(f"Warning: PRIMARY_PUBLIC_IP is not a valid IPv4 address: {primary_public_ip}", file=sys.stderr)
            primary_public_ip = None

    log(f"Discovering interfaces from {NETWORK_DIR}")
    ifaces = discover_interfaces()
    if not ifaces:
        print('No interfaces discovered from systemd-networkd configs', file=sys.stderr)
        return 3
    log('Discovered interfaces: ' + ', '.join(ifaces))

    # Filter to only interfaces that have a public IPv4 assigned
    public_ifaces: List[str] = []
    for i in ifaces:
        if has_public_ipv4(i):
            public_ifaces.append(i)
        else:
            log(f"Skipping {i}: no public IPv4 assigned", True)

    if not public_ifaces:
        print('No interfaces with a public IPv4 address found', file=sys.stderr)
        return 3

    log('Candidate interfaces (public IPv4): ' + ', '.join(public_ifaces))

    results: List[IfaceResult] = []
    for iface in public_ifaces:
        try:
            res = evaluate_interface(iface, targets, count=ping_count, timeout=ping_timeout)
            results.append(res)
        except Exception as e:
            log(f"Error testing {iface}: {e}")

    viable = [r for r in results if r.reachable and r.packet_loss < 100.0]
    if not viable:
        print('No viable interfaces (all tests failed)', file=sys.stderr)
        return 3

    # If a PRIMARY_PUBLIC_IP is specified, try to find a viable interface that presents this IP
    selected_iface_name: Optional[str] = None
    detected_ips: Dict[str, str] = {}
    if primary_public_ip:
        for r in viable:
            try:
                ip = get_public_ip_via_iface(r.iface)
                detected_ips[r.iface] = ip
                log(f"Detected public IP via {r.iface}: {ip}", True)
                if ip == primary_public_ip:
                    selected_iface_name = r.iface
                    log(f"Prioritizing interface {r.iface} due to PRIMARY_PUBLIC_IP match {primary_public_ip}")
                    break
            except Exception as e:
                log(f"Could not detect public IP via {r.iface}: {e}", True)

    if selected_iface_name is None:
        viable.sort(key=score_iface)
        log('Interface ranking (best first):')
        for r in viable:
            log(f" - {r.iface}: loss={r.packet_loss:.1f}% rtt={r.avg_rtt_ms if r.avg_rtt_ms is not None else 'n/a'}")
        best = viable[0]
    else:
        # Find its result object
        best = next(r for r in viable if r.iface == selected_iface_name)

    log(f"Selected interface: {best.iface}")

    try:
        ensure_default_route(best.iface, dry_run=dry_run)
    except Exception as e:
        print(f"Failed to update default route: {e}", file=sys.stderr)
        return 1

    try:
        public_ip = get_public_ip_via_iface(best.iface)
    except Exception as e:
        print(f"Failed to detect public IP: {e}", file=sys.stderr)
        return 1
    log(f"Public IP via {best.iface}: {public_ip}")

    cf = CloudflareClient(cf_token)
    # Resolve zone id if needed
    if not zone_id:
        if not zone_name:
            zone_name, zone_id = cf.find_best_zone_for_fqdn(fqdn)
        else:
            zone_id = cf.get_zone_id(zone_name)
    if not zone_id:
        print('Unable to determine Cloudflare zone id. Set CF_ZONE_ID or CF_ZONE_NAME.', file=sys.stderr)
        return 2

    log(f"Cloudflare zone: {zone_name or 'id only'} ({zone_id})")

    if dry_run:
        print(f"DRY-RUN: Would update Cloudflare A {fqdn} -> {public_ip}")
        return 0

    try:
        cf.upsert_a_record(zone_id, fqdn, public_ip)
    except Exception as e:
        print(f"Failed to upsert Cloudflare DNS record: {e}", file=sys.stderr)
        return 1

    print(f"Updated Cloudflare A {fqdn} -> {public_ip} via interface {best.iface}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
