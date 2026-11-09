# Reviewer Guide — ShareBox

This page is a compact index for the SignPath Foundation review. It links to the
parts of the project that matter for eligibility, build reproducibility, and
signing scope.

## Project

| Item | Value |
| --- | --- |
| Name | ShareBox |
| Repository | <https://github.com/Jiapenghui94/sharebox> |
| License | Apache-2.0 (`LICENSE`) |
| Language | Python 3.12 (application), C# (installer), Go (optional tsnet sidecar) |
| Status | First public release `v2.1.7`; used in a small internal pilot |
| Purpose | Local-first, peer-to-peer folder sharing for small teams |

ShareBox lets each device share a local folder. Devices pair directly and
exchange files over HTTP; there is no central server. Company codes isolate
tenants, permissions are per-folder (read-only or full control), and file
changes are written to an audit log. All account, index, and log data stays on
the user's device.

## Build and CI

| Item | Value |
| --- | --- |
| Workflow | `.github/workflows/windows-build.yml` |
| Build script | `scripts/build-windows.ps1` |
| Runner | GitHub Actions `windows-latest` |
| Python | 3.12, installed by `actions/setup-python` |
| Steps | unit tests → PyInstaller onedir → payload zip → C# installer (`csc.exe`) |
| Secrets required to build | **None** |
| Artifact | `ShareBox-Setup.exe` |

The build only uses public packages (PyInstaller from PyPI) and the .NET
Framework compiler that ships with the Windows runner. It does not download or
embed any proprietary component.

Workflow page:

<https://github.com/Jiapenghui94/sharebox/actions/workflows/windows-build.yml>

## Tests

```powershell
python -m unittest discover -s tests -t .
```

The suite covers API behavior, path handling, password/company crypto, pairing
and company isolation, sync, and storage. The same command runs in CI before
the build, so a green `windows-build` run means the tests passed.

<https://github.com/Jiapenghui94/sharebox/tree/main/tests>

## Release

`v2.1.7` is published as a pre-release with an **unsigned** installer:

<https://github.com/Jiapenghui94/sharebox/releases/tag/v2.1.7>

Its release notes state that official signed builds will be signed by SignPath
Foundation. After approval, the first signed release will be a new version
(e.g. `v2.1.8`), built by the same workflow.

## Signing scope

Two Windows PE files are in scope:

1. `ShareBox-Setup.exe` — the installer users download;
2. `ShareBox.exe` — the application launcher inside the installer payload.

Both should be signed with SHA-256. Signing happens in CI after a successful
build; no local or modified binaries are submitted for signing.

## Security and privacy

- `SECURITY.md` — key handling, supported versions, reporting.
- `PRIVACY.md` — no telemetry, no analytics, no data collection; all data is local.
- Auth keys and device tokens are supplied at runtime and never committed.

## Optional tsnet sidecar

`tsnet/` contains an optional Go sidecar that joins a Tailscale tailnet with
`tsnet` and exposes a local SOCKS5 proxy. It is **built from source in CI**
(`.github/workflows/build-tsnet.yml`); no prebuilt binary is committed, and no
auth key is compiled into it. It is not part of the `v2.1.7` artifact yet.

- Source: `tsnet/`
- Design: `docs/TSNET.md`
- CI artifact: `sharebox-tsnet` from `build-tsnet`

## No proprietary components

- Application: Python standard library only at runtime; PyInstaller bootloader.
- Installer: C# compiled from `installer/Installer.cs` on the runner.
- Sidecar: Go module pinned in `tsnet/go.mod`, built in CI.
- No bundled Tailscale client, no closed-source SDKs, no license-gated build steps.

## Contact

- Issues: <https://github.com/Jiapenghui94/sharebox/issues>
- Maintainer: see the application form / GitHub profile `Jiapenghui94`.
