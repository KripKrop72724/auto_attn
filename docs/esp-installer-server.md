# State Life Device Installer: server side

The Windows installer app (design: `docs/esp-installer-design.md`) talks to ADD at
`https://autoattn.slichealth.com/api/v1/installer/*`. ADD keeps every secret. The protected
provisioner builds each board's encrypted settings (NVS) and HMAC eFuse key from the fleet root,
and encrypts both to the app's one-time X25519 key. Firmware comes from signed installer bundles
in a read-only store. The dashboard's Prepare page offers the app itself for download from
another read-only store ("App store" below). Code: `apps/add_backend/zk_add/installer_api.py`,
`apps/add_backend/zk_add/installer_apps.py` and `apps/add_provisioner/app.py`.

## Routes

The app signs in with the dashboard's ADD admin username and password (`POST /api/v1/auth/login`).
Every installer route needs the `add_admin` cookie. Mutations also need `X-CSRF-Token`. A package
also needs the admin `password` in the body (step-up) on every call.

| Method and path | Result |
| --- | --- |
| `GET /capabilities` | `{enabled, provisioner_ready, min_app_version, server_time, bundle_count}`. Answers even when the installer is switched off. |
| `GET /bundles[?family=zkt\|hikvision]` | `{bundles: [{bundle_id, family, label, version, recommended, release_state, notes, download_bytes, manifest_sha256}]}` |
| `GET /bundles/{bundle_id}/manifest` | `{manifest, signature}`, exactly as signed |
| `GET /bundles/{bundle_id}/files/{name}` | The file's bytes; only names listed in the manifest. Header `X-Content-SHA256`. |
| `GET /devices/{mac}` | `{known, mac, connector_id, zone_id, zone_name, device_id, family, firmware_version, online, last_seen_at, ever_online, public_name, group, status_text, hik_source_epoch, settings_change_allowed, settings_change_reason}` |
| `GET /zones` | `{zones: [{zone_id, zone_name, device_ids, families}]}` for active connectors |
| `POST /packages` | 201 `{request_id, provisioning_manifest, provisioning_ciphertext_b64, hmac_ciphertext_b64}` |
| `POST /events` | `{request_id, mac, kind, detail}`, then 204 |
| `GET /apps` | `{latest: {version, git_sha, published_at, files: [{platform, arch, kind, filename, size, sha256, download_path}]} \| null, min_app_version}`, for the dashboard's download card |
| `GET /apps/{version}/{filename}` | The app file as an attachment; only files that the version's manifest lists. Header `X-Content-SHA256`. |

Notes:

- **MAC:** `aa:bb:cc:dd:ee:ff`, `AA-BB-…` or 12 hex digits. It is matched the way onboarding stores
  `hardware_id` (lower case, colons).
- **`ever_online`:** true once the board itself has opened its authenticated device stream
  (`Connector.last_seen_at`). Onboarding alone does not count.
- **`public_name` and `group`:** what `/status` calls the board, and Life or Health.
- **`online` and `status_text`:** the `/status` meaning (attendance reaches ADD). The reason is in
  plain words, and times are in Pakistan time.
- **`hik_source_epoch`:** ADD's Hikvision policy epoch, or else the epoch of the latest record.
- **`settings_change_allowed` and `settings_change_reason`:** whether ADD will build a
  `change_settings` package for the board, by the rule in "Boards with a locked key" below. The
  reason is in plain words when it will not, and `null` when it will. An unknown board is allowed.
- **`recommended`:** the newest bundle (`created_at`) marked `recommended` in its family.

`POST /packages` body:

```json
{"password": "…", "request_id": "8-96 of [A-Za-z0-9._-]", "mac": "…",
 "mode": "new_board | resume | change_settings", "bundle_id": "… (new_board and resume)",
 "app_version": "1.0.0",
 "recipient_public_key": "base64 raw 32-byte X25519",
 "configuration": {"firmware_family": "zkt | hikvision", "zone_id": "…", "zone_name": "…",
   "device_id": "…", "wifi_ssid": "…", "wifi_password": "…", "zkt_port": 4370,
   "zkt_comm_key": 0, "zkt_preferred_ip": "0.0.0.0",
   "hik_host": "…", "hik_port": 80, "hik_transport": "http_digest | https", "hik_username": "…",
   "hik_password": "…", "hik_expected_serial": "…", "hik_profile": "…",
   "hik_source_epoch": "…", "hik_ca_pem": ""}}
```

