## 21.10.2026 - Mobile remote controls

- Published the deployed companion API source, including device-authentication diagnostics, reviewed Wing order extensions and evidence-based reconciliation of purchase deviations. Existing quantity, budget, ownership and lease checks remain enforced for normal purchases.
- Release verification: all 161 backend tests and 31 subtests passed, including the real two-client protocol against isolated journals. The test explicitly enables the dormant client sync protocol without changing production defaults.

- Deployed the compatible remote-screen API alongside the mobile dashboard. Linux API tests and 29 public HTTPS checks passed; the local paired ANKe client now publishes all 28 configured touch actions. Previous code and the remote metadata database were backed up for rollback.

- Forward validated Elite key bindings to the tablet remote so touch controls follow each commander's local configuration; older clients remain compatible.

# Changelog

## 20.10.2026 - Diagnose rejected ANKe device authentication

- Authentication failures now distinguish unknown devices, invalid keys and malformed headers using only a device-ID fingerprint. Repeated messages are throttled; credentials and authorization headers are never logged.

## 20.10.2026 - Reconcile proven purchase quantity deviations

- Originating clients can reconcile an already executed purchase whose original journal and exact cargo delta prove a quantity or cost deviation. The ledger records actual expenses once, including spending above the old reservation; further purchases remain subject to the shared total budget. Normal transaction submissions keep the original quantity and cost limits.

## 20.10.2026 - ANKe client sync regression coverage

- Explicitly reviewed Wing starts can add missions to an existing order while retaining its ID and shared credit ledger. Supply order, total budget, pending-transaction guards, exclusive lease and revision checks remain enforced.

- The two-client integration check now verifies that unchanged mission syncs leave the client preview stable, and supports the independent client lease-renewal lock.


## 20.10.2026 - v1.9.0 - Cross-client Wing Trade and remote diagnostics

- Added a separate application release version in `VERSION`; legacy API compatibility remains at 1.8.0. This release also includes the remote screen service, tenant request isolation, dashboard query improvements and conflict alert graphics described below.
- Deployed the data API and dashboard to production, raised the device upload limit to 9 MiB, and passed 24 public HTTPS checks. Temporary probe records and deployment backups were removed after verification.

- Added persistent account-scoped Wing missions, portable orders, shared spending limits and fenced execution leases for paired ANKe clients, with journal/cargo receipt validation and safe handling of uncertain transactions.
- Added resumable compressed uploads of ANKe, remote and Elite journal logs, plus an independent operational message feed. Diagnostic archives expire after 30 days; view permissions are checked on every read and download page.

## 28.09.2026 - Opt-in ANKe remote screens

- Added device pairing, owner-managed view/control grants and short-lived exclusive control sessions for ANKe clients. Tenant and administrator roles never bypass device access rules.
- Added bounded assist command results, media-room rotation on access revocation and separate device metadata storage; no video or raw keyboard events are recorded.
- Included self-hosted LiveKit/TURN deployment templates and authorization/session tests. Server enablement and production acceptance remain explicit deployment steps.
- Deployed LiveKit, certificates and SNI routing alongside existing VALK/UPS services. Verified real Elite streaming and five synthetic streams with fifteen receivers over forced TURN/TLS; removed old application backups after successful function checks while retaining the fresh rollback snapshot.

## 27.09.2026 - Faster dashboard API and isolated concurrent requests

- Watchlist history selects the existing system/time index when available, avoiding a slow scan of settled snapshots.
- Added tenant leaderboard indexes and a compact Data explorer filter catalog. Global search now applies Unicode-aware literal matching before pagination, so users no longer download entire tables to search.
- Tenant database sessions are isolated per request and connections are pooled per database. Four bounded API workers can serve requests concurrently; exception teardown rolls back uncommitted work.
- Alert lists fetch notification delivery information in one batch. Added concurrency, tenant isolation, catalog, search and index-compatibility tests.
- Deployed on 27 September after verified online backups of all three tenant databases. All 136 backend tests and 31 subtests pass; server-runtime tests and twelve concurrent production reads confirm tenant isolation. VALK Development's 86-system watchlist now returns in approximately 0.09–0.16 seconds in individual checks, down from 5.7 seconds. Seven additive indexes are installed across the tenants.

## 12.09.2026 - Conflict details in Discord graphics

- New-conflict and conflict-update graphics now include Won Days and Stakes for both factions in the previous and alert snapshots, while retaining the existing 1000 x 285 pixel image size.
- Factions are matched by name and pair, preserving their display order even if the source order changes. Zero scores and unavailable scores remain distinct; long labels fit within their columns.
- Full conflict details remain in the accompanying Discord text. Graphics use the saved historical comparison rather than current live scores.
- Translated this changelog into English.
- Validation: all 13 graphics tests passed, including image dimensions, pair matching, reversed order, zero/missing values, long labels and missing snapshots. A rendered example was visually checked.

- Deployment: graphics backend activated on 12.09.2026 at 03:26 CEST; installed source matches the tested file. The service is active and health checks pass for all three tenants. These changes have not yet been committed.

## 11.09.2026 - BGS alerts and conflict updates

- Conflict warnings include both factions' won days and stakes. Each new settled system tick can trigger a follow-up update, resolving earlier warnings. Multiple conflicts are handled separately, with no duplicate alerts for the same tick.
- Gap warnings are suppressed for faction pairs in pending or active conflicts. A cleanup script removes redundant existing alerts after creating a backup.
- Admins can mark every alert visible to them as read. Only their own read status in the current tenant changes.
- Users can resolve and delete their own personal alerts; admins can do so for shared alerts. Resolved alerts are automatically removed after ten days, and deleted events are not recreated.
- Discord alerts receive comparison graphics; conflict text and snapshot fields show scores and stakes.
- Rule, permission, cleanup and presentation tests cover these changes. The VALK Dashboard changelog contains the complete user-facing overview.
