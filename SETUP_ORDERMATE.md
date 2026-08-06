# Moving the dashboard to ORDERMATE

Target: **ORDERMATE** — Fujitsu, Xeon E3-1230 v6 (4c/8t @ 3.5GHz), 8 GB RAM, Windows Server 2012 R2
Foundation. Replaces the **Surface Book** (i7-6600U, 2c/4t, Wi-Fi, unreliable charger) as the machine
the shop depends on.

Nothing here changes the live server until step 9. Steps 1–8 are safe to do while the shop trades.

---

## Why the Surface can't simply be swapped out

The NIIMBOT label printer is Bluetooth. `bleak` drives Bluetooth through **WinRT**, which is Windows
10+ only, and **Windows Server ships no Bluetooth stack at all** — so no USB dongle can make BLE
printing work on 2012 R2. The 80mm network printer works but its paper barely sticks; fine for burger
boxes, not for food labels.

Fix: **don't move the printer, move the bytes.** The Surface stays on as a label relay. ORDERMATE
renders the label and POSTs the PNG to it; the Surface does the BLE print. Same codebase both ends.

Result: the Surface stops being the machine the shop depends on. If its charger finally dies you lose
labels and remote access — **not trade**.

---

## 1. BIOS

- **Restore on AC Power Loss → ON.** A power cut takes the whole shop down anyway; what matters is
  that the server comes back by itself instead of waiting for someone to find the power button.
- Note whether **TPM / Intel PTT** and **Secure Boot** exist. Not needed now; needed if you ever put
  Windows 11 on it.

## 2. Network

- Plug into the router by **ethernet**. This is the whole point — the Surface's Marvell AVASTAR Wi-Fi
  caused the 2 Aug dinner-rush dropouts.
- Reserve its IP in the router (DHCP reservation, tidier than a static). Write the IP down.
- **No Tailscale needed.** The Surface already advertises `192.168.0.0/24` as a Tailscale subnet
  route (`RouteAll: true`), so ORDERMATE is reachable remotely the moment it has a LAN address.

## 3. Python

Install **Python 3.12** (64-bit) — 3.12 is the **last version that supports Server 2012 R2**; 3.13
dropped it. Tick **"Add python.exe to PATH"**.

Verify: `python -V` → `Python 3.12.x`

## 4. Dependencies

```
pip install flask certifi pypdf pillow soco thermoworks-cloud aiohttp
pip install bleak
```

`bleak` is expected to fail — to install, or to import at runtime. **That is fine and does not break
anything.** Every `import bleak` in the app is inside a function, so the app still boots; only the
local BLE path is unavailable, and labels go via the relay instead.

## 5. ffmpeg

Needed for the camera feed and rotisserie counting. Download a Windows build, unzip, and add its
`bin` folder to the system PATH.

Verify: `ffmpeg -version`

## 6. Copy the app across

From the Surface's live folder
`C:\Users\me\Downloads\KitchenDashboard-ServerPC-2\KitchenDashboard-ServerPC\`, copy:

- `dashboard_app.py`, `dashboard_ui.html`, `rfx_bridge.py`, `weekly-books.html`, `Chicken.png`
- `start_dashboard_watchdog.bat`
- **`kitchen_data.json`** — all settings, credentials, products, staff. Never in git; must be copied
  by hand.
- `selfupdate.json` (optional — it will rebuild itself)

## 7. Firewall

Allow **TCP 8080 inbound** on the Private profile, so tablets and the Tailscale subnet route can
reach it.

## 8. Test run — ON A DIFFERENT PORT

> **Do not run two dashboards against live Square at once.** Both would poll orders, both would
> deduct stock, both would print tickets, both could email suppliers. Test on a **copy** of
> `kitchen_data.json` and on a port that isn't 8080.

Check, in this order:

| Check | How |
|---|---|
| App boots | page loads on `http://<ordermate-ip>:<testport>` |
| Probes | temps appear (RFX cloud — no Bluetooth involved) |
| Camera | live tile renders (proves ffmpeg) |
| Square | orders board populates |
| Network printer | Settings → label printer test |
| Sonos | music panel responds |

## 9. Cutover (the only step that touches the live shop — do it quiet, not mid-service)

1. Stop the dashboard on the Surface.
2. Start ORDERMATE's on **8080** with the **real** `kitchen_data.json`.
3. Set up the watchdog: `start_dashboard_watchdog.bat` via a scheduled task at boot
   (see `setup-autodeploy.bat` / the existing `KitchenDashboardRun` task for the pattern).
   Keep `ExecutionTimeLimit` at **PT72H** — `PT0S` kills the task instantly on this build of Windows.
4. Point the tablets and the kiosk at the new IP.
5. On the **Surface**: leave the dashboard running. It now serves only `/api/label_relay` and the
   Tailscale subnet route.
6. On **ORDERMATE**, set the label printer:

```json
{ "mode": "relay",
  "relay_url": "http://<surface-ip>:8080",
  "relay_key": "<any shared secret, same on both>" }
```

   Set the same `relay_key` on the Surface. Then print a test label and confirm it comes out of the
   NIIMBOT on sticky stock.

## 10. Security note

Server 2012 R2 stopped getting security patches in **October 2023**. ORDERMATE therefore stays
**LAN-only**: no port forwarding, nothing exposed to the internet, remote access via Tailscale only.

---

## Rollback

Stop ORDERMATE's dashboard, start the Surface's on 8080, point the tablets back. The Surface keeps a
full working copy throughout — nothing is deleted from it at any stage.
