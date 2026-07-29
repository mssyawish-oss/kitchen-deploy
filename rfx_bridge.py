"""
ThermoWorks RFX bridge for the kitchen dashboard.

Pulls probe temperatures from ThermoWorks Cloud (via the unofficial `thermoworks-cloud`
library) and hands them to the dashboard the SAME way the FM230 Bluetooth dock does —
so the cook state machine, alarms, standby and stock crediting all work unchanged.

OPT-IN and SAFE:
- Nothing here runs unless db['thermoworks'] has an email + password AND probe_source == 'rfx'.
- It never touches the live BLE path; selecting RFX just changes where temps come from.

CLOUD / INTERNET DEPENDENT and UNOFFICIAL: reads from ThermoWorks' Firebase backend. Can break
if they change their web service; catch-all guards keep the dashboard alive if it does.

Auth needs a plain EMAIL + PASSWORD ThermoWorks account (NOT "Sign in with Google/Apple").
"""
import asyncio, subprocess, sys, threading, time

_LIB_OK = None

def _ensure_lib(log=lambda m: None):
    """Import thermoworks_cloud, pip-installing it once if missing (the self-updating Windows box
    pulls code files but has no manual pip step, so self-heal the dependency here)."""
    global _LIB_OK
    if _LIB_OK is not None:
        return _LIB_OK
    try:
        import thermoworks_cloud  # noqa: F401
        _LIB_OK = True
    except Exception:
        try:
            log("rfx: installing thermoworks-cloud …")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                            "thermoworks-cloud", "aiohttp"], check=True, timeout=240)
            import thermoworks_cloud  # noqa: F401
            _LIB_OK = True
        except Exception as e:
            log(f"rfx: could NOT install thermoworks-cloud: {e}")
            _LIB_OK = False
    return _LIB_OK


async def _connect(email, password):
    import aiohttp
    from thermoworks_cloud import AuthFactory, ThermoworksCloud
    session = aiohttp.ClientSession()
    try:
        auth = await AuthFactory(session).build_auth(email, password)
        return session, ThermoworksCloud(auth)
    except Exception:
        await session.close()
        raise


# ── persistent event loop + cached login ────────────────────────────────────────────────────────
# A full read used to take ~20s because every poll re-authenticated from scratch. aiohttp sessions are
# bound to the event loop that made them, and asyncio.run() builds a NEW loop each call — so a cached
# session is only possible if we keep ONE loop alive in a background thread and run everything on it.
_LOOP = {"loop": None}
_CONN = {"session": None, "client": None, "email": None, "at": 0.0}
_AUTH_TTL = 2400          # re-login every 40 min (tokens outlive this; cheap insurance)
_CHANNELS = {}            # {serial: [channel numbers that actually exist]} — discovered once, then reused


def _run(coro, timeout=120):
    """Run a coroutine on the shared background loop (created on first use)."""
    if _LOOP["loop"] is None:
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True, name="rfx-loop").start()
        _LOOP["loop"] = loop
    return asyncio.run_coroutine_threadsafe(coro, _LOOP["loop"]).result(timeout=timeout)


async def _client(email, password):
    """The logged-in cloud client, reused across polls."""
    c = _CONN
    if (c["client"] is not None and c["email"] == email
            and (time.time() - c["at"]) < _AUTH_TTL):
        return c["client"]
    await _drop_conn()
    session, client = await _connect(email, password)
    _CONN.update({"session": session, "client": client, "email": email, "at": time.time()})
    return client


async def _drop_conn():
    """Forget the cached login (called on any error, so the next poll re-authenticates cleanly)."""
    s = _CONN.get("session")
    _CONN.update({"session": None, "client": None, "email": None, "at": 0.0})
    if s is not None:
        try: await s.close()
        except Exception: pass


