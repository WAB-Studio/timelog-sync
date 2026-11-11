# timelog-sync

`aw-client` with one command more: `aw-client timelog` reads ActivityWatch on this computer and
uploads each day's time to TimeLog, for the person's own eyes (TimeLog shows a person only their own rows):

- per app, and per window title inside an app,
- per website (hostname), and per page: its address without query or fragment, and its title,
- Claude Code's active seconds, per day and per session (id, project folder, first and last message, seconds).

It never sends an event, a URL's query, a message of Claude Code, or anything on the exclusion list.

## What stays on the PC

- **Private windows.** A window title naming a private window (Incognito, InPrivate, Private Browsing,
  «Modo incógnito», «Navegación privada»…) is dropped, and so is a window whose controls name one. Private
  windows are absent from the browsers' history anyway.
- **The exclusion list.** Hosts (with their subdomains) and apps never sent: not as time, a site, a page or a
  title. It ships with Colombian banks (bancolombia.com, davivienda.com, bbva.com.co, nequi.com.co,
  daviplata.com) and password managers (1Password, Bitwarden, LastPass, Dashlane, KeePass, KeePassXC, NordPass,
  Proton Pass). Change it with `exclude = ["mybank.com", "Foo.exe"]` in `aw-timelog.toml` (that list replaces
  the default; several entries as a string split by `;` also work), or `--exclude` (repeatable, added to the
  default) and `--no-default-exclude` on the command line. `write_exclude()` in `aw_client/timelog_module.py`
  is what a tray menu calls.
- **Browsers.** No browser extension, for Chrome, Edge, Brave, Vivaldi, Opera, Arc, Chromium and Firefox.
  On Windows `aw-timelog` reads the address bar of the focused browser window every `poll_seconds` (5) through
  UI Automation and keeps the page (host, address without query or fragment, title) and its seconds in a small
  SQLite file in its data folder. Private windows, excluded hosts and an address being typed are never written.
  Only the time ActivityWatch says you were at the computer counts. Where there is no sample (another
  system, or the reading failed) the focused window's title, without «- Google Chrome», «— Mozilla Firefox»,
  «- Microsoft Edge»…, is looked up in the browser's own history (every profile): the file is copied to a
  temporary folder and the copy is read, the original is never written. A title nothing places is not sent;
  its time stays under the browser's app row. Safari is not read.
- **VS Code.** Its window titles («file — folder — Visual Studio Code», Insiders, Cursor, WSL and Remote
  windows) go up like any window title; TimeLog groups them by project folder.

## On Windows, with the installer

1. Download the installer from https://github.com/WAB-Studio/activitywatch/releases/latest and install it.
   It is ActivityWatch with `aw-timelog` bundled; quit any ActivityWatch already running first.
2. In TimeLog, «Llaves» → «Crear llave»; copy the `tl_` key.
3. Right-click ActivityWatch's icon next to the clock → «TimeLog: pegar llave…» → paste it.

`aw-timelog` starts with ActivityWatch and uploads within a minute, then every hour. It also updates itself:
it checks the newest release when it starts and every six hours, downloads the setup, checks its SHA-256 and
reinstalls in silence; ActivityWatch closes and opens again on its own. «TimeLog: buscar actualización» in the
tray checks right away. `auto_update = false` in `aw-timelog.toml` leaves only the tray's check. It reads
`aw-timelog.toml` in ActivityWatch's config folder before each upload: `key`, `url`, `device`
(the computer's name by default), `interval_minutes` (60), `days` (2), `claude_dir` (Claude Code's logs), `poll_seconds` (5; 0 turns the address-bar reading off), `timelog_enabled` and `gmail_enabled` (both true; see below), the Gmail settings below and, only to override the automatic search, `browser_history` (a list of history files or browser data folders; under WSL, `/mnt/c/Users/<user>/AppData/Local/...`). Its log is under
«Open log folder» in the same menu.

## TimeLog and Gmail, each on or off

The program does two jobs and each is optional. Tray: «TimeLog: subir mi tiempo» and «Gmail: reenviar correos del banco»
are checkable entries that write `timelog_enabled` and `gmail_enabled` (both default to true) in `aw-timelog.toml`;
they are read again every round, so no restart is needed. With TimeLog off nothing is uploaded and the address bar
is not read; no `tl_` key is needed. With Gmail off no round runs. The auto-updater runs either way.
A member who wants only the bank emails turns TimeLog off.

## Bank emails to HomeFund (Gmail)

The same program forwards the bank notification emails of a Gmail account to HomeFund, which turns each into a
movement. Off until an account is connected and a HomeFund key is pasted.

1. Tray → «Gmail: conectar cuenta…». The first time it asks for one line, `<client_id> <client_secret>`, which you copy
   from HomeFund → Ajustes → «Correos del banco»; it is saved in `aw-timelog.toml` as `gmail_client_id` and
   `gmail_client_secret`. A self-hoster can create their own OAuth client in Google Cloud («Desktop app», Gmail API on)
   and put both in the toml; the dialog is skipped while both are set. They never go in the repository.
2. In HomeFund, create a webhook key (`whk_…`). Tray → «HomeFund: pegar llave…» (or `homefund_key` in the toml).
3. The browser opens Google's consent page; accept. The account's token is saved
   as `gmail/<address>.json` in the config folder. Repeat for each account.

Settings in `aw-timelog.toml`: `gmail_minutes` (10; 0 turns Gmail off), `gmail_query` (the banks: Bancolombia, Nequi,
Lulo, Nu, Falabella, Banco de Bogotá, ACH, Davivienda, BBVA, Daviplata; the value replaces it), `gmail_client_id`,
`gmail_client_secret`, `homefund_url` (https://our-piggy-bank.vercel.app), `homefund_key`.

Each round, per account: refresh the access token, list `gmail_query after:<time>`, read each new message and POST
`{subject, text, html}` to `<homefund_url>/api/webhooks/email`. HomeFund does not dedupe, so `gmail/<address>.state`
keeps the last time and the ids already sent: each email goes once. A first run starts from now and never
backfills. A 5xx or no network is tried again next round; a 401 or 422 is logged and that email is skipped.
Gmail runs in its own thread: a failure there never touches the TimeLog upload.

Privacy: access is read-only (`gmail.readonly`). Only the subject and body of emails matching `gmail_query` leave
the PC, and only to `homefund_url`. Tokens and the client secret stay in the config folder.

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
