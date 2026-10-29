# timelog-sync

`aw-client` with one command more: `aw-client timelog` reads ActivityWatch on this computer and
uploads each day's time per app to TimeLog. It sends the app's name and its seconds; never a window
title, never a URL.

## On Windows

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