The configuration rules match `ProvisioningConfiguration` and the provisioner's
`validate_request`, except that the Hikvision CA limit is lower than `ProvisioningConfiguration`'s:

- **ZONE ID and device ID:** `^[A-Za-z0-9][A-Za-z0-9._-]*$`, up to 64 and 31 characters.
  `LF-ZONE-G&PBLD8-01` is therefore refused.
- **Zone name:** 1–120 UTF-8 bytes, with no edge spaces and no control characters.
- **Wi-Fi:** the SSID is 1–32 bytes. The password is 8–63 bytes, or 64 hex digits.
- **ZKT settings:** port 1–65535 (default 4370). The comm key is 0–4294967295. The preferred IP
  is `0.0.0.0` or RFC1918.
- **ZKT boards:** must send `zkt_comm_key` and `zkt_preferred_ip`, and must send no `hik_*` value
  (`hik_port` and `hik_transport` are ignored).
- **Hikvision boards:** the comm key is absent or 0. `hik_host` must be an RFC1918 IPv4 address.
  Username, password, serial, profile and epoch are required (up to 63, 127, 119, 79 and 64 bytes).
  The CA is optional, up to 3967 bytes: the most ESP-IDF 5.5.3's NVS generator stores in one
  string, and the provisioner's `nvs_rows` limit. A longer CA is refused as 422 `INVALID_FIELD`
  for `hik_ca_pem`. The transport defaults to `https`. The port defaults to 443 for `https` and 80
  for `http_digest`.
- **Numbers:** must be JSON numbers, not strings. Unknown keys are ignored.
- **`bundle_id`:** required for `new_board` and `resume`, which write firmware. A
  `change_settings` package writes no firmware, so it needs no published bundle. It may leave
  `bundle_id` out or send `null` or `""`, and ADD ignores any bundle it names.

Then ADD checks these in order:

1. For `new_board` and `resume`: the bundle exists, its signature verifies, and its family matches
   the configuration.
2. A board ADD already knows keeps its family. This applies to every mode.
3. For `resume` and `change_settings`: the board's locked key can read settings from the fleet
   root (below).
4. No other active connector uses the same `(zone_id, device_id)`.

Next, ADD commits and calls the provisioner (90 s timeout). It checks that the envelopes are bound
to this request, writes the audit row and returns the package with `Cache-Control: no-store`.

### Boards with a locked key

The provisioner always derives a board's HMAC key, and so its settings encryption, from the fleet
root. A board set up before this app may hold a key from an older root: the historical PII root,
or a split recovery root. New settings from the fleet root would leave it unable to start, and the
app would already have erased its unsent punches with the storage. Nobody can read a locked key,
so ADD decides from its own records:

- **`change_settings`** is allowed when ADD has no connector for the MAC, or the connector has
  never been online (`ever_online` false), or the audit log holds an `INSTALLER_PACKAGE_ISSUED`
  row for the MAC with mode `new_board`. That row means this app set the board up, so its key
  came from the fleet root. A `resume` row proves nothing: a resume burns no key, and the board
  may have been locked before this app with an older root. Nor does a `change_settings` row or a
  reported event. A board this app set up and then finished with a resume still has its
  `new_board` row.
- **`resume`** is allowed only when ADD has no connector for the MAC, or it has never been online.
  A board whose setup stopped has never started.
- **Otherwise** the answer is 409 `SETTINGS_CHANGE_NOT_SUPPORTED`, with the message "This board was
  set up before this app. Only head office can change its settings." Nothing is built or audited.
  Head office changes such a board with the protected `zone-lite-device-provisioning.yml`
  workflow, whose `nvs_root_source` can name the board's own root.

`GET /devices/{mac}` reports the same `change_settings` rule, so the app can say so before
anyone types the settings.

## Errors

The body of every installer error is `{"code", "message", "field"?}`. `detail` stays what ADD's
usual handler sends. For installer refusals it is the same object again. For the shared sign-in
checks it is ADD's own sentence, for example `"Invalid CSRF token."` or
`"Password confirmation failed."`. `message` is in plain words, and no error ever repeats a
submitted value. Other ADD routes keep their own error shape.

