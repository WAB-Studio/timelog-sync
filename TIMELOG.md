# timelog-sync

`aw-client` with one command more: `aw-client timelog` reads ActivityWatch on this computer and
uploads each day's time to TimeLog as seconds, classified here on the PC:

- per app (its name),
- per website, by hostname alone (`github.com`, never a path, a query or a page title),
- Claude Code's active seconds.

It never sends a window title, a URL path, a page title or an event.

## Websites and Claude Code

- **Websites** need ActivityWatch's browser extension (aw-watcher-web) in the browser. It makes the
  `aw-watcher-web-*` bucket; with none, no sites are sent and TimeLog says how to install it.
- **Claude Code** is read from its session logs (`~/.claude/projects`, that is
  `%USERPROFILE%\.claude\projects` on Windows). Only each message's timestamp is read; time is the
  stretches where messages are at most 5 minutes apart (a lone message is one minute), merged across
  sessions and cut at the Bogotá day. If Claude Code runs in WSL, point at its folder with
  `--claude-dir "\\wsl$\Ubuntu\home\<user>\.claude\projects"` (repeatable) or, for `aw-timelog`, with
  `claude_dir` in `aw-timelog.toml` (several folders split by `;`). With no folder found nothing is
  sent for Claude Code and TimeLog keeps what it has.

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
