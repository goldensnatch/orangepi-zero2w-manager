# Rocky Good Patron SMB Seeding Setup

Goal: keep Rocky/qBittorrent behind Gluetun, but store completed downloads on the Windows mini-PC SMB share so torrents can seed for long periods without filling Rocky's local storage.

## Reconciled decision

Use the other session's simpler seed-folder shape, but keep Hayden's explicit current backing folder:

```text
Windows backing folder now: H:\Rocky-Export
Recommended SMB share name: Rocky-Export
Rocky stable mount point:   /mnt/rocky-seed
Future larger-drive rule:   keep /mnt/rocky-seed stable; swap only the Windows/NAS backing path
```

If Hayden later prefers the cleaner share name `Rocky-Seed`, create that share against the larger drive and leave Rocky mounted at `/mnt/rocky-seed`.

## Storage layout

Windows raw seed folder:

```text
H:\Rocky-Export
  complete\      # original torrent payloads; do not destructively modify while seeding
  incomplete\
  watch\
  library\
    movies\      # optional media library/import target
    tv\
```

Windows prep/output folder, separate from seeding data:

```text
H:\Switch-Ready  # extracted/copy-ready files for delivery workflows
```

Rocky sees the SMB share as:

```text
/mnt/rocky-seed
  complete/
  incomplete/
  watch/
  library/
    movies/
    tv/
```

Container path policy:

```text
qBittorrent incomplete: /downloads/incomplete -> /mnt/rocky-seed/incomplete
qBittorrent complete:   /downloads/complete   -> /mnt/rocky-seed/complete
qBittorrent watch:      /downloads/watch      -> /mnt/rocky-seed/watch
Radarr downloads:       /downloads/complete   -> /mnt/rocky-seed/complete
Sonarr downloads:       /downloads/complete   -> /mnt/rocky-seed/complete
Radarr library:         /movies               -> /mnt/rocky-seed/library/movies
Sonarr library:         /tv                   -> /mnt/rocky-seed/library/tv
Jellyfin libraries:     /data/movies, /data/tv -> /mnt/rocky-seed/library/...
NS USBLoader root:      /mnt/rocky-seed/complete
```

## Key rule: seed payloads are immutable-ish

To keep seeding, qBittorrent must keep the original torrent payload at the same path it downloaded to. Do not move, rename, delete, or extract destructively inside:

```text
H:\Rocky-Export\complete
```

Do Windows prep work into a separate folder instead:

```text
H:\Rocky-Export\complete\SomeTorrentFolder  # original seed payload
H:\Switch-Ready\SomePreparedFolder          # extracted/copy-ready output
```

## Windows setup

Run on the Windows mini-PC in PowerShell as Hayden/admin:

```powershell
New-Item -ItemType Directory -Force "H:\Rocky-Export\complete"
New-Item -ItemType Directory -Force "H:\Rocky-Export\incomplete"
New-Item -ItemType Directory -Force "H:\Rocky-Export\watch"
New-Item -ItemType Directory -Force "H:\Rocky-Export\library\movies"
New-Item -ItemType Directory -Force "H:\Rocky-Export\library\tv"
New-Item -ItemType Directory -Force "H:\Switch-Ready"
New-SmbShare -Name "Rocky-Export" -Path "H:\Rocky-Export" -ChangeAccess "Hayden"
```

If the share already exists, only create the subfolders.

## Rocky mount setup

Run on Rocky/OrangePi Linux:

```bash
sudo apt-get update
sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/rocky-seed
sudo mkdir -p /etc/samba/credentials
sudo nano /etc/samba/credentials/rocky-export
```

Credential file content:

```text
username=Hayden
password=YOUR_WINDOWS_PASSWORD_OR_SMB_USER_PASSWORD
domain=WORKGROUP
```

Then lock it down:

```bash
sudo chmod 600 /etc/samba/credentials/rocky-export
```

Add this line to `/etc/fstab`:

```text
//192.168.1.141/Rocky-Export /mnt/rocky-seed cifs credentials=/etc/samba/credentials/rocky-export,uid=1000,gid=1000,iocharset=utf8,vers=3.0,nofail,x-systemd.automount,x-systemd.requires=network-online.target,x-systemd.after=network-online.target,file_mode=0664,dir_mode=0775 0 0
```

Mount and create folders:

```bash
sudo systemctl daemon-reload
sudo mount /mnt/rocky-seed
mkdir -p /mnt/rocky-seed/complete /mnt/rocky-seed/incomplete /mnt/rocky-seed/watch /mnt/rocky-seed/library/movies /mnt/rocky-seed/library/tv
touch /mnt/rocky-seed/.rocky-write-test && rm /mnt/rocky-seed/.rocky-write-test
findmnt /mnt/rocky-seed
```

## Docker compose volume changes

qBittorrent transfer-stack should use:

```yaml
volumes:
  - ./qbittorrent-config:/config
  - /mnt/rocky-seed/complete:/downloads/complete
  - /mnt/rocky-seed/incomplete:/downloads/incomplete
  - /mnt/rocky-seed/watch:/downloads/watch
```

Media-stack compose in this repo has been aligned to the same `/mnt/rocky-seed` layout.

## qBittorrent settings

In qBittorrent WebUI (`http://192.168.1.199:8088`):

- Default Save Path: `/downloads/complete`
- Keep incomplete torrents in: `/downloads/incomplete`
- Watched folder: `/downloads/watch`
- Category for long seeding: `longseed`
- Seeding time target for 10 days: `14400` minutes
- Prefer pause after limit rather than delete, so cleanup stays deliberate.

## Service-order guard

Before starting/recreating qBittorrent, verify the SMB mount is live:

```bash
findmnt /mnt/rocky-seed && test -w /mnt/rocky-seed/complete && echo OK
```

If the mount is unavailable, do not start qBittorrent. Starting qBittorrent against an empty local fallback directory is the main failure mode this setup must avoid.

## Switch Transfer Hub direction

Switch Transfer should become a hub over these concepts:

1. Download / Seed Storage: Rocky SD vs Mini-PC SMB vs future USB/NAS, mount status, free space.
2. qBittorrent Seeding: active torrents, seeding torrents, seed time, safe-to-cleanup status.
3. Windows Prep: `H:\Rocky-Export` as immutable seed source and `H:\Switch-Ready` as prepared output.
4. Delivery Options: NS USBLoader, USB/UMS, MTP only if real MTP is present, FTP/LAN.

## Later migration to a larger drive

When the larger drive arrives, keep the Linux mount point `/mnt/rocky-seed` the same if possible. Move the SMB backing storage from `H:\Rocky-Export` to the new disk/share, then Rocky/qBittorrent/Radarr/Sonarr/Jellyfin do not need path changes.
