# ANKe Remote deployment

The implementation spans ANKe, the Flask API and VALKDashboardV2. It is off by
default. Use LiveKit **1.13.7**, Python `livekit==1.1.20`, `windows-capture==2.0.1`
and the dashboard's locked `livekit-client`. Video is H.264; audio and recording
are disabled. A media host is trusted with the relayed content (transport TLS/
WebRTC encryption, not application-level end-to-end encryption).

## Configure

1. Install the checksum-verified LiveKit Linux release, HAProxy and Nginx on the
   VALK host. Create an unprivileged `livekit` account. No Docker is required.
2. Point `media.<your-domain>` and `turn.<your-domain>` at the public server.
   Obtain certificates for both; provision TURN's files to `/etc/valk-remote`
   with read access for the livekit group (including a renewal hook).
3. Copy the example files, replace **every** `REPLACE.example`, and generate a
   key using `livekit-server generate-keys`. Save its YAML mapping in
   `/etc/valk-remote/keys.yaml` with mode 0640, root:livekit. Do not use dev keys.
4. Set Flask environment variables:
   ```ini
   REMOTE_SCREEN_ENABLED=true
   REMOTE_SCREEN_DATABASE=/home/valk/valk/instance/remote-screen.sqlite
   REMOTE_LIVEKIT_URL=wss://media.<your-domain>
   REMOTE_LIVEKIT_KEY=<generated-key>
   REMOTE_LIVEKIT_SECRET=<generated-secret>
   DASHBOARD_JWT_SECRET=<same-secret-as-dashboard>
   ```
   Set `REMOTE_LIVEKIT_URL` and `DASHBOARD_JWT_SECRET` in the dashboard as well,
   then rebuild it (CSP includes the media origin). Keep environment files 0600.
5. Run **one Flask process** (the existing threaded Waitress deployment). Remote
   runtime sessions are process-local with a shared lock. Do not deploy multiple
   WSGI workers/replicas without moving runtime coordination into a shared service.
   Persistent metadata and ACLs use a separate SQLite file, backed up with its WAL.
6. For one public IP, reserve TCP 443 for `valk-tls-router`. It dispatches TURN by
   SNI and forwards other TLS to Nginx on 127.0.0.1:8443 with PROXY v2. Both TURN
   and Nginx require PROXY v2 on their private backend listeners. LiveKit 1.13.7
   advertises TURN/TLS on 443 while listening internally on 5349. Update all
   existing HTTPS vhosts accordingly and restore client addresses with
   `set_real_ip_from 127.0.0.1; real_ip_header proxy_protocol;`. Remove competing
   public :443 listeners. HTTP/ACME on :80 remains unchanged. Validate Nginx and
   HAProxy configurations before switching listeners. This requires a planned
   server configuration change; copying the examples alone does not deploy it.
   Include `nginx-device-api.conf.example` in the dashboard HTTPS vhost so ANKe
   can use `https://valk-elite.de` as its API origin. The exact `/api/remote-screen/v1/`
   prefix routes to Flask, while browser BFF requests continue to Next.js.
7. Allow TCP 443 and 7881, UDP 7882 and 3478. Keep 7880, 8443 and TURN's backend
   5349 private; clients reach TURN TLS through 443. Install and enable the two
   systemd units. Check WebRTC UDP and forced TURN/TLS separately from outside.

## API and session contract

User routes below `/api/remote-screen/v1`: `POST pairings`, `GET devices`,
`GET devices/{id}/share-candidates?q=<username>` (owner only, minimum 2 characters),
`PUT devices/{id}/grants` (`user_id`, `rights`), `DELETE devices/{id}`,
`POST devices/{id}/viewers`, `POST|DELETE devices/{id}/viewers/{viewer_id}`,
`POST|DELETE .../control` (`mode=assist|manual`), `POST devices/{id}/commands`
and `GET devices/{id}/commands/{command_id}`. Dashboard BFF calls require an
active database-backed user session, not legacy tenant API keys. ACLs always
apply to admins too. Device routes are `POST pair` (one-time code) and
`POST device/pulse` (`Authorization: Device <id>.<secret>`).

Browser and device pulse every 800 ms. Viewer expiry is 5 seconds; control lease
expiry is 3 seconds. ANKe subtracts network elapsed time from both deadlines and
uses an independent watchdog. Raw input additionally needs a fresh input-state
snapshot within 750 ms. Keyboard/button packets are reliable, mouse movement
lossy, topic `anke.input.v1`, addressed to the device participant. Input includes
`epoch`, strictly increasing `seq`, and `sent_at` in server-adjusted Unix seconds.
Types: `key(code,down)`, `button(button,down)`, `move(x,y)`, `relative(x,y)`,
`wheel(delta)`, `state(keys,buttons)` and `release`. Desktop/system shortcuts are
rejected. No text injection, shell commands or paths are accepted.

