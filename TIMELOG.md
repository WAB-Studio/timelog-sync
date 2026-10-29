# timelog-sync

`aw-client` with one command more: `aw-client timelog` reads ActivityWatch on this computer and
uploads each day's time per app to TimeLog. It sends the app's name and its seconds; never a window
title, never a URL.

## On Windows

1. Install ActivityWatch and leave it running.
2. `pip install git+https://github.com/WAB-Studio/timelog-sync`
3. In TimeLog, «Llaves» → «Crear llave»; copy the `tl_` key.
4. Try it: `aw-client timelog --url https://timelogapp.vercel.app --key tl_… --dry-run`
5. Schedule it every hour (PowerShell):

   ```powershell
   $action = New-ScheduledTaskAction -Execute "aw-client" -Argument "timelog --url https://timelogapp.vercel.app --key tl_…"
   $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
   Register-ScheduledTask -TaskName "TimeLog" -Action $action -Trigger $trigger
   ```

`--days` (default 2) sends the last days again, so a day missed while the PC slept is filled the next
time; each day is replaced whole. `TIMELOG_URL` and `TIMELOG_KEY` stand in for the two flags.