| Status | `code` | When |
| --- | --- | --- |
| 401 | `AUTH_REQUIRED`, `SESSION_EXPIRED` | No session, or the session has expired. Sign in again. |
| 403 | `CSRF_INVALID` | The CSRF token is missing or wrong. |
| 403 | `PASSWORD_REJECTED` (`field: password`) | The step-up password was not accepted. |
| 404 | `BUNDLE_NOT_FOUND`, `FILE_NOT_FOUND`, `NOT_FOUND` | Unknown, unverified or unlisted bundle or file, or app version or file; or an unknown path. |
| 409 | `INSTALLER_DISABLED` | The kill switch is on, or provisioning is off. Every route except capabilities. |
| 409 | `BUNDLE_UNAVAILABLE`, `BUNDLE_FAMILY_MISMATCH` (`field: bundle_id`) | The bundle of a `new_board` or `resume` package is missing, unverified, or for the other family. |
| 409 | `FAMILY_MISMATCH` (`field: firmware_family`, `known_family`) | ADD knows this MAC as the other family. |
| 409 | `SETTINGS_CHANGE_NOT_SUPPORTED` | A `resume` or `change_settings` package for a board that may hold a key from an older root ("Boards with a locked key"). Trying again gives the same answer; head office must change the board. |
| 409 | `DEVICE_ID_IN_USE` (`field: device_id`, `suggested_device_id`) | Another active board uses the ID in that zone. The suggestion is the smallest free positive number. |
| 422 | `INVALID_FIELD` (`field`) | The named field breaks a rule, or a `new_board` or `resume` package has no `bundle_id`. `INVALID_REQUEST` means the body is unreadable. |
| 426 | `APP_UPDATE_REQUIRED` | `app_version` is below `ADD_INSTALLER_MIN_APP_VERSION`. |
| 429 | `RATE_LIMITED` (`Retry-After`) | Too many packages or events. `Retry-After` is the number of seconds until the oldest counted request leaves the window, and the message says how many minutes to wait. |
| 502 | `PROVISIONER_UNAVAILABLE` | The provisioner is unreachable, timed out or failed, or returned an unusable package. Nothing was issued. |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `ADD_INSTALLER_ENABLED` | `true` | Kill switch. The installer also needs `ADD_PROVISIONING_ENABLED=true`. |
| `ADD_INSTALLER_BUNDLE_STORE_PATH` | `/installer-bundles` | The store inside add-api. |
| `ADD_INSTALLER_APP_STORE_PATH` | `/installer-apps` | The app store inside add-api. |
| `ADD_INSTALLER_MIN_APP_VERSION` | `1.0.0` | Older apps get 426 on `POST /packages`. |
| `ADD_INSTALLER_PACKAGE_LIMIT` / `ADD_INSTALLER_EVENT_LIMIT` | `30` / `240` | Per user, per window. All field staff share the single admin account, so these limits are fleet-wide. |
| `ADD_INSTALLER_RATE_WINDOW_SECONDS` | `600` | The rate-limit window. It slides: each request counts until it is this old. |
| `ADD_INSTALLER_PROVISIONER_TIMEOUT_SECONDS` | `90` | The timeout of the package build call. |

The installer also uses these existing settings:

- `ADD_PROVISIONING_WORKER_URL` and `ADD_PROVISIONING_INTERNAL_TOKEN`: the provisioner and its
  bearer token.
- `ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64`: the vault key-1 public key that verifies bundles.

**Switching it off or on.** Add `ADD_INSTALLER_ENABLED='false'` to the protected environment file
`C:\ProgramData\StateLife\AttendanceDeviceDashboard\config\add.env` on the ADD host. Then run the
Deploy ADD workflow (`workflow_dispatch`, `recover_core` off). It recreates add-api with the new
environment. A restart (`docker restart`, `docker compose restart`, Docker Desktop, or
`recover_core`) keeps the old environment, so the installer stays on. Deploys keep keys that are
already in that file. To switch the installer back on, remove the line (or set it to `'true'`)
and run Deploy ADD again. Then check `GET /api/v1/installer/capabilities`: it must show
`enabled: false` (or `true`).

## Bundle store

- **Host directory:** `ADD_INSTALLER_BUNDLE_STORE_HOST_PATH`, an `add-production` GitHub
  variable. The default is
  `C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles`.