def _c(value, units):
    """Normalise a reading to Celsius (the dashboard works in C)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if units and str(units).strip().upper().startswith("F"):
        v = (v - 32) * 5.0 / 9.0
    return round(v, 1)


async def _one_channel(client, serial, n):
    try:
        from thermoworks_cloud import ResourceNotFoundError
    except Exception:
        ResourceNotFoundError = tuple()
    try:
        ch = await client.get_device_channel(serial, str(n))
    except ResourceNotFoundError:
        return None
    except Exception:
        return None
    if ch is None:
        return None
    return {"channel": ch.number or str(n), "label": ch.label,
            "value": ch.value, "units": ch.units, "value_c": _c(ch.value, ch.units),
            "status": ch.status, "last_seen": str(ch.last_seen)}


async def _read_channels(client, serial, rescan=False):
    """A device's channels. The first look probes 1..8 to discover which exist; after that only the
    known ones are fetched — and always CONCURRENTLY. (Was 8 sequential requests per device every
    poll, half of them 404s for channels that never existed: the bulk of a 20-second read.)"""
    nums = None if rescan else _CHANNELS.get(serial)
    probe = list(range(1, 9)) if nums is None else list(nums)
    results = await asyncio.gather(*[_one_channel(client, serial, n) for n in probe])
    out = [r for r in results if r is not None]
    if nums is None:                       # remember what this device actually has
        _CHANNELS[serial] = [p for p, r in zip(probe, results) if r is not None]
    elif not out:                          # cache went stale (device swapped?) → rediscover next time
        _CHANNELS.pop(serial, None)
    return out


async def _account_id(client):
    user = await client.get_user()
    return (getattr(user, "account_id", None) or getattr(user, "id", None)
            or getattr(user, "user_id", None))


async def _diagnose(email, password):
    """Uses the SHARED cached login (never closes it — the poller depends on it staying open)."""
    client = await _client(email, password)
    acct = await _account_id(client)
    devices = await client.get_devices(acct) if acct else []
    rows = []
    for d in devices:
        chans = await _read_channels(client, d.serial, rescan=True)   # Test = rediscover from scratch
        vals = [c.get("value_c") for c in chans if c.get("value_c") is not None]
        core = round(min(vals), 1) if vals else None
        label = d.label or d.device_name or d.serial
        # a mappable PROBE = has real sensor readings and isn't the gateway (gateway ch reads NO PROBE)
        is_probe = bool(vals) and "GATEWAY" not in (label or "").upper()
        rows.append({
            "serial": d.serial, "label": label, "type": d.type,
            "battery": d.battery, "wifi_strength": d.wifi_strength, "gateway_rssi": d.gateway_rssi,
            "transmit_secs": d.transmit_interval_in_seconds, "last_seen": str(d.last_seen),
            "channels": chans, "core": core, "sensor_count": len(chans), "is_probe": is_probe,
        })
    return {"ok": True, "account_id": acct, "device_count": len(rows),
            "probe_count": sum(1 for r in rows if r["is_probe"]), "devices": rows}


def rfx_diagnose(email, password, log=lambda m: None):
    """Manual 'Test connection' — returns every device + channel the account can see, so the
    right serial:channel -> probe mapping can be set. Re-authenticates each call (fine; it's manual)."""
    if not email or not password:
        return {"ok": False, "error": "Enter your ThermoWorks email and password first."}
    if not _ensure_lib(log):
        return {"ok": False, "error": "thermoworks-cloud library isn't installed on the server yet."}
    try:
        return _run(_diagnose(email, password))
    except Exception as e:
        try: _run(_drop_conn(), timeout=20)
        except Exception: pass
        name = type(e).__name__
        msg = str(e) or name
        if "Auth" in name or "credential" in msg.lower() or "password" in msg.lower():
            msg = ("Login failed — check the email/password. It must be a plain email+password "
                   "ThermoWorks account, NOT 'Sign in with Google/Apple'.")
        return {"ok": False, "error": msg}


_STALE_SECS = 300   # a probe silent this long (docked/charging/off) = not in use → show blank, not a frozen temp


def _is_stale(last_seen_s):
    """True if a channel's last_seen timestamp is older than _STALE_SECS. A docked/charging RFX probe
    stops transmitting but the cloud keeps its LAST reading — without this check the dashboard would
    show that stale number as if it were live. Unparseable timestamps count as fresh (fail open)."""
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(str(last_seen_s))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() > _STALE_SECS
    except Exception:
        return False

async def _device_sensors(client, serial):
    """All FRESH sensor readings (°C) for one probe, in channel order. Stale channels (docked/charging
    probe — the cloud keeps old values) are dropped so a silent probe returns []."""
    vals = []
    for ch in await _read_channels(client, serial):
        c = ch.get("value_c")
        if c is not None and not _is_stale(ch.get("last_seen")):
            vals.append(c)
    return vals


async def _device_core_temp(client, serial):
    """One RFX MEAT probe has 4 internal sensors (channels). The meat's true centre is the COLDEST
    of them (the deepest point) — that's the safe 'is it cooked' reading. Return min channel °C,
    or None if the probe has gone quiet (docked/charging) — a frozen old temp must not look live."""
    vals = await _device_sensors(client, serial)
    return round(min(vals), 1) if vals else None


def _serial_of(m):
    """A mapping value is a serial string (new) or [serial, channel] (old) — normalise to the serial."""
    if isinstance(m, (list, tuple)):
        return m[0] if m else None
    return m


_BATT = {"at": 0.0, "map": {}}   # {serial: percent}, refreshed at most every _BATT_TTL seconds
_BATT_TTL = 300

async def _battery_map(client):
    """{serial: battery%} for every device on the account. Cached — battery moves slowly and the
    device list is an extra two API calls, which we don't want on every 8s poll."""
    import time as _t
    if _t.time() - _BATT["at"] < _BATT_TTL and _BATT["map"]:
        return _BATT["map"]
    try:
        acct = await _account_id(client)
        devices = await client.get_devices(acct) if acct else []
        _BATT["map"] = {d.serial: d.battery for d in devices if d.battery is not None}
        _BATT["at"] = _t.time()
    except Exception:
        pass          # keep whatever we had; battery is advisory, never break the temp read for it
    return _BATT["map"]


async def _read_mapped(email, password, mapping):
    """mapping: {"1": serial, "2": serial, ...} — each dashboard probe -> one RFX probe (device).
    Returns {1: {"core": c, "sensors": [c,c,c,c], "battery": pct}, ...} — core = coldest sensor
    (true meat centre), sensors = every fresh internal reading so the dashboard can show them all.
    Uses the cached login and reads every probe CONCURRENTLY."""
    client = await _client(email, password)
    batt = await _battery_map(client)
    items = [(int(pid), _serial_of(m)) for pid, m in (mapping or {}).items()]

    async def one(serial):
        if not serial: return []
        try: return await _device_sensors(client, serial)
        except Exception: return []

    got = await asyncio.gather(*[one(s) for _, s in items])
    return {pid: {"core": (round(min(v), 1) if v else None), "sensors": v,
                  "battery": batt.get(serial)}
            for (pid, serial), v in zip(items, got)}


def rfx_read_temps(email, password, mapping, log=lambda m: None):
    """Poller entry point: returns {probe_id: {...}} for the configured mapping, or {} on failure.
    Retries ONCE with a fresh login — a cached token that expired mid-shift must not blank the probes."""
    if not (email and password and mapping):
        return {}
    if not _ensure_lib(log):
        return {}
    for attempt in (1, 2):
        try:
            return _run(_read_mapped(email, password, mapping))
        except Exception as e:
            try: _run(_drop_conn(), timeout=20)     # force a clean re-login next time
            except Exception: pass
            if attempt == 2:
                log(f"rfx read error: {e}")
                return {}
    return {}
