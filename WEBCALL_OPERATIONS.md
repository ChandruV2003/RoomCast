# NTC Newark WebCall Operations

## Public listener flow

- Open `https://ntcnas.myftp.org/webcall/`
- Enter the 4-digit PIN `7070`
- The room page will try to start audio automatically
- If the browser blocks autoplay, tap `Start Audio`
- The page will show whether the meeting is active right now
- You can also send people `https://ntcnas.myftp.org/webcall/p/7070` to skip manual PIN entry

## Control panel flow

- Open `https://ntcnas.myftp.org/webcall/admin`
- Enter the admin password
- Pick `Room A` or `Room B`
- Press `Start Call` to bring that room live
- Press `Stop Call` to take the room down
- Use `Settings` for input order and schedule entries
- Use `Turn Auto On` from the control panel only if a room was manually held on or off and you want it following the saved schedule again

The admin panel is only a monitor/control surface. Closing the browser does not stop the service.

Settings always uses the current device list reported by each laptop agent, so the panel stays in sync with whatever Windows is actually exposing.

## What the volunteers actually do

- Power on the source laptop and sign into Windows
- Leave the Scarlett interface connected
- Open `/webcall/admin`
- Confirm `Agent online`
- Confirm the current input looks correct
- Confirm listeners can hear audio if this is an active meeting
- Use `Settings` only when schedules or input order need to change

## What the laptop agent does

- Runs in the background on the source laptop
- Polls WebCall for desired state
- Pulls the current Windows input list into the server
- Chooses the first available input from the saved service order
- Starts and stops ffmpeg publishing
- Reports current device, ingest state, and last error
- Warns after sustained silence
- Allows schedule-driven auto-stop only after the scheduled end time and sustained silence
- Prevents duplicate agent instances for the same host

Current limitation: the agent does not yet power-cycle USB devices or restart Focusrite drivers. It handles publish/retry logic, not hardware recovery.

## Listener visibility

- The admin panel shows live listener count per room
- `Listeners now` shows who is currently connected
- `Recent access` shows who connected recently and when
- Web listeners are labeled from their IP address
- Phone listeners are labeled from the calling number when the provider passes it through

## Setting up a new source laptop

1. Copy the WebCall files to the laptop
2. Install Python 3.12+ and ffmpeg
3. Get the host slug and heartbeat token from the server
4. Run the install script:

```powershell
.\install_roomcast_agent_task.ps1 `
  -ServerUrl "https://ntcnas.myftp.org/webcall" `
  -HostSlug "<host-slug>" `
  -Token "<heartbeat-token>" `
  -TaskName "WebCall Source Agent"
```

5. The installer will stop stale RoomCast host processes, refresh the task, and start it immediately
6. Confirm the task is running
7. Open the control panel and verify the new host shows `Agent online`
8. Open `Settings` and confirm the known device list is populated for that host
9. Set the input order for that host so the preferred device is first

That is enough for another Windows source laptop to join the system. The server stays the same; the only host-specific values are the slug and heartbeat token.

## Using another building or network

Yes. A source laptop on another network can publish to the same server as long as:

- it can reach `https://ntcnas.myftp.org/webcall`
- its host slug/token are registered on the server
- the selected input device is available locally

Listeners can join from anywhere that can reach the public WebCall URL.

## Current phone status

- The app-side phone flow exists
- The server can issue a signed phone stream URL and track phone listener sessions
- A real phone number is not live yet
- To make PSTN calling public, a paid provider account and number still need to be provisioned