- **Mount:** the directory is mounted read-only into add-api only, at `/installer-bundles`.
- **Creation:** `deploy.ps1` creates the directory when it is missing. An empty store is valid.
- **Layout:** `<store>/<bundle_id>/manifest.json`, `manifest.sig` and the image files (design §6).
- **Publishing:** `firmware-key-bootstrap.yml` (`PUBLISH INSTALLER BUNDLE`) publishes bundles
  (`docs/esp-installer-bundles.md`). It never overwrites one.

A bundle is offered only when all of these hold:

- The directory name matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` and equals `bundle_id`.
- The directory is a real directory inside the store, not a symlink that leaves it.
- `manifest.sig` is a base64 RSA-PSS SHA-256 signature (salt 32) over
  `json.dumps(manifest, sort_keys=True, separators=(",", ":"))`. It must verify with
  `ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64`.
- The manifest has:
  - `schema` `zone-lite-installer-bundle-v1` and `partition_layout` `zone-lite-ota-v1`;
  - a `firmware_family` of `zkt` or `hikvision`;
  - a semantic `version`;
  - a `release_state` of `AVAILABLE`, `FIELD_BASELINE` or `HIL_ONLY`;
  - a full `git_sha`;
  - a `created_at` with a time zone;
  - a factory image whose `app_project` (if given) matches the family.
- Every image is a regular file directly in the bundle directory. Its size and SHA-256 match the
  manifest, and it fits the 16 MB flash.
- No `factory`, `ota_0` or `ota_1` image has the exact SHA-256 of a release ADD has revoked
  (`state` `REVOKED` or `revoked_at` set). This is checked on every request, so a revocation
  withdraws its bundles at once: the catalogue and `bundle_count` leave them out, their manifest
  and files answer 404 `BUNDLE_NOT_FOUND`, and a package for one gets 409 `BUNDLE_UNAVAILABLE`.

A bad bundle is logged once (`Installer bundle <id> skipped: <reason>`) and left out. An entry
whose directory cannot be read, such as a symlink loop, is logged once as `its directory cannot
be read` and left out too. Neither ever breaks the other bundles or causes a 500. ADD verifies
each bundle in full when it first sees it, and again when any file in its directory changes. To
withdraw a bundle, move its directory out of the store.

## App store

The Prepare page ("Prepare an ESP32" on the Firmware page) shows a **State Life Device
Installer** card. It reads `GET /apps` and offers the newest app for the visitor's computer:
Windows or Mac, with a smaller link for the other one. It shows the version, the size and the
SHA-256, how to open the app the first time, and an empty state when nothing is published.
Both routes use the dashboard's admin session and the installer kill switch, like every other
installer route.

- **Host directory:** `ADD_INSTALLER_APP_STORE_HOST_PATH`, an `add-production` GitHub
  variable. The default is
  `C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-apps`.
- **Mount:** read-only into add-api only, at `/installer-apps`.
- **Creation:** `deploy.ps1` creates the directory when it is missing. An empty or missing store
  is valid: `latest` is then `null`.
- **Layout:** `<store>/<version>/manifest.json` and the files it lists. A published version
  directory is never rewritten with different bytes.

`manifest.json` is UTF-8 without a byte order mark:

```json
{"schema": 1, "product": "state-life-device-installer", "version": "1.1.0",
 "git_sha": "<40 hex>", "published_at": "2026-11-04T09:30:00Z", "files": [
  {"platform": "windows", "arch": "x64", "kind": "setup",
   "filename": "StateLife-Device-Installer-Setup-1.1.0.exe", "size": 86123456, "sha256": "<64 hex>"},
  {"platform": "macos", "arch": "arm64", "kind": "dmg",
   "filename": "StateLife-Device-Installer-1.1.0-macOS-arm64.dmg", "size": 99614720, "sha256": "<64 hex>"}]}
