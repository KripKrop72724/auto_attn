# ESP installer bundles

An installer bundle is one signed, MAC-independent set of flash images per firmware family and version. The State Life Device Installer writes it to every new Zone Lite board of that family; ADD makes only the per-board settings (encrypted NVS and the eFuse key). Bundles are published once, never per board, by the `Zone Lite signing key bootstrap` workflow (`.github/workflows/firmware-key-bootstrap.yml`) with the confirmation `PUBLISH INSTALLER BUNDLE`. ADD serves them read-only from `/installer-bundles`. The format is section 6 of [esp-installer-design.md](esp-installer-design.md).

## What a bundle holds

`<store>/<bundle_id>/` holds `manifest.json` (canonical JSON), `manifest.sig` (base64 RSA-PSS SHA-256, salt length 32, by vault key 1) and these images:

| Role | File | Offset | ZKT | Hikvision |
| --- | --- | --- | --- | --- |
| `bootloader` | `bootloader-signed.bin` | `0x0` | Signed by all three vault keys | Same, built from the exact source |
| `partition-table` | `partition-table.bin` | `0x10000` | The production `zone-lite-ota-v1` table | Same |
| `otadata` | `ota-data.bin` | `0x17000` | Selects `ota_0` (the flash tool's bytes) | Blank, so it boots factory |
| `factory` | `zone-lite-signed.bin` | `0x20000` | The exact signed AVAILABLE release | The exact-source app, signed by key 1 |
| `ota_0` | `zone-lite-signed.bin` | `0x2A0000` | The same release bytes | Not written |
| `ota_1` | `rollback-predecessor.bin` | `0x520000` | The qualified predecessor (default 2.5.2) | Not written |
| `storage` | `storage-empty.bin` | `0x7A0000` | Empty SPIFFS, SHA-256 `f39db7c1…5ec70` | Same |

The app adds two ranges no bundle carries: `nvs` (ADD's per-board settings) and the blank redo area (`redo-area`: 0x60000 × 0xFF at `0xFA0000`, made by the app). A manifest listing either role is refused, and so is a partition table with any partition past `0xFA0000`.

The partition table is at `0x10000` (`CONFIG_PARTITION_TABLE_OFFSET`, where the flash tool writes it), not `0x8000`. Each application entry also carries `app_project`, `app_version` and `app_digest`. `app_digest` is the SHA-256 the image carries after its segments: what the board's `esp_partition_get_sha256()` returns and what ADD release manifests call `application_sha256`. It is not the file hash.

Bundle IDs:

- ZKT: `zkt-<version>-<app digest 8>`, for example `zkt-2.7.8-b8d0ca7e`. The same release always gets the same ID.
- Hikvision: `hikvision-<version>-<source sha 7>-<app digest 8>`. Source `dc7d9c3` embeds its build time, so every Hikvision publication gets a new ID.

| Field | ZKT | Hikvision |
| --- | --- | --- |
| `release_state` | `AVAILABLE` | `FIELD_BASELINE` |
| `recommended` | `true` | `true` |
| `git_sha` | The main commit that built the bootloader and partition table | The exact source commit |

## Before you publish

- Dispatch from `main` only. The run also needs every ADD CI gate to be green for that exact commit: repository-contract, windows-publication, firmware, backend, backend-static, frontend, oracle-projection and containers.
- The signing vault must already exist on the ADD runner (`%ProgramData%\StateLife\AttendanceDeviceDashboard\firmware-signing`). This mode never creates keys. Key 1 must be the only ACTIVE key. The key inside its `key-1-public.pem` (the DER SubjectPublicKeyInfo) must have SHA-256 `0523b6a90880e60c1526533128931c770024659d89f798a756c92fa7edae7bf0`. That is the key every installer pins. The run compares this value, not the PEM file's hash: a copy of the same key with Windows (CRLF) line endings has another file hash. (The vault's own key ID, `9fc614e0…`, is the hash of the LF PEM file it wrote.)
- ZKT only: the production release and the predecessor must both be AVAILABLE in the firmware store (`ADD_FIRMWARE_STORE_HOST_PATH`, already set for `firmware-signing`) and in deployed ADD. ADD records a revocation only in its database and never marks the store, so the run asks ADD which releases it still offers. It reads ADD's database read-only, inside `attendance-device-dashboard-add-api-1` on the runner host. A revoked release is never chosen, and `latest` is the newest release ADD still offers.
- ZKT only: the predecessor must also be one the State Life Device Installer accepts, that is, listed with its exact application digest in `QUALIFIED_PREDECESSORS` (`apps/esp_installer/src/sl_installer/core/bundles.py`). Every installed app refuses any other ota_1 image.
- Optional: set the variable `ADD_INSTALLER_BUNDLE_STORE_HOST_PATH`. When it is unset, bundles go to `C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles`. ADD's deploy uses the same default for the `/installer-bundles` mount. Two environments read the variable: the publisher in `firmware-signing` and ADD's deploy in `add-production`. Set it as a repository variable, or set it to the same path in both, so that ADD reads the directory the bundles are written to.

## Publish a ZKT bundle

In GitHub, open Actions, then **Zone Lite signing key bootstrap**, then **Run workflow** on `main`, and set:

| Input | Value |
| --- | --- |
| `device_mac` | empty |
| `confirmation` | `PUBLISH INSTALLER BUNDLE` |
| `family` | `zkt` |
| `production_release` | `latest` (the newest ZKT release AVAILABLE in the store and in deployed ADD) or an exact AVAILABLE version such as `2.7.8` |
| `rollback_predecessor` | `2.5.2`. For 2.8.0, `2.7.8` also qualifies. Either way it must be one the installer app accepts. |

`wifi_profile` and `hikvision_source_ref` are ignored. `none` is refused for both release inputs, because a bundle must boot a published release beside a qualified predecessor.

The same dispatch from a terminal:

```
gh workflow run firmware-key-bootstrap.yml --ref main -f confirmation='PUBLISH INSTALLER BUNDLE' -f family=zkt -f production_release=latest -f rollback_predecessor=2.5.2
```

## Publish a Hikvision bundle

Use the same steps and set:

| Input | Value |
| --- | --- |
| `device_mac` | empty |
| `confirmation` | `PUBLISH INSTALLER BUNDLE` |
| `family` | `hikvision` |
| `hikvision_source_ref` | `dc7d9c3b0de1aa5e1f1b7f279f16e317bf7afe65`, the default. It is the HIK 3.1.1 source of the Jhelum and BLD1 boards. |

`production_release` and `rollback_predecessor` are ignored.

```
gh workflow run firmware-key-bootstrap.yml --ref main -f confirmation='PUBLISH INSTALLER BUNDLE' -f family=hikvision -f hikvision_source_ref=dc7d9c3b0de1aa5e1f1b7f279f16e317bf7afe65
```

The build follows the Jhelum recipe: `idf.py -D ZONE_LITE_FAMILY=hikvision` with the standard `ZONE_LITE_SETUP_PASSWORD`. Main's `build_empty_storage_image.py` then runs against that build directory, because the old source does not have the tool. The result is the same source and recipe, not the same bytes as the Jhelum image.

## What gets checked

1. **Dispatch inputs** (`dispatch-inputs`).
   - The confirmation is typed exactly, in capitals, before anything else is checked. GitHub's job conditions ignore letter case, so this is the check that refuses `publish installer bundle` or `Publish Installer Bundle`. Any other letter case fails the whole run. Text that differs in more than letter case runs no job at all.
   - The ref is `main`.
   - `device_mac` is empty.
   - `hikvision_source_ref` is a full lowercase 40-hex SHA.
   - ZKT: `production_release` is `latest` or an exact version, and `rollback_predecessor` is an exact version.
   - The two per-device modes still fail without an exact MAC.
2. **Build** (`installer-bundle-build`, ubuntu).
   - The exact-commit CI gate passes.
   - Hikvision: the checkout is exactly `hikvision_source_ref`, and that commit is an ancestor of the main commit that runs, so only merged source is ever signed.
   - The application contains the protected setup password and not `SLIC-CI-ONLY`.
   - The application descriptor names the right family.
   - The empty storage hashes to `f39db7c1…`.
3. **Publish job, before the signer** (`installer-bundle-publish`).
   - The SubjectPublicKeyInfo of the vault's `key-1-public.pem` is the pinned installer key (`build_installer_bundle_manifest.py spki`).
   - ZKT: it chooses only store releases with no HIL or diagnostic marker, of the ZKT family, that deployed ADD still offers: AVAILABLE, not revoked, with the same image SHA-256 and application digest (`build_installer_bundle_manifest.py add-available`).
4. **Signer, before any key is decrypted** (`bootstrap-firmware-signing.ps1 -InstallerBundle`).
   - The vault exists and is complete.
   - Key 1 is the only ACTIVE key. Its public key matches its key ID.
   - The output directory is new.
   - The storage is the bench-verified empty SPIFFS.
   - ZKT, the release: the production release and the predecessor are AVAILABLE, have no HIL or diagnostic marker, have the right family, layout and channel, and their image hash and size match their manifests. The predecessor is older.
   - ZKT, qualification: the production release's signed `queue_storage.allowed_bootstrap_images` lists the predecessor's exact application digest.
   - ZKT, identity: each image's descriptor and digest match its manifest.
   - ZKT, installer app: the app's `QUALIFIED_PREDECESSORS` lists the predecessor with that exact digest (`build_installer_bundle_manifest.py predecessor`).
   - ZKT, deployed ADD: after the identity check of this commit's build and before the vault is touched, ADD must still offer both releases (`add-available --require-all`). The per-device bootstrap runs this check too.
   - Hikvision: no release directories are given.
5. **Signing.** The bootloader is signed with keys 1, 2 and 3. For Hikvision, the application is signed with key 1. ZKT uses the published release bytes unchanged.
6. **Bundle and manifest** (`scripts/build_installer_bundle_manifest.py build`).
   - The fixed roles, names and offsets are used.
   - The partition table equals ESP-IDF's output for `partitions.csv`.
   - The otadata is exact.
   - Every application has a valid appended SHA-256 and an ESP32-S3 header. It has at least one Secure Boot V2 signature block (the bootloader has all three) and it fits its slot.
   - ZKT: the predecessor is older.
   - The bundle ID is derived from the images.
   - The manifest is written in canonical form.
   - The runner signs the manifest with key 1 in a throwaway container. It then checks the signature against `key-1-public.pem`.
7. **Publisher** (`deploy/add/publish-installer-bundle.ps1`).
   - Before anything is written, it repeats every bundle and manifest check of step 6. It also verifies the signature against the vault public key and refuses a key whose SubjectPublicKeyInfo SHA-256 is not the pinned one.
   - It copies the bundle into `<store>\.staging\<id>-<guid>`, images first and the signed manifest last, and verifies that copy. No reader of `<store>\<id>\manifest.json` looks one level down.
   - It renames the copy to `<store>\<bundle_id>` on the same volume. The rename fails rather than merge if that ID already exists.
   - It verifies the published directory once more, then prints the bundle ID.

## Where files land

- **Store:** `<store>\<bundle_id>\` on the ADD host. Nothing else is written there except the short-lived `.staging` directory, which is removed once it is empty.
- **Job summary:** the `installer-bundle-publish` job writes `Published installer bundle <bundle_id>`.
- **Artifacts:** each kept for 1 day, as with the per-device bootstrap.
  - `zone-lite-installer-unsigned-<sha>`: the unsigned build.
  - `zone-lite-installer-bundle-<sha>`: the published bundle.
- **Exposure:** the repository is public, so signed-in GitHub users can download these artifacts during that day. The images contain the compiled setup-AP password, the same exposure as today's bootstrap artifacts.

To check a bundle by hand on any machine with Python and `cryptography`:

```
python scripts/build_installer_bundle_manifest.py verify --bundle <store>\<bundle_id> --public-key key-1-public.pem
python scripts/build_installer_bundle_manifest.py spki --public-key key-1-public.pem
```

The second command must print `0523b6a9…7bf0`, the pinned key, whatever line endings the PEM file has.

## Retire a bundle

Published bundles are never edited or overwritten. To stop offering one, move its directory out of the store on the ADD host, into a folder beside the store rather than inside it. Keep the retired copy as evidence instead of deleting it:

```powershell
$store = 'C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles'
$retired = 'C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles-retired'
New-Item -ItemType Directory -Path $retired -Force | Out-Null
Move-Item -LiteralPath (Join-Path $store 'zkt-2.7.8-b8d0ca7e') -Destination (Join-Path $retired ('zkt-2.7.8-b8d0ca7e-' + (Get-Date -Format yyyyMMdd)))
```

ADD stops listing it the next time it reads the store. A ZKT ID depends only on the release, so publishing the same release again, for example after a bootloader change on main, is refused until the old bundle is retired. The new bundle then reuses the ID with different bytes. Installers must therefore treat the catalogue's `manifest_sha256`, not the ID alone, as the identity of a cached copy.

Revoking a ZKT release in ADD changes only ADD's database. `PUBLISH INSTALLER BUNDLE` can never choose that release again, for production or for ota_1. ADD also stops offering every published bundle whose `factory`, `ota_0` or `ota_1` image has the revoked release's exact SHA-256: the catalogue leaves it out, its manifest and files answer 404 `BUNDLE_NOT_FOUND`, and a package for it is refused with 409 `BUNDLE_UNAVAILABLE`. That takes effect at once, with nothing to move. Retire those bundles as above anyway, so the store shows what is offered.

## Common refusals

| Message | What to do |
| --- | --- |
| `Type the confirmation exactly, in capitals: …` | Run again with the confirmation exactly `PUBLISH INSTALLER BUNDLE`, in capital letters. |
| `An installer bundle cannot create or replace signing keys` | The vault is missing on this runner. This is an incident; never bootstrap a new vault for bundles. |
| `Installer bundles are published only from main` | Dispatch on `main`. |
| `Exact main CI gate is not green: <job>` | Wait for, or fix, CI on that exact commit. |
| `No AVAILABLE ZKT release '<x>' in the firmware store and deployed ADD` | Publish or promote the release first, or choose another version. A release revoked in ADD never comes back: publish a new version. |
| `… add-available refused: Deployed ADD's release catalogue could not be read` | `attendance-device-dashboard-add-api-1` is not running on the runner host, or its database is not answering. Fix ADD, then run again. |
| `A revoked or unpublished release is never signed: Release <x> is not AVAILABLE in deployed ADD…` | ADD revoked that release, never published it, or verified other bytes for it than the store holds. Choose another release. |
| `Production release <x> does not qualify <y> as its ota_1 rollback image` | Pick a predecessor that release accepts (2.5.2, or 2.7.8 for 2.8.0). |
| `Every installed app would refuse this bundle: Rollback predecessor <y> is not one the State Life Device Installer accepts` | Pick a predecessor the app lists, such as 2.5.2. A new one must first ship in an installer release, with ADD's `installer_min_app_version` raised to that release. |
| `Empty storage image is not the bench-verified empty SPIFFS…` | The build's SPIFFS options or spiffsgen changed. The image must be proven on hardware again before the pin changes. |
| `Installer bundle <id> is already published…` | Retire the published bundle first if it really must be replaced. |
| `Installer bundle signing key is not the key every installer pins` | The SubjectPublicKeyInfo of the vault's key 1 is not `0523b6a9…`: the key changed. Stop: no installed app would accept the bundle. |
