# Deploy Dom on a Raspberry Pi

Use a 64-bit Raspberry Pi OS installation. Run Dom under your regular account.
Keep its instance home separate from the code so releases can be switched without
moving the database, notes or secrets.

```text
~/dom/
  app -> releases/<tag>
  releases/<tag>/
  repo.git/        cache of the release tags (created by dom deploy)
  instance/
  backups/
```

Run Python from `~/dom/app`. When you run `python -m dom` from your user home,
the `~/dom` folder can shadow the installed `dom` package. Use a normal `cd` into
the `app` symlink so the installer records the stable app path.

## Cut a release

Deploy from tags, not from branch heads. A tag is the version Dom reports about
himself, and it names the release folder. Do this on the development machine, from
a clean `main` that has passed CI. It needs Git 2.32 or later and the
[GitHub CLI](https://cli.github.com/).

1. Bump `version` in `pyproject.toml` to the new `X.Y.Z` and commit it.
2. Generate the changelog section from the commits since the previous tag. It
   lists one line per commit, so edit it into bullets a reader can follow.
   Put it in `CHANGELOG.md` directly under an empty `## [Unreleased]` heading,
   replacing the old Unreleased content, and commit.

   ```sh
   uv run python scripts/changelog.py vPREVIOUS HEAD --title "[X.Y.Z] - $(date +%F)"
   ```

3. Tag the commit and publish the release, using the changelog section as notes.

   ```sh
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > release-notes.md
   gh release create vX.Y.Z --verify-tag --title vX.Y.Z --notes-file release-notes.md
   rm release-notes.md
   ```

`git archive` writes the tag into `dom/_version.py` (`.gitattributes` marks it
`export-subst`), so a release folder built from a tag reports `vX.Y.Z` to
`self_inspect`. A folder built from an untagged commit reports the nearest tag
plus the commit, such as `v0.7.0-3-g1a2b3c4`. Only a history with no tag at all
reports the bare commit.

## Stage the first release

For the first install, clone the repository into a separate checkout, then run
these commands from it. Choose a release tag. Later updates use `dom deploy` or
`/update` instead (see below).

```sh
root=~/dom
tag=vX.Y.Z
release="$root/releases/$tag"
mkdir -p "$root/releases"
mkdir "$release"
git archive "$tag" | tar -x -C "$release"
(cd "$release" && uv sync --frozen --no-dev)
```

Use a new release directory each time. If staging fails, leave `app` pointing
at the current release until the new one is ready.

## Install on first run

```sh
ln -s "releases/$tag" "$root/app"
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

## Update to a new release

`dom deploy` does the whole update. In Telegram, `/update` (owner DM only) runs
the same command for you: it shows the newest release newer than the running
one, with its changelog section, and deploys it when you press the button.
Releases come from `[update] repo_url` (the public repository by default). Only
tags that are exactly `vX.Y.Z` are offered or deployed, never branches, other
refs or pre-release tags.

```sh
cd "$root/app"
.venv/bin/python -m dom deploy "$root/instance" vX.Y.Z
```

The steps, in order:

1. Fetch the release tags into `~/dom/repo.git` and check that the tag exists
   (and, for `/update`, still points at the commit you were shown).
2. Unpack the tag into `releases/vX.Y.Z` with `git archive` and run
   `uv sync --frozen --no-dev` there. Dom keeps running meanwhile. If this
   fails, nothing else changes.
3. Stop `dom.service` and run `dom backup` on the instance home. If the backup
   fails, the current release starts again and the update stops.
4. Repoint `app` to the new release (a new symlink renamed over the old one)
   and start `dom.service`.
5. Wait up to 120 seconds (`--health-seconds`) for the new process to report
   ready and stay up for 10 seconds. On success, delete every release folder
   except the new one and the previous one.
6. Otherwise repoint `app` to the previous release and start it again. If that
   also fails, Dom messages you through the Bot API directly.

The outcome is saved in the instance home (`update-state.json`) and the Dom
process that comes up sends it to you in Telegram. `uv` is looked up on `PATH`
and then in `~/.local/bin`. Only one deploy runs at a time.

`/update` runs the deploy in a transient user unit named `dom-update`, so it
keeps running while `dom.service` restarts. Read its log with
`journalctl --user -u dom-update`. The first `/update` needs a release that
already has the command, so deploy that one with `dom deploy` by hand.

### Roll back by hand

To go back to the previous release, stop Dom and repoint `app`, using the
folder name `dom deploy` reported as kept:

```sh
systemctl --user stop dom.service
ln -s "releases/<previous>" "$root/app.next"
mv -Tf "$root/app.next" "$root/app"
systemctl --user start dom.service
```

From v0.8.3 the dashboard can save any reasoning effort a provider lists, such as
`high`. v0.8.2 and older accept only `low` and `medium`, and refuse to start with
any other `reasoning_effort` in `config.toml`. Before rolling back past v0.8.3,
restore the copy the last dashboard save kept, or set each effort back:

```sh
cp "$DOM_HOME/config.toml.bak" "$DOM_HOME/config.toml"   # the file before the last save
grep -n reasoning_effort "$DOM_HOME/config.toml"           # or edit each to low or medium, or delete the line
```

`config.toml.bak` holds the file as it was before the most recent save, so it also
undoes that save's other change; `config.toml.orig` is the file before the first
save from the dashboard.

Rollbacks rely on every migration staying readable and writable by the previous
release: add tables, columns and indexes, or rebuild a table with the same columns
(for example to widen a `CHECK`), but never remove or rename anything the previous
release uses. The previous release skips versions it does not know and runs on the
migrated database. A release that breaks this rule must say so in its changelog,
and is rolled back by restore.

If the newer code migrated the database to an incompatible version, move the
current instance home aside and use the old release's
`dom restore <archive> <home>` to restore the pre-update archive before starting
the old service. Preserve the moved home for recovery of data written after the
update. Keep the bot stopped during the restore.

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