```

A version is offered only when all of these hold:

- The directory name is three numbers with dots between them, such as `1.1.0` (at most 32
  characters), and equals `version`. Other names are ignored.
- The directory, `manifest.json` and every listed file are real: a symlink is refused.
- The manifest is at most 64 KiB of JSON with no repeated key, `schema` is the number 1, the
  `product` is `state-life-device-installer`, `git_sha` is a full lower-case commit and
  `published_at` is a UTC time ending in `Z`. Other keys are ignored.
- `files` lists one or two files, at most one per platform: `windows` is `x64` `setup` ending in
  `.exe`, and `macos` is `arm64` `dmg` ending in `.dmg`.
- Each file name matches `[A-Za-z0-9._-]+`, does not start with a dot, has at most 100
  characters and is not a TEST build (`-TEST.` or `-TEST-` in its name).
- Each file is a regular file directly in the directory, with the listed `size` (at most 2 GiB)
  and the listed lower-case `sha256`.

The highest valid version is `latest`; versions compare as numbers, so 1.10.0 is above 1.9.0. A
version that fails is logged once (`Installer app <version> skipped: <reason>`) and left out,
so an older valid version is offered instead. ADD hashes each file once and again only when
its size or modification time changes; an older version is hashed only when every newer one
fails. `GET /apps/{version}/{filename}` serves any valid version, not only the newest. It
checks the version the same way first, and answers 404 `FILE_NOT_FOUND` for anything else. The
download is `application/vnd.microsoft.portable-executable` (the Setup) or
`application/x-apple-diskimage` (the disk image), with `Content-Disposition: attachment`.

add-web's nginx passes app downloads through unbuffered: buffering 100 MB for a slow site
link would fill its 32 MB temporary space. To withdraw a version, move its directory out of
the store.

## Audit

- **`INSTALLER_PACKAGE_ISSUED`:** one row per package. Actor: the admin username. Target: `esp`
  and the MAC. `request_id`: the app's request ID. `after` holds only `mode`, `bundle_id` (`null`
  for `change_settings`, which writes no firmware), `zone_id`, `device_id`, `firmware_family` and
  `app_version`. It is written and committed before the response. Refused or failed requests
  write no row. ADD reads these rows to decide which boards may get new settings ("Boards with a
  locked key").
- **`INSTALLER_<KIND>`:** one row per `POST /events`. `kind` matches `^[A-Za-z][A-Za-z0-9_]{1,47}$`
  and is stored in upper case. `PACKAGE_ISSUED` is reserved. `detail` is at most 8 KiB. Keys that
  look secret are stored as `[REDACTED]`, at any depth. That is any key that contains `secret`,
  `token`, `cookie`, `csrf`, `pass`, `pwd`, `psk`, `key`, `hmac`, `nvs`, `ords`, `private`,
  `plaintext` or `ciphertext`, in any case, plus ADD's usual secret names. It covers the
  firmware's own `wifi_pass`, `hik_pass`, `tel_pass` and `zkt_key`. A harmless key with such a part
  (for example `key_burned`) is redacted too, so report such facts under another name. ADD keeps
  only the first 80 characters of a key, but it judges the whole key. The outcome is `REPORTED`.
- **`INSTALLER_APP_DOWNLOADED`:** one row per app download, written and committed before the
  file is sent. Actor: the admin username. Target: `installer_app` and the version. `after`
  holds the `version`, `platform`, `arch`, `filename`, `size` and `sha256`. The outcome is
  `SUCCESS`. A refused download writes no row.

## Provisioner

`POST /internal/v2/nvs-packages` runs on the internal network with the same bearer token. Its body
is `{request_id, target_mac, recipient_public_key, configuration}`, with the configuration as
above.

1. It reuses `validate_request`, plus the `nvs_rows` field checks.
2. It runs the same builder subprocess (`_run_builder`) in a temporary directory.
3. It returns 201 `{provisioning_manifest, provisioning_ciphertext_b64, hmac_ciphertext_b64}`.

Both keys always come from the fleet root (`ADD_FLEET_ROOT_SECRET`); the builder gets no other
root. That is why ADD refuses `resume` and `change_settings` packages for boards that may hold
another root's key ("Boards with a locked key").

It reads no factory bundle and writes no artifact file. Failures are 422
`{"code": "INVALID_FIELD", "field"}` (never with values) or 500 `{"code": "BUILD_FAILED"}`.
`/internal/v1/packages` is unchanged.

### NVS layout

On 2 Nov 2026, new boards with ZKT 2.7.8 lost `zone_cfg` on their second boot. The firmware
then logs "No encrypted per-device provisioning namespace" and stays inert. In the old image,
`zone_cfg` sat on page 0, the ACTIVE page, which the first run then wrote to. Fleet boards that
keep `zone_cfg` on old FULL pages are not affected. Every builder now lays the image out so
that normal operation never writes a page that holds provisioning data. This covers the
provisioner, the device-provisioning workflow and `provision_zone_lite.py`, which all call
`build_nvs_partition`.

- **Page 0 onward (FULL):** the namespace entry, then every row the firmware never writes: the
  zone, ORDS, bootstrap secret, telnet and Hikvision terminal rows, and the CA. `_padN` keys
  fill each of these pages to exactly 126 entries; the firmware never reads them. ESP-IDF's
  generator then marks these pages FULL. A FULL page with no erased entry is never the active
  page and never a garbage-collection victim, so no runtime write programs it. That holds while
  every read verifies: on any page, ESP-IDF erases an entry whose CRC fails.
- **The end of the last FULL page with room:** `wifi_ssid`, `wifi_pass`, `zkt_key` and
  `hik_host`. Only an operator's action rewrites them: Wi-Fi saved by the setup portal or the
  USB rescue, an authenticated COMM-key change and, in Hikvision builds from main, a
  re-discovered terminal host. A board needs them to come back online, so they stay off the
  page that takes the runtime writes. A rewrite erases the old entry here, which programs this
  page's last entry-state word. That word holds only these rows and one-entry padding: never
  the namespace entry, and no part of a longer item. The new value then lives with the runtime
  writes. After a rewrite the page holds a few erased entries. ESP-IDF reclaims the page with
  the most erased entries, which in a 400-boot replay was never this one.
- **The next page (ACTIVE):** `zkt_serial`, `add_ws`, `conn_id` and `dev_token`. A new board
  writes them on its first online run, at onboarding and when it pins the ZKT serial. Every
  image holds them empty, and ADD issues the connector identity again when a board onboards.
  Every runtime write goes to this page or a later one.
- **At least two pages stay erased.** ZKT uses one FULL page, and four pages stay erased.
  Hikvision needs a second FULL page once its CA leaves page 0 no room for the end rows:
  above 1823 bytes with the test values, and above 1311 bytes with every Hikvision field at its
  maximum length. The 3967-byte maximum always needs it. Values and their limits are unchanged.
  The padding takes 68 to 81 entries for ZKT and 4 to 132 for Hikvision, of the 630 entries
  that writes can use.

String values reach the image as the generator stores them. A JSON null becomes an empty
string, CR and CRLF become LF, and a continuation line that starts with `#` is dropped, such as
a `# Subject:` line in a CA bundle. The builder writes the values that way, so such a value no
longer fails the build.

