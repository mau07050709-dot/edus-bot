# EDUS Bot

Windows desktop application for managing a user-authorized EDUS appointment search through a personal Brave browser. Built with Python, Qt for Python and Playwright over the local Chrome DevTools Protocol.

This independent source export starts with an empty account catalog. Runtime files are stored under `%LOCALAPPDATA%/EDUS_Bot_Public` and credentials under the **EDUS Bot Public** Windows Credential Manager service.

## Features

- Dark desktop interface, persistent custom background, account management and per-account preferences.
- Personal Brave connection, verified account/session association and bounded connection recovery.
- Monitoring restores the verified page after recoverable navigation or local CDP interruptions.
- Visual security-code recognition with challenge renewal and manual intervention when needed.
- Service and specialty selection, fresh-response validation and per-account local consultation history.
- One-time search or continuous monitoring. After an empty result, monitoring waits two minutes, closes the verified EDUS session, reloads the login form and authenticates again.
- Foreground appointment notification and a locally generated bugle alarm.
- Appointment selection by table order or earliest date/time, detail validation, confirmation flow and receipt reconciliation.
- Persistent protection against duplicate or ambiguous booking attempts, plus a Stop control.

## Windows setup

Use 64-bit Windows 10/11, Python 3.12 or 3.13, and Brave installed separately.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

In your personal Brave, open `brave://inspect/#remote-debugging`, enable debugging for that browser instance, and accept its local connection prompt when requested. Availability of this setting depends on your Brave version. Keep one EDUS tab open and Windows awake.

Use the account form under **Opciones avanzadas** to add your own credentials. Choose your identification type and verify the establishment assigned by EDUS. Configure your service, specialty and appointment preference. The interface currently uses Spanish.

`settings.example.json` documents a blank configuration. Normal setup uses the GUI. Credentials belong in Windows Credential Manager rather than in a JSON file or environment file.

## Operation

**INICIAR EDUS BOT** authorizes a search and potential automatic booking for the selected account and that execution. Choose **EDUS real · Consulta única** for a single search, or **EDUS real · Recargar cada 2 minutos** for monitoring. Monitoring has no cycle-count or elapsed-hours cap and ends on a detected offer, Stop, or a blocking error.

**Consultar ahora** performs a query without sending a booking. **Simulación local** exercises notifications and scheduling using fictitious results. Changing accounts requires the official session transition.

A detected offer triggers the alarm before booking confirmation. A reservation is reported as confirmed only after matching evidence appears in the official appointment listing. Unknown screens, inconsistent data, blocked sessions or uncertain responses require intervention.

## Access and service conditions

This is an independent project and is not affiliated with or endorsed by CCSS. Use your own authorized account and review the current official service conditions before enabling real automation. The configured two-minute delay is an application setting, not evidence of a CCSS-authorized polling rate. Respect server errors, additional checks and access restrictions.

Official access and user guidance are available through the [EDUS portal](https://edus.ccss.sa.cr/eduscitasweb/). The application uses the user's existing Brave profile. This repository contains source and app assets only.

## Tests

All test accounts, identifiers, facilities and responses are fictitious. The browser tests intercept requests with local fixtures and require Brave to be installed.

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_monitoring test_navigation test_booking test_booking_page
```

Run from this directory. Tests do not require a real EDUS account. The public export was validated locally. Real sign-in, availability, CAPTCHA acceptance and successful real booking require separate authorized service access and were not exercised for this export. Full real booking remains unverified.

## Build from source

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\Compilar.ps1
```

A versioned directory is created under `dist`. Keep the executable alongside its `_internal` directory. The optional `-DesktopShortcut` switch creates **EDUS Bot Public.lnk**. Binary distribution requires a separate dependency-license review.

## Architecture

- `window.py`, `theme.py` — UI, alarms and personal background.
- `browser.py`, `brave.py`, `session_link.py` — browser transport, authentication and verified session association.
- `captcha.py`, `appointments.py`, `query.py` — image recognition, portal navigation and result validation.
- `booking.py`, `booking_page.py` — offer selection, confirmation and receipt evidence.
- `monitoring.py`, `schedule.py` — sequential scheduling utilities.
- `accounts.py`, `credentials.py`, `settings.py`, `consultation_history.py`, `activity.py` — local persistence.

## Licensing and privacy

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for dependency licenses and asset provenance. A license for the application source has not been selected by its owner. Select one before presenting this as an open-source project or inviting reuse.

Never commit runtime account catalogs, credentials, session links, databases, logs, personal background images, browser profiles or patient screenshots. The export excludes these files physically as well as through `.gitignore`.
