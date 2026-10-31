# timelog-sync

`aw-client` with one command more: `aw-client timelog` reads ActivityWatch on this computer and
uploads each day's time to TimeLog, for the person's own eyes (TimeLog shows a person only their own rows):

- per app, and per window title inside an app,
- per website (hostname), and per page: its address without query or fragment, and its title,
- Claude Code's active seconds, per day and per session (id, project folder, first and last message, seconds).

It never sends an event, a URL's query, a message of Claude Code, or anything on the exclusion list.

## What stays on the PC

- **Private windows.** The browser watcher marks private tabs and they are dropped; a window title naming a
  private window (Incognito, InPrivate, Private Browsing…) is dropped too.
- **The exclusion list.** Hosts (with their subdomains) and apps never sent: not as time, a site, a page or a
  title. It ships with Colombian banks (bancolombia.com, davivienda.com, bbva.com.co, nequi.com.co,
  daviplata.com) and password managers (1Password, Bitwarden, LastPass, Dashlane, KeePass, KeePassXC, NordPass,
  Proton Pass). Change it with `exclude = ["mybank.com", "Foo.exe"]` in `aw-timelog.toml` (that list replaces
  the default; several entries as a string split by `;` also work), or `--exclude` (repeatable, added to the
  default) and `--no-default-exclude` on the command line. `write_exclude()` in `aw_client/timelog_module.py`
  is what a tray menu calls.
- **Browsers' window titles.** A browser's window title is its page title: the pages above carry it, so the
  window rows leave browsers out.
## On Windows, with the installer

1. Download the installer from https://github.com/WAB-Studio/activitywatch/releases/latest and install it.
   It is ActivityWatch with `aw-timelog` bundled; quit any ActivityWatch already running first.
2. In TimeLog, «Llaves» → «Crear llave»; copy the `tl_` key.
3. Right-click ActivityWatch's icon next to the clock → «TimeLog: pegar llave…» → paste it.

`aw-timelog` starts with ActivityWatch and uploads within a minute, then every hour. It reads
`aw-timelog.toml` in ActivityWatch's config folder before each upload: `key`, `url`, `device`
(the computer's name by default), `interval_minutes` (60), `days` (2) and `claude_dir` (Claude Code's logs). Its log is under
«Open log folder» in the same menu.

## On Windows, by hand

1. Install ActivityWatch and leave it running.
2. `py -m pip install --user git+https://github.com/WAB-Studio/timelog-sync@timelog`
3. In TimeLog, «Llaves» → «Crear llave»; copy the `tl_` key.
4. Try it: `py -m aw_client timelog --url https://timelog-delta.vercel.app --key tl_… --dry-run`
5. Schedule it every hour (PowerShell):

   ```powershell
   $action = New-ScheduledTaskAction -Execute "pyw" -Argument "-m aw_client timelog --url https://timelog-delta.vercel.app --key tl_…"
   $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
   Register-ScheduledTask -TaskName "TimeLog" -Action $action -Trigger $trigger
   ```

`py -m aw_client` needs no PATH: pip puts `aw-client.exe` in a folder Windows does not search. `pyw` runs the
task without opening a console window every hour.

`--days` (default 2) sends the last days again, so a day missed while the PC slept is filled the next
time; each day is replaced whole. `TIMELOG_URL` and `TIMELOG_KEY` stand in for the two flags.