Before generating, the builder checks that the plan holds exactly the rows it was given. After
generating, it checks the image. The page headers and entry-state bitmaps are plaintext. The
builder decrypts the entries with the image's XTS key, in memory only. The check requires every
provisioning page to be FULL with 126 entries, the end rows in an entry-state word of their own,
the first-run rows on one later ACTIVE page, two erased pages, and every value stored as
planned. Otherwise the build fails (`BUILD_FAILED`). `tests/firmware/tools/check_provisioning_nvs.py`
runs in espressif/idf:v5.5.3 in CI. It pins the generator's page-fit rules: a string ends by
entry 124, and a number may take entry 125.

## Onboarding change

`/device/v2/onboard` now answers **409** instead of 500 when the MAC is registered for the other
firmware family. The detail starts with `FIRMWARE_FAMILY_MISMATCH:`. The transaction is still
rolled back: no nonce, site or connector change is kept.

## Limits

- **Device IDs:** `DEVICE_ID_IN_USE` sees only boards that have onboarded. Two boards prepared
  back to back for the same zone and device ID both pass, because nothing is reserved at package
  time. This is on purpose, so that a faulty board can be replaced.
- **Locked keys:** ADD cannot read a board's key. It trusts a board it does not know, or has never
  seen online, to take settings from the fleet root. If such a board in fact holds an older key,
  it will not start after the change, and head office must set it up again with its own root.
  ADD keeps no record of that setup, which is why a `resume` row never allows a later
  `change_settings`.
- **Rate limits:** they live in memory and reset when add-api restarts.
- **Store files:** the store's files are not ACL-restricted, like the other stores under Public
  Documents. Changing a file only makes its bundle unavailable, because every file must match the
  signed manifest.
- **App store:** the app manifest is not signed. Anyone who can write to the app store's host
  directory can change a file and its listed SHA-256 together, and ADD would then offer that
  file. The SHA-256 only proves a copy matches what the store holds.
