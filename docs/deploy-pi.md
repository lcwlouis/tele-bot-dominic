# Deploy Dom on a Raspberry Pi

Use a 64-bit Raspberry Pi OS installation. Run Dom under your regular account.
Keep its instance home separate from the code so releases can be switched without
moving the database, notes or secrets.

```text
~/dom/
  app -> releases/<sha>
  releases/<sha>/
  instance/
  backups/
```

Run Python from `~/dom/app`. When you run `python -m dom` from your user home,
the `~/dom` folder can shadow the installed `dom` package. Use a normal `cd` into
the `app` symlink so the installer records the stable app path.

## Stage a release

Clone the repository into a separate checkout, then run these commands from it.
Choose a reviewed commit. On a fresh install, `HEAD` is the checked-out commit.
For an update, advance the checkout first with `git pull --ff-only` and choose the
commit to deploy.

```sh
root=~/dom
sha=$(git rev-parse HEAD)
release="$root/releases/$sha"
mkdir -p "$root/releases"
mkdir "$release"
git archive "$sha" | tar -x -C "$release"
(cd "$release" && uv sync --frozen --no-dev)
```

Use a new release directory each time. If staging fails, leave `app` pointing
at the current release until the new one is ready.

## Install on first run

```sh
ln -s "releases/$sha" "$root/app"
cd "$root/app"
.venv/bin/python -m dom init "$root/instance"
```

Follow the [setup guide](setup.md) to populate `.env` and choose your models.
Then preview the units and install them.

```sh
.venv/bin/python -m dom install-service "$root/instance"
.venv/bin/python -m dom install-service "$root/instance" --yes
```

The installer writes three user units to `~/.config/systemd/user/`, reloads
systemd and enables and starts the bot and backup timer. It refuses to replace
customized unit files without `--force`. Run the printed `loginctl enable-linger`
command if linger is off, using an account authorized to enable it. Linger lets
the user service start at boot and continue after logout.

The bot service uses `MemoryHigh=500M`, `MemoryMax=700M`, `Restart=on-failure`,
`RestartSec=15` and `Nice=5`. The persistent timer runs the backup at 04:00 in the
Pi's local time, with idle I/O scheduling. The backup command keeps the latest
seven archives in `~/dom/backups`. Archives include `.env` and private data.

## Switch releases and roll back

After staging the new release, record the current symlink target, stop the bot
and take a backup using the old release. Continue after the backup succeeds.

```sh
cd "$root/app"
previous=$(readlink "$root/app")
systemctl --user stop dom.service
.venv/bin/python -m dom backup "$root/instance"
ln -s "releases/$sha" "$root/app.next"
mv -Tf "$root/app.next" "$root/app"
systemctl --user start dom.service
cd "$root/app"
.venv/bin/python -m dom doctor "$root/instance"
```

Keep the old release and the pre-update backup until the new release is verified.
For a rollback, stop Dom and use the same `ln -s` and `mv -Tf` steps with
`"$previous"` as the target. If the newer code migrated the database to an
incompatible version, move the current instance home aside and use the old
release's `dom restore <archive> <home>` to restore the pre-update archive
before starting the old service. Preserve the moved home for recovery of data
written after the update. Keep the bot stopped during the restore.

## Keep logs across reboots

Raspberry Pi OS ships
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf` with
`Storage=volatile`. As an administrator, create
`/etc/systemd/journald.conf.d/99-dom-persistent.conf` so it sorts later.

```ini
[Journal]
Storage=persistent
```

Restart `systemd-journald.service` as an administrator to apply the change.
Query the service and timer logs from the Dom account.

```sh
journalctl --user -u dom.service -f
journalctl --user -u dom-backup.service --since yesterday
journalctl --user -b -1 -u dom.service
systemctl --user list-timers dom-backup.timer
```

## Log power, temperature and free memory

A one-minute sample can help diagnose under-voltage or memory pressure. Verify
your user can run `vcgencmd get_throttled` and `vcgencmd measure_temp`. Save these
units under `~/.config/systemd/user/`.

`pi-health.service`

```ini
[Unit]
Description=Log Raspberry Pi power, temperature and memory

[Service]
Type=oneshot
ExecStart=/usr/bin/vcgencmd get_throttled
ExecStart=/usr/bin/vcgencmd measure_temp
ExecStart=/usr/bin/free -m
```

`pi-health.timer`

```ini
[Unit]
Description=Sample Raspberry Pi health once a minute

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s

[Install]
WantedBy=timers.target
```

Run `systemctl --user daemon-reload` and
`systemctl --user enable --now pi-health.timer`. Read the samples with
`journalctl --user -u pi-health.service`.

## Optional desktop

The desktop image is about 8.4 GB. Give the Pi enough disk space for the download
and unpacked layers. Pull it by itself in a detached terminal such as a
`tmux` session. Wait for it to finish before building a release or starting
other heavy work. See [Desktop network](desktop.md) for the firewall and
isolation checks.
