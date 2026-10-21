# Current production deployment — 21 October 2026

The mobile dashboard and compatible input-binding API are deployed.

- Follow-up dashboard release: `20261021-mobile-navigation` adds the direct
  Operations → Mobile Remote link, including the mobile navigation drawer.
  Reports and dashboard rollback information are retained in
  `/home/valk/dashboard-v2/shared/20261021-mobile-navigation`; its previous release
  is `20261021-mobile-remote-124704`. The three live API modules `remote_screen.py`,
  `anke_data.py` and `anke_receipts.py` already match the local source checksums.
  All 30 follow-up public HTTPS checks passed, including the authenticated link.

- Public entry: `https://valk-elite.de/remote`.
- Dashboard `current` points to `/home/valk/dashboard-v2/releases/20261021-mobile-navigation`.
- Only `remote_screen.py` was updated in the live backend; unrelated local changes
  to account data and receipt processing were not deployed. `valkflask` restarted
  successfully through its existing systemd restart policy.
- Candidate API tests: 12 passed in Linux. Native dashboard production build passed.
- 29 public HTTPS checks passed, including mobile authentication/assets, configured
  input bindings, manual/assist lease switching and synthetic review/start/stop acknowledgements.
  Temporary probe devices and sessions were removed. No real game input or assist
  start was sent.
- The local paired ANKe source installation was started and reports all 28 touch
  actions, including four panel focus keys and previous/next tabs. The separately
  paired Shadow-PC now also reports all 28 actions and all six panel/tab bindings
  in the follow-up verification. Both devices reported idle capture; real game
  acceptance remains outstanding.
- Backups and rollback manifests: `/home/valk/dashboard-v2/shared/mobile-remote-20261021-124704`.
  The previous dashboard release is `20261020-anke-data`; backend code and an online
  SQLite snapshot were retained. No schema migration was required.
- Elite was not running on the local Windows machine; start it and restart ANKe
  before testing live controls from the iPad.

---

# Previous production deployment — 20 October 2026

The account-scoped Wing Trade data API and remote diagnostics are deployed.

- Dashboard: `/home/valk/dashboard-v2/current` points to
  `/home/valk/dashboard-v2/releases/20261020-anke-data`.
- Backend: `remote_screen.py`, `anke_data.py`, `anke_receipts.py` and
  `anke_mission_protocol.py` are active in `/home/valk/valk`; `valkflask` is active.
- Additive SQLite tables are present; database integrity check succeeded.
- The existing public device API prefix now accepts up to 9 MiB in Nginx;
  global configuration validation and reload succeeded.
- Public dashboard health returns HTTP 200. The Linux production build and
  11 relevant dashboard tests passed before activation.
- 24 public HTTPS checks passed, including authenticated BFF access,
  compressed upload above 64 KiB, exactly 25 operational events, archive
  download/search, isolated account sync across two paired test devices,
  concurrent-lease rejection, owner archive access and revoked-device rejection.
- Probe devices, their diagnostics/account records and the temporary dashboard
  session were removed. No game input, real mission update or external message
  was sent during validation.
- At the user's request, five deployment backup targets were removed after
  successful validation (74,328 bytes); active data and dependency releases remain.
  The supplied local sudo credential file was deleted and sudo cache invalidated.
- Deployment manifests and verification reports remain under
  `/home/valk/dashboard-v2/shared/anke-data-20261020`.

The updated local ANKe source requires an application restart before automatic
uploads are active. Wing synchronization must be enabled for each current FID on
each participating client. A supervised two-PC Elite/depot test remains field
acceptance; the public probes used isolated synthetic records.

---

# Hetzner deployment — 28 September 2026

API, dashboard and LiveKit are deployed on 167.235.65.113. Pairing succeeded
with the user's existing dashboard session. The source ANKe installation is
paired to owner jjt in VALK Development and reports commander FREYA M.
Screen transmission is on; remote control is off. No assist was started and
no keyboard/mouse command was sent to Elite.

## Services and configuration

- LiveKit 1.13.7: valk-livekit.service, /etc/valk-remote/livekit.yaml,
  protected keys.yaml, dedicated livekit Unix account.
- HAProxy: valk-tls-router.service; TLS SNI routes public 443 to Nginx loopback
  8443 or TURN/TLS loopback 5349 with PROXY v2.
- valk-remote-firewall.service restricts direct TCP 5349 to loopback.
  RTC TCP 7881, UDP 7882 and TURN/UDP 3478 are available.
- Let's Encrypt certificate: /etc/letsencrypt/live/valk-remote, expires
  27 December 2026. Renewal hook copies protected certificates, reloads Nginx
  and restarts LiveKit.
- Kernel UDP buffer ceilings: /etc/sysctl.d/90-valk-remote.conf, 16 MiB.
- Flask: existing single-process valkflask.service; additive metadata database
  /home/valk/valk/instance/remote-screen.sqlite. The exact Nginx device API prefix
  /api/remote-screen/v1/ reaches Flask; browser BFF stays on Next.
- Active dashboard with final proxy/build fixes:
  /home/valk/dashboard-v2/releases/20260928-anke-remote-origin.
  Build with `bash deploy/build-native.sh`: Next persists CSP during build.
  CSRF checks use VALK_PUBLIC_URL; remote upstream paths are relative to /api/.
- Deployment manifests: /home/valk/dashboard-v2/shared/anke-remote-20260928.

## Verification

- Dashboard health 200, UPS login page 200 after normal redirect, media 200;
  unauthenticated device list 401. Nginx configuration passes validation.
- Real Elite WGC/H.264 through public server: 387 decoded frames per receiver
  with three receivers in 30 seconds; first image at 3.95 seconds including start.
- Forced TURN/TLS: five synthetic publishers and 15 receivers, 326–373 decoded
  frames each in 30 seconds. All 20 connections confirmed as TURN. TURN/UDP was
  disabled only during this test and restored in a finally block.
- Dashboard boundary/proxy tests: 8 pass locally; production build and seven
  boundary tests pass on Linux. ANKe remote suite: 13 pass.
- Browser end-to-end live viewing: connected, excellent quality, real Elite
  image. Local disable hides the image; re-enabling and reopening restores it.
  Observed image-age samples 0.5–1.3 seconds are not a glass-to-glass benchmark.

The synthetic test does not establish 60 Mbit/s saturation or glass-to-glass p95.
Five real PCs, mixed-DPI/Ultrawide window cases, physical input, browser rights
revocation and two-hour assist operation remain field acceptance tasks.
The user will start their own assist.

## Backup and access cleanup

Fresh rollback snapshot: /var/backups/anke-remote-20260928, root only,
SHA256SUMS verified. Preserve this new deployment snapshot.
Old application backups: /var/backups/ups_lizenz4_server/pre-rc5-20260928
and empty dated phpLiteAdmin, housekeeping and API-performance backup folders.
/home/ups/backup and /home/ups/deploy-backups are already empty.
Cleanup completed after successful function verification: four directories,
three files, 182139 bytes. Exact manifest: shared/anke-remote-20260928/backup-cleanup.json.
Historical reports, active data, release directories and OS package-manager
state are preserved.
Both duplicate temporary root SSH entries marked anke-remote-temporary were
removed after administration. A fresh root login is rejected; normal valk SSH
access was verified. access-cleanup.json records the removal.