Commands are `prepare(assist,viewer_id)`, `confirm(review_id,checks,viewer_id)` and
`stop`. IDs are single-use at the client, bound to configuration and lease. A
command response means queued, not executed: poll its `pending/completed/failed/
expired` result. Start reviews expire after 30 seconds; stops after 3 seconds.
Restart invalidates all runtime sessions. Revoking access rotates the media room
because self-hosted LiveKit tokens cannot be immediately revoked. No frames or
raw keys are persisted. The audit log retains at most 10,000 high-level actions.

## Rollout and acceptance

Deploy the API and dashboard first, then opt in a single ANKe client using its
Remote page. Create the code in Operations → ANKe Remote. Enable viewing first,
then remote control locally. Use windowed/borderless Elite; minimized or failed
capture is shown as unavailable. Desktop Edge/Chrome are the initial manual
control target; phones support viewing and assist actions.

Build the dashboard with `bash deploy/build-native.sh` after setting
`REMOTE_LIVEKIT_URL=wss://media.example.org` and `VALK_PUBLIC_URL` in
`.env.runtime`. Next stores CSP headers at build time; setting the media URL
only at process startup is insufficient. Verify that the deployed CSP includes
both the HTTPS and WSS media origins before opening a viewer.

Validate: owner and explicit grants only; immediate local disable; logged-out
viewers lose new frames within 5s; dropped control lease releases inputs within
3s; lost data packets within 750ms; no reconnect replay; no desktop fallback.
Measure glass-to-glass p95 (<1s) and input response (<500ms at RTT <=100ms) on
the target deployment. Run the two-hour assist coexistence test on a supervised
game session. Neither synthetic media tests nor mocked browser tests establish
those production latency/duration requirements.

For a repeatable local media check, use ANKe's `tools/remote_screen_smoke.py` with
a loopback LiveKit dev server. Default: five synthetic publishers and fifteen
decoders. `--elite` uses the real Elite capture process and three decoders. This
never injects input. `--seconds` controls duration. External testing accepts an
`--access-file` containing short-lived, room-scoped tokens minted on the server;
never export the LiveKit signing secret. `--relay` forces receivers and synthetic
publishers through TURN. To prove TLS specifically, advertise only TURN/TLS for
the isolated test window and restore TURN/UDP immediately afterwards.

Rollback: turn off `REMOTE_SCREEN_ENABLED`, stop the media/router services and
restore original Nginx :443 listeners. Leave the additive metadata database in
place; existing BGS endpoints and user tables are unchanged.


## Paired-client data extension (20 October 2026)

Deploy `anke_data.py`, `anke_receipts.py` and `anke_mission_protocol.py` with
`remote_screen.py`. The additive tables use the same `REMOTE_SCREEN_DATABASE`;
back up the SQLite database and WAL. Restart invalidates execution leases.
Increase `client_max_body_size` for the device prefix from 64k to **9m** using
the updated Nginx include, then validate/reload Nginx. Device APIs still enforce
smaller per-route limits (256 KiB logs, 1 MiB event batches, 9 MiB missions).

Paired clients now pulse even with video disabled. Diagnostics upload automatically;
Wing sync is an explicit per-FID choice in ANKe. `device/diagnostics/logs` accepts
checksummed base64 gzip chunks; `device/diagnostics/events` accepts operational
messages/status. Dashboard reads `devices/{id}/diagnostics` and
`devices/{id}/diagnostics/logs/{stream}?offset=N` under the current view ACL.

`device/wing/accounts` binds an explicitly selected account to a paired device.
`device/wing/{fid}/sync` merges mission evidence; `/lease`, `/order`,
`/transactions` and `/reconcile` coordinate account execution. Only devices with
the same owner and tenant share accounts. Unresolved transactions remain blocked
across lease expiry. Original journal events and exact cargo deltas are required
to settle them; a screen grant never authorizes mission writes.

The existing single-process deployment requirement remains. Archive TTL is
30 days; the latest 25 messages survive until device removal. No raw video,
configuration files or game input are archived. Deploy backend/dashboard before
updated clients. Complete supervised two-PC handoff, depot OCR and network-loss
acceptance before production release; no production deployment is implied by
these source changes.
