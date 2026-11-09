# Privacy Policy

ShareBox is a local-first, peer-to-peer file sharing application.

## Data we collect

**None.** ShareBox does not collect, transmit, or sell personal data.
There is no telemetry, analytics, advertising, or crash reporting.

## Where data is stored

All data stays on the user's own devices:

- account credentials (PBKDF2 hashes), company settings, device pairings,
  file indexes, audit logs, and sync state are stored in the local
  `%LOCALAPPDATA%\ShareBox` directory;
- shared files remain in the folders the user explicitly selects;
- nothing is uploaded to a server operated by the project.

## Network connections

ShareBox only connects to devices the user has explicitly paired with.
If the user chooses to install Tailscale for remote access, that connection
is governed by Tailscale's own privacy policy. Tailscale is optional.

## Third parties

The application does not integrate advertising, analytics, or tracking SDKs.
GitHub is used only for source code hosting and release downloads.

## Contact

For privacy questions, open an issue at
<https://github.com/Jiapenghui94/sharebox/issues>.
