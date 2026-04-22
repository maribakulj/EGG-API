# First launch on Windows and macOS

EGG-API desktop installers (`.msi`, `.pkg`, AppImage) ship **unsigned
by choice** to keep the project free. At the first launch, Windows
SmartScreen and macOS Gatekeeper will warn you that the publisher is
not recognised. This is expected — the app is safe to use, the
source code is public, and you need to dismiss the warning exactly
once per installation.

## Windows 10 and 11

At first double-click on `EGG-API-x.y.z.msi`:

1. A blue window appears: *"Windows protected your PC. Microsoft
   Defender SmartScreen prevented an unrecognised app from starting"*.
2. Click the small blue link **"More info"** (not *Don't run*).
3. A new button appears: **"Run anyway"**. Click it.
4. The installer proceeds normally.

Once installed, opening the shortcut from the Start menu does **not**
trigger SmartScreen again — the warning applies only to the installer
download.

### WebView2 runtime

The EGG-API desktop app uses Microsoft Edge's WebView2 to render the
admin UI inside a native window.

- **Windows 11** ships WebView2 by default — nothing to do.
- **Windows 10 build ≥ 1803** ships it on most fresh installs.
- **Older Windows 10** may need a one-off install: download
  [WebView2 Runtime — Evergreen](https://go.microsoft.com/fwlink/p/?LinkId=2124703)
  from Microsoft (free, ~2 MB bootstrapper, will fetch the runtime
  silently).

If EGG-API cannot start its window, it shows a native message box
with the URL to open in any browser — so even without WebView2 the
service is usable.

### Windows Defender Firewall

At first launch, Windows may ask *"Do you want to allow this app to
communicate on the network?"*. EGG-API only binds to **localhost**,
so you can safely click **Cancel** (the service still works). If you
plan to expose the service on your LAN, click **Allow**.

## macOS 11+

At first open of `EGG-API-x.y.z.pkg`:

1. A dialog says *"EGG-API can't be opened because Apple cannot check
   it for malicious software"*. Click **OK** to dismiss.
2. Open **System Settings → Privacy & Security**.
3. Scroll down to the *Security* section — a message says *"EGG-API
   was blocked to protect your Mac"* with an **"Open Anyway"** button.
4. Click **Open Anyway**, confirm with your password.
5. The installer proceeds normally.

Alternative one-liner for power users:

```bash
xattr -d com.apple.quarantine /Applications/EGG-API.app
```

## Linux (AppImage)

AppImages don't trigger an OS-level warning but must be made
executable:

```bash
chmod +x EGG-API-x.y.z.AppImage
./EGG-API-x.y.z.AppImage
```

The launcher depends on **WebKitGTK 4.1** for its native window.
Most modern distros have it pre-installed; if not:

```bash
# Debian / Ubuntu
sudo apt install libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1

# Fedora
sudo dnf install webkit2gtk4.1
```

## What if the window does not appear at all?

On any OS, the launcher writes its log + the magic URL to:

- **Windows**: `%APPDATA%\EGG-API\logs\launcher.log`
- **macOS**: `~/Library/Application Support/EGG-API/logs/launcher.log`
- **Linux**: `$XDG_DATA_HOME/egg-api/logs/launcher.log` (or
  `~/.local/share/egg-api/logs/launcher.log`).

Open that file, copy the `http://127.0.0.1:<port>/admin/setup-otp/...`
URL, and paste it in your browser. The server is running — only the
embedded window is missing.

If nothing is in the log either, the launcher will try to show a
native message box explaining where the log is. Take a screenshot
and file an issue on GitHub with the contents of `launcher.log`.

## Why not sign the installers?

Code-signing on macOS requires an Apple Developer Program membership
($99/year). On Windows it requires an Authenticode certificate
(~$400-700/year for an EV cert). For a free, open-source tool aimed
at small institutions, we chose to invest that budget elsewhere.

Signing removes the warnings above but adds nothing to the safety of
the software itself — the code is public, audited, and the binaries
are built by GitHub Actions from a known commit, with SHA256SUMS
published alongside each release. If you work at an institution that
is willing to sponsor a signing certificate, please
[open an issue](https://github.com/maribakulj/egg-api/issues) — we
will add the signing step to the release workflow.
