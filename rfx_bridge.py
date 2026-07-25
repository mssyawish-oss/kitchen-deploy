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
import asyncio, subprocess, sys

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


async def _read_channels(client, serial):
    """Discover a device's channels by probing 1..8 (RFX presents each probe/sensor as a channel)."""
    try:
        from thermoworks_cloud import ResourceNotFoundError
    except Exception:
        ResourceNotFoundError = tuple()
    out = []
    for n in range(1, 9):
        try:
            ch = await client.get_device_channel(serial, str(n))
        except ResourceNotFoundError:
            continue
        except Exception:
            continue
        if ch is None:
            continue
        out.append({"channel": ch.number or str(n), "label": ch.label,
                    "value": ch.value, "units": ch.units, "value_c": _c(ch.value, ch.units),
                    "status": ch.status, "last_seen": str(ch.last_seen)})
    return out


async def _account_id(client):
    user = await client.get_user()
    return (getattr(user, "account_id", None) or getattr(user, "id", None)
            or getattr(user, "user_id", None))


async def _diagnose(email, password):
    session, client = await _connect(email, password)
    try:
        acct = await _account_id(client)
        devices = await client.get_devices(acct) if acct else []
        rows = []
        for d in devices:
            rows.append({
                "serial": d.serial,
                "label": d.label or d.device_name or d.serial,
                "type": d.type,
                "battery": d.battery,
                "wifi_strength": d.wifi_strength,
                "gateway_rssi": d.gateway_rssi,
                "transmit_secs": d.transmit_interval_in_seconds,
                "last_seen": str(d.last_seen),
                "channels": await _read_channels(client, d.serial),
            })
        return {"ok": True, "account_id": acct, "device_count": len(rows), "devices": rows}
    finally:
        await session.close()


def rfx_diagnose(email, password, log=lambda m: None):
    """Manual 'Test connection' — returns every device + channel the account can see, so the
    right serial:channel -> probe mapping can be set. Re-authenticates each call (fine; it's manual)."""
    if not email or not password:
        return {"ok": False, "error": "Enter your ThermoWorks email and password first."}
    if not _ensure_lib(log):
        return {"ok": False, "error": "thermoworks-cloud library isn't installed on the server yet."}
    try:
        return asyncio.run(_diagnose(email, password))
    except Exception as e:
        name = type(e).__name__
        msg = str(e) or name
        if "Auth" in name or "credential" in msg.lower() or "password" in msg.lower():
            msg = ("Login failed — check the email/password. It must be a plain email+password "
                   "ThermoWorks account, NOT 'Sign in with Google/Apple'.")
        return {"ok": False, "error": msg}


async def _read_mapped(email, password, mapping):
    """mapping: {"1": [serial, channel], "2": [...], ...} -> returns {1: temp_c, ...}."""
    session, client = await _connect(email, password)
    try:
        temps = {}
        for pid, sc in (mapping or {}).items():
            try:
                serial, channel = sc[0], str(sc[1])
                ch = await client.get_device_channel(serial, channel)
                temps[int(pid)] = _c(ch.value, ch.units) if ch else None
            except Exception:
                temps[int(pid)] = None
        return temps
    finally:
        await session.close()


def rfx_read_temps(email, password, mapping, log=lambda m: None):
    """Poller entry point: returns {probe_id: celsius} for the configured mapping, or {} on failure."""
    if not (email and password and mapping):
        return {}
    if not _ensure_lib(log):
        return {}
    try:
        return asyncio.run(_read_mapped(email, password, mapping))
    except Exception as e:
        log(f"rfx read error: {e}")
        return {}
