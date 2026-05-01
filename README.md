# RoomCast

`RoomCast` is the internal project name for the server-centered audio bridge behind `NTC Newark WebCall`.

The public product is the listener and admin experience at:

- `https://ntcnas.myftp.org/webcall/`
- `https://ntcnas.myftp.org/webcall/admin`

## What is in this repo

- `roomcast_server.py`
  Public listener UI, admin control panel, listener/session tracking, CSV reports, and source ingest endpoints.
- `roomcast_store.py`
  SQLite-backed room, host, schedule, and meeting state.
- `roomcast_agent.py`
  Background laptop agent that reports devices, chooses the first available input from the saved order, and publishes audio.
- `install_roomcast_agent_task.ps1`
  Windows scheduled-task installer for source laptops.
- `docker-compose.roomcast.yml`
  TrueNAS-friendly service definition.
- `WEBCALL_OPERATIONS.md`
  Operator runbook for volunteers and future host setup.

## Operating model

- The NAS hosts the public listener UI and the admin control panel.
- Each source laptop runs one background agent.
- The admin panel is only a remote control surface. Closing it does not stop a live room.
- Room schedules are saved on the server.
- Schedule starts are exact.
- Schedule-driven stops only happen after both:
  - the schedule end time has passed
  - sustained silence has been detected

## Local development

1. Create a virtual environment and install RoomCast dependencies.

```bash
python3 -m venv .venv-roomcast
./.venv-roomcast/bin/pip install -r requirements.roomcast.txt
```

2. Run the test suite.

```bash
PYTHONWARNINGS=ignore::ResourceWarning ./.venv-roomcast/bin/python -m unittest -q test_schedule_engine.py test_roomcast_store.py test_roomcast_agent.py test_roomcast_server.py
```

3. Run the web app locally.

```bash
./.venv-roomcast/bin/python roomcast_server.py
```

Default internal port: `1967`

## Deploying the server

```bash
docker compose -f docker-compose.roomcast.yml up -d --build
```

Important environment values:

- `ROOMCAST_SECRET_KEY`
- `ROOMCAST_ADMIN_PASSWORD`
- `ROOMCAST_DEFAULT_PIN`
- `ROOMCAST_TWILIO_WEBHOOK_TOKEN`
- `ROOMCAST_TELEPHONY_SECRET`

## Installing a new source laptop

1. Install Python `3.12+` and `ffmpeg`.
2. Clone or copy this repo to the laptop.
3. Get the host slug and heartbeat token from the server.
4. Run:

```powershell
.\install_roomcast_agent_task.ps1 `
  -ServerUrl "https://ntcnas.myftp.org/webcall" `
  -HostSlug "<host-slug>" `
  -Token "<heartbeat-token>" `
  -TaskName "WebCall Source Agent" `
  -PollIntervalSeconds 3
```

5. The installer will register startup and logon triggers, clear stale RoomCast agent processes for that host, and start the task immediately.
6. Sign into Windows and confirm the scheduled task is running.
6. Open the admin panel and check that:
   - the host shows `Agent online`
   - the known input list appears under `Settings`
   - the input order is saved with the preferred device first

## Current defaults

- Shared listener PIN: `7070`
- Public name: `NTC Newark WebCall`
- Room B default schedule:
  - Thursday and Friday `18:50-21:00` enabled
  - Saturday `17:50-20:30` enabled
  - Morning templates `10:20-13:00` saved disabled for every day
  - Evening templates `18:50-21:00` saved disabled for Monday through Wednesday
  - No Sunday evening template
- Room A default schedule:
  - Wednesday `18:50-21:00` enabled
  - Sunday `10:20-14:00` enabled
  - No other saved rows
- Silence handling:
  - The admin panel warns after 15 seconds without program audio.
  - After a scheduled end time, auto-stop waits for 5 minutes of sustained silence before ending the room.

## Current limitations

- The agent handles publish/retry logic, not USB driver restarts.
- The installer auto-detects `py -3.13`, `-3.12`, `-3.11`, then `-3` in that order unless you pass `-PythonSelector`.
