# Installer signing and auto-update

This document explains how the desktop installers are built, what a
maintainer needs to do to **sign** them (so operators stop seeing
Gatekeeper / SmartScreen warnings), and how the in-app
release-info endpoint works.

## Current status

The installers produced by the `desktop-package.yml` GitHub Actions
workflow (Sprint 17) ship **unsigned**:

- **macOS**: `briefcase package macOS app --adhoc-sign` produces a
  `.pkg`. Ad-hoc signatures let the bundle launch on the building
  machine but Gatekeeper prompts for confirmation elsewhere.
- **Windows**: `briefcase package windows MSI` produces an unsigned
  `.msi`. SmartScreen will warn the first time an operator runs it.
- **Linux**: `briefcase package linux -p AppImage` — Linux has no
  platform-level signing expectation; users verify via checksum.

Operators can still download and run the artefacts; the signing work
below simply turns "suspicious binary" into "known publisher".

## macOS signing + notarization

1. **Obtain a Developer ID certificate** from Apple ($99/y). Export
   it as a `.p12` + password.
2. **Create an app-specific password** in appleid.apple.com for
   notarization uploads.
3. **Add the following secrets to the GitHub repository**:
   - `APPLE_CERTIFICATE_P12` — base64 of the `.p12`.
   - `APPLE_CERTIFICATE_PASSWORD`
   - `APPLE_DEVELOPER_ID` — the team identifier (`XXXXXXXXXX`).
   - `APPLE_ID` / `APPLE_NOTARY_PASSWORD` — app-specific password.
4. **Update `desktop-package.yml`** — replace the macOS step's
   `--adhoc-sign` with:
   ```yaml
   - name: Import signing certificate
     if: matrix.name == 'macOS'
     run: |
       echo "${{ secrets.APPLE_CERTIFICATE_P12 }}" | base64 -d > cert.p12
       security create-keychain -p actions build.keychain
       security import cert.p12 -k build.keychain \
         -P "${{ secrets.APPLE_CERTIFICATE_PASSWORD }}" \
         -T /usr/bin/codesign
       security list-keychains -s build.keychain
       security default-keychain -s build.keychain
       security unlock-keychain -p actions build.keychain
   - name: Briefcase package (signed + notarized)
     if: matrix.name == 'macOS'
     env:
       IDENTITY: "Developer ID Installer: EGG-API Contributors (${{ secrets.APPLE_DEVELOPER_ID }})"
     run: briefcase package macOS app --identity "$IDENTITY" --notarize
   ```
5. **Test**: download the signed `.pkg` on a clean Mac. Gatekeeper
   should either open silently or show the standard "downloaded from
   the internet" confirmation, not the red "unidentified developer"
   shield.

## Windows Authenticode

1. **Obtain an EV code-signing certificate** (Sectigo, DigiCert…).
   EV certs land immediately on SmartScreen's reputation list;
   standard OV certs need a few thousand installs first.
2. **Store the PFX + password in GitHub secrets**:
   - `WINDOWS_CERTIFICATE_PFX` (base64)
   - `WINDOWS_CERTIFICATE_PASSWORD`
3. **Update the Windows step in `desktop-package.yml`**:
   ```yaml
   - name: Sign MSI
     if: matrix.name == 'Windows'
     shell: pwsh
     run: |
       $pfx = [Convert]::FromBase64String("${{ secrets.WINDOWS_CERTIFICATE_PFX }}")
       [IO.File]::WriteAllBytes("cert.pfx", $pfx)
       & "C:\\Program Files (x86)\\Windows Kits\\10\\bin\\x64\\signtool.exe" `
         sign /f cert.pfx /p "${{ secrets.WINDOWS_CERTIFICATE_PASSWORD }}" `
         /fd sha256 /tr http://timestamp.digicert.com /td sha256 `
         dist\\*.msi
   ```
4. Prior to running this step, remove the `--adhoc-sign` flag from
   `briefcase package windows MSI` — it's a no-op on Windows but
   signals the wrong intent.

## Linux AppImage

- No signing step is required for the OS to launch the bundle.
- We publish a `SHA256SUMS` file alongside each release so operators
  can verify the download. The release workflow produces it with:
  ```bash
  cd dist && sha256sum *.AppImage *.pkg *.msi > SHA256SUMS
  ```

## Auto-update flow

EGG-API does **not** ship an in-process auto-updater. The desktop
launcher exposes a small GET endpoint that tells the admin dashboard
whether a newer version exists; downloading it and running the
platform-native installer is the operator's choice.

- `GET /admin/v1/releases` — admin-gated; returns:
  ```json
  {
    "current_version": "2.0.0",
    "platform": "linux",
    "python": "3.12.3",
    "latest_version": "2.0.1",
    "update_available": true,
    "html_url": "https://github.com/maribakulj/egg-api/releases/tag/v2.0.1",
    "assets": [ ... ],
    "repository": "maribakulj/egg-api"
  }
  ```
- The upstream check is cached for 10 minutes in-process.
- `EGG_DISABLE_RELEASE_CHECK=1` turns the check off for air-gapped
  deployments; the endpoint still reports `current_version`.

Briefcase also ships its own platform-native auto-update machinery
(macOS Sparkle integration, Windows MSI upgrade tables) that can be
wired on later if / when we invest in signing.
