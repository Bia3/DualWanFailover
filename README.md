DualWAN Manager — systemd-networkd + Cloudflare

This repository provides a small, self-contained Python script that:
- Discovers candidate interfaces managed by systemd-networkd
- Tests external connectivity per interface (reachability, packet loss, latency)
- Chooses the best interface and updates the default route accordingly
- Detects the public IPv4 via the chosen interface
- Updates a Cloudflare A record for a specified FQDN with that IP

Important notes
- Interfaces are defined by files in /etc/systemd/network/xx-<interface>.network
  - To disable an interface from consideration, append "disabled" to the end of the network file name and run: networkctl reload
- Requires Linux with systemd-networkd, iproute2, ping, and curl.
- Root privileges are required for modifying routes. You can use DRY_RUN=1 to preview actions without making changes.

Quick start
1) Install prerequisites
   - Python 3.8+
   - iproute2 (ip), iputils-ping (ping), curl

2) Clone or copy this repo to your server and make the script executable
   chmod +x dualwan_manager.py

3) Export required environment variables
   - FQDN: fully qualified domain name to update (e.g., host.example.com)
   - CF_API_TOKEN: Cloudflare API token with DNS edit permissions for the zone
   - Optional (if not supplying CF_ZONE_ID):
     - CF_ZONE_NAME: the base zone name (e.g., example.com), or the script can infer it from FQDN
   - Optional:
     - CF_ZONE_ID: if you already know the zone id
     - TEST_TARGETS: comma separated list of hosts/IPs to ping (default: 1.1.1.1,8.8.8.8)
     - PING_COUNT: number of pings per target (default: 4)
     - PING_TIMEOUT: ping timeout seconds (default: 2)
     - DRY_RUN: set 1 to avoid changing routes or DNS
     - VERBOSE: set 1 for detailed logs
     - PRIMARY_PUBLIC_IP: if set, prefer the interface whose detected public IPv4 equals this value; overrides metric-based ranking when matched

   Example:
   export FQDN=host.example.com
   export CF_API_TOKEN=YOUR_TOKEN_HERE
   export CF_ZONE_NAME=example.com
   export TEST_TARGETS=1.1.1.1,8.8.8.8,9.9.9.9
   export DRY_RUN=1

4) Run the script (with sudo if you want it to actually change routes)
   sudo ./dualwan_manager.py --verbose

What the script does
- Discovers interfaces
  - Reads /etc/systemd/network/*.network (skips files whose name ends with "disabled")
  - Only considers interfaces that currently have at least one public IPv4 address assigned
  - Attempts to infer interface name from file name pattern "NN-iface.network" or from a Match Name= in the file
- Tests each interface
  - For each target in TEST_TARGETS, runs: ping -4 -I <iface> -c <count> -W <timeout> <target>
  - Aggregates these per-interface metrics: reachability, average packet loss, average RTT
- Chooses the best interface
  - If PRIMARY_PUBLIC_IP is set and any viable interface presents that public IP, it will be selected regardless of metrics
  - Otherwise, ranks by lowest packet loss, then lowest average RTT
  - Discards interfaces with 100% loss to all targets
- Updates default route
  - Uses ip route replace default via <gw> dev <iface> [metric X] if a gateway is known on that interface, otherwise ip route replace default dev <iface>
- Detects public IP
  - Queries https://api.ipify.org via the chosen interface (curl --interface)
  - You can override the service via IP_DISCOVERY_URL env var
- Updates Cloudflare DNS A record
  - Finds Zone ID via CF_ZONE_ID, or determines it from CF_ZONE_NAME or from the FQDN itself
  - Upserts the A record for FQDN to your detected public IP

Security and permissions
- The script must run as root to modify routes. Without root, use DRY_RUN=1 to preview actions.
- The CF_API_TOKEN should have permissions: Zone:DNS:Edit at minimum for the target zone.

Operational tips
- To stop considering an interface: rename its .network file to append "disabled" and run networkctl reload.
- Consider a cron/systemd timer to run this script periodically and keep DNS updated as failover occurs.
- You can provide additional external targets (TEST_TARGETS) that are relevant to your traffic.

Exit codes
- 0: success
- 1: runtime error (e.g., failed to modify route or DNS)
- 2: configuration error (missing env vars, zone not found)
- 3: no viable interfaces discovered or all tests failed

Limitations
- The script approximates external packet loss and hop latency using ICMP echo to public resolvers; tailor TEST_TARGETS to reflect your needs.
- IPv4 only at present for both testing and DNS record updates.

License
- MIT