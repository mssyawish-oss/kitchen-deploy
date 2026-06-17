"""Kitchen Operations Dashboard v2"""
import asyncio, threading, json, os, sys, socket, smtplib, time, urllib.request, urllib.parse, ssl, re
from datetime import datetime, timedelta, timezone

# macOS/python.org ships without root certs — use certifi's CA bundle so HTTPS (Square) + SMTP TLS (email) verify.
try:
    import certifi; SSL_CTX=ssl.create_default_context(cafile=certifi.where())
except Exception: SSL_CTX=ssl.create_default_context()
from email.mime.text import MIMEText
from flask import Flask, Response, request, jsonify, send_file, session, redirect

NOTIFY_CHAR  = "0000ff01-0000-1000-8000-00805f9b34fb"
PRINTER_PORT = 9100
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))   # all data lives next to this script (portable to any machine)
HISTORY_FILE = os.path.join(BASE_DIR, "bbq_history.json")
DATA_FILE    = os.path.join(BASE_DIR, "kitchen_data.json")

def load_data():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE,encoding="utf-8") as f: return json.load(f)
    except Exception as e: print(f"Load error:{e}")
    return {"suppliers":[],"products":[],"recipes":[],"packdown_tasks":[],"packdown_log":[],"service_alerts":[],"quiet_jobs":[],"email_config":{},"staff":[]}

def save_data(data):
    try:
        with open(DATA_FILE,"w",encoding="utf-8") as f: json.dump(data,f,indent=2)
    except Exception as e: print(f"Save error:{e}")

db = load_data()
probe_temps={1:None,2:None,3:None,4:None}
probe_names={1:"Probe 1",2:"Probe 2",3:"Probe 3",4:"Probe 4"}
probe_lock=threading.Lock()
ble_status={"connected":False,"message":"Scanning..."}
settings={"cooked_temp":80.0,"almost_temp":70.0,"overdone_temp":90.0,"use_by_minutes":90,"quality_minutes":90,"printer_ip":"192.168.0.151","bbq_drop_minutes":70,"fried_drop_minutes":15,"bbq_pieces":4,"fried_pieces":18}
probe_state={i:{"status":"idle","alerted":False,"printed":False,"peak_temp":None,"removed":False,"removal_timer":None,"cook_start":None} for i in range(1,5)}
state_lock=threading.Lock()
data_lock=threading.Lock()
timer_triggers={1:False,2:False,3:False,4:False}

# ── shared batch timers (state lives in db so every device sees the same countdown) ──
def _default_timers():
    s=settings["use_by_minutes"]*60
    return [{"id":i,"label":"Batch %d"%(i+1),"total":s,"remaining":s,"running":False,"end_at":None,"expired":False} for i in range(4)]
if not isinstance(db.get("timers"),list) or len(db.get("timers") or [])!=4:
    db["timers"]=_default_timers(); save_data(db)

def timers_snapshot():
    # compute live 'remaining' for running timers; lazily flip expired ones
    now=time.time(); changed=False; out=[]
    with data_lock:
        for t in db["timers"]:
            if t.get("running") and t.get("end_at"):
                rem=t["end_at"]-now
                if rem<=0:
                    t["running"]=False;t["expired"]=True;t["remaining"]=0;t["end_at"]=None;changed=True
                    out.append(dict(t))
                else:
                    snap=dict(t);snap["remaining"]=int(round(rem));out.append(snap)
            else:
                out.append(dict(t))
        if changed: save_data(db)
    return out

_hist_cache={"mtime":None,"data":{"batches":[]}}
def load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            m=os.path.getmtime(HISTORY_FILE)
            if _hist_cache["mtime"]!=m:
                with open(HISTORY_FILE,encoding="utf-8") as f: _hist_cache["data"]=json.load(f)
                _hist_cache["mtime"]=m
            return _hist_cache["data"]
    except Exception as e: print(f"History error:{e}")
    return {"batches":[]}

def avg_cook_time(bt,default):
    h=load_history(); r=[b["cook_mins"] for b in h["batches"] if b.get("type")==bt][-10:]
    return round(sum(r)/len(r)) if len(r)>=3 else default

def record_batch(bt,mins):
    # learn real drop cadence per type (bbq/fried) — written by the frontend on each drop
    try:
        with data_lock:
            h=load_history();h.setdefault("batches",[]).append({"type":bt,"cook_mins":round(mins),"time":datetime.now().isoformat()})
            h["batches"]=h["batches"][-200:]
            with open(HISTORY_FILE,"w",encoding="utf-8") as f: json.dump(h,f)
            _hist_cache["mtime"]=None
    except Exception as e: print(f"record_batch:{e}")

def record_cook(pid,name,temp,cook_mins):
    # food-safety cook log: one entry per cooked batch
    try:
        with data_lock:
            log=db.setdefault("cook_log",[])
            log.append({"time":datetime.now().isoformat(),"probe":pid,"name":name,"temp":round(temp,1),"cook_mins":cook_mins})
            db["cook_log"]=log[-1000:]
            save_data(db)
    except Exception as e: print(f"record_cook:{e}")

def _add_min(hhmm,m):
    try:
        h,mn=map(int,hhmm.split(":"));tot=(h*60+mn+m)%1440;return f"{tot//60:02d}:{tot%60:02d}"
    except Exception: return "23:59"

def build_rotisserie_profile(text,mapping):
    import csv as _csv, io as _io, datetime as _dt
    from collections import defaultdict
    birds=defaultdict(float);dd=defaultdict(set)
    for row in _csv.DictReader(_io.StringIO(text)):
        k=(row.get('Item') or '').strip()+'|'+(row.get('Price Point Name') or '').strip()
        if k not in mapping: continue
        if (row.get('Event Type') or '').strip().lower() not in ('payment','sale',''): continue
        try: d=_dt.date.fromisoformat((row.get('Date') or '').strip());hh=int((row.get('Time') or '0').strip()[:2]);q=float(row.get('Qty') or 0)
        except Exception: continue
        b=q*mapping[k]
        if b>0: birds[(d.weekday(),hh)]+=b;dd[d.weekday()].add(d)
    profile={str(w):{str(h):round(birds[(w,h)]/(len(dd[w]) or 1),2) for h in range(8,23)} for w in range(7)}
    return profile,{str(w):len(dd[w]) for w in range(7)},round(sum(birds.values()))

DEFAULT_ROT_MAP={'BBQ CHICKEN|WHOLE':1.0,'BBQ CHICKEN|1/2':0.5,'BBQ CHICKEN|1/4':0.25,'1/2 BBQ CHICKEN PACK|Regular':0.5,'1/4 BBQ CHICKEN PACK|Regular':0.25}

def compose_daily_report():
    now=datetime.now();today=now.strftime("%Y-%m-%d")
    L=["BRUNO'S CHICKEN SHOP — DAILY REPORT",now.strftime("%A %d %B %Y, %H:%M"),"="*40,""]
    cooks=[c for c in db.get("cook_log",[]) if c.get("time","")[:10]==today]
    L.append(f"COOKED BATCHES: {len(cooks)}")
    for c in cooks[-40:]:
        cm=c.get("cook_mins")
        L.append(f"  {c.get('time','')[11:16]}  {c.get('name','?')}  {c.get('temp','?')}C"+(f"  ({cm} min)" if cm else ""))
    L.append("")
    L.append(f"Avg cook time:  BBQ {avg_cook_time('bbq',settings['bbq_drop_minutes'])} min   Fried {avg_cook_time('fried',settings['fried_drop_minutes'])} min")
    L.append("")
    tasks=db.get("packdown_tasks",[]);done={}
    for e in db.get("packdown_log",[]):
        if e.get("time","")[:10]==today and e.get("done"): done[e.get("taskId")]=e
    L.append(f"PACKDOWN: {len(done)}/{len(tasks)} tasks completed")
    miss=[t for t in tasks if t.get("id") not in done]
    if miss:
        L.append("  MISSED:")
        for t in miss: L.append(f"   - {t.get('name','?')} ({t.get('shift','day')} shift)")
    if done:
        L.append("  Completed:")
        for e in list(done.values())[:50]: L.append(f"   - {e.get('taskName','?')}  by {e.get('staff','?')} at {e.get('time','')[11:16]}")
    L.append("")
    notes=db.get("handover_notes",[])
    if notes:
        L.append(f"HANDOVER NOTES ({len(notes)}):")
        for n in notes[-25:]: L.append(f"   - {n.get('text','')}"+(f"  — {n.get('who')}" if n.get('who') else ""))
        L.append("")
    return "\n".join(L)

def report_loop():
    sent_date=None
    while True:
        try:
            cfg=db.get("report_config",{}) or {}
            if cfg.get("enabled",True) and (db.get("email_config",{}) or {}).get("smtp_user"):
                now=datetime.now();today=now.strftime("%Y-%m-%d");hhmm=now.strftime("%H:%M");t=cfg.get("time","22:30")
                if sent_date!=today and t<=hhmm<_add_min(t,3):
                    to=cfg.get("to") or db["email_config"].get("smtp_user")
                    try: send_email(to,"Bruno's — Daily Report "+now.strftime("%d/%m/%Y"),compose_daily_report());print("Daily report sent to",to)
                    except Exception as e: print(f"report send:{e}")
                    sent_date=today
        except Exception as e: print(f"report_loop:{e}")
        time.sleep(60)

def print_ticket(probe_id,batch_name,temp,use_by_time):
    try:
        ESC=b'\x1b';GS=b'\x1d';now=datetime.now()
        lines=(ESC+b'\x61\x01'+GS+b'\x21\x11'+ESC+b'\x45\x01'+b"COOKED\n"+GS+b'\x21\x00'+ESC+b'\x45\x00'+b"\n"+
               ESC+b'\x45\x01'+f"Batch: {batch_name}\n".encode()+ESC+b'\x45\x00'+
               f"Probe:     {probe_id}\n".encode()+b"--------------------------------\n"+
               f"Temp:      {temp:.1f} C\n".encode()+f"Time off:  {now.strftime('%H:%M  %d/%m/%Y')}\n".encode()+
               f"Use by:    {use_by_time.strftime('%H:%M  %d/%m/%Y')}\n".encode()+b"--------------------------------\n\n"+
               ESC+b'\x64\x04'+GS+b'\x56\x41\x05')
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
            s.settimeout(5);s.connect((settings["printer_ip"],PRINTER_PORT));s.sendall(lines)
    except Exception as e: print(f"Printer:{e}")

def trigger_timer_start(pid):
    i=pid-1
    if 0<=i<4:
        secs=settings["use_by_minutes"]*60; now=time.time()
        with data_lock:
            db["timers"][i].update({"total":secs,"remaining":secs,"running":True,"expired":False,"end_at":now+secs})
            save_data(db)
    timer_triggers[pid]=True

def check_probe_status(pid,temp):
    cooked=settings["cooked_temp"];almost=settings["almost_temp"];overdone=settings["overdone_temp"]
    with state_lock:
        ps=probe_state[pid]
        if ps["peak_temp"] is None or temp>ps["peak_temp"]: ps["peak_temp"]=temp
        prev=ps["status"]
        ns="overdone" if temp>=overdone else "ready" if temp>=cooked else "almost" if temp>=almost else "idle"
        ps["status"]=ns
        if prev=="idle" and ns!="idle" and not ps.get("cook_start"): ps["cook_start"]=time.time()
        if ns in("ready","overdone") and not ps["printed"]:
            ps["printed"]=True
            ub=datetime.now()+timedelta(minutes=settings["use_by_minutes"])
            cook_mins=round((time.time()-ps["cook_start"])/60) if ps.get("cook_start") else None
            threading.Thread(target=record_cook,args=(pid,probe_names.get(pid,f"Probe {pid}"),temp,cook_mins),daemon=True).start()
            threading.Thread(target=print_ticket,args=(pid,probe_names.get(pid,f"Probe {pid}"),temp,ub),daemon=True).start()
        if ns=="overdone" and prev=="ready": ps["alerted"]=False
        peak=ps["peak_temp"]
        if peak and peak>=cooked and (peak-temp)>=15 and not ps["removed"]:
            ps["removed"]=True
            if ps["removal_timer"]: ps["removal_timer"].cancel()
            t=threading.Timer(10.0,trigger_timer_start,args=(pid,));t.daemon=True;ps["removal_timer"]=t;t.start()

def handle_data(sender,data):
    if len(data)<6: return
    if data[1]==0x00:
        pid=data[2];raw=int.from_bytes(data[4:6],"little");temp_c=round((raw-1186)/15.5+23,1)
        if 1<=pid<=4 and 0<temp_c<400:
            with probe_lock: probe_temps[pid]=temp_c
            check_probe_status(pid,temp_c)

async def ble_loop():
    from bleak import BleakScanner,BleakClient
    while True:
        try:
            ble_status.update({"connected":False,"message":"Scanning for FM230..."})
            devices=await BleakScanner.discover(timeout=10)
            device=next((d for d in devices if d.name=="FM230"),None)
            if not device: ble_status["message"]="FM230 not found — retrying...";await asyncio.sleep(3);continue
            async with BleakClient(device.address) as client:
                ble_status.update({"connected":True,"message":"Connected to FM230"})
                await client.start_notify(NOTIFY_CHAR,handle_data)
                while client.is_connected: await asyncio.sleep(0.5)
        except Exception as e:
            print(f"BLE:{e}")
            ble_status.update({"connected":False,"message":"Retrying..."});await asyncio.sleep(3)

def send_email(to,subject,body):
    cfg=db.get("email_config",{})
    if not cfg.get("smtp_user"): raise Exception("Email not configured")
    sender=cfg.get("from_addr") or cfg["smtp_user"]
    msg=MIMEText(body);msg["Subject"]=subject;msg["From"]=sender;msg["To"]=to
    with smtplib.SMTP(cfg.get("smtp_host","smtp.gmail.com"),cfg.get("smtp_port",587)) as s:
        s.ehlo();s.starttls(context=SSL_CTX);s.login(cfg["smtp_user"],cfg["smtp_pass"]);s.sendmail(sender,to,msg.as_string())

# ── SQUARE POS — poll recent payments to gauge how busy we are ──
SQUARE_BASE="https://connect.squareup.com"        # sandbox: https://connect.squareupsandbox.com
SQUARE_VERSION="2026-01-22"
square_status={"sales_last_30min":0,"busy":False,"error":"","configured":False}

def query_square_recent(cfg,minutes=30):
    token=(cfg.get("access_token") or "").strip()
    if not token: raise Exception("No Square access token set")
    loc=(cfg.get("location_id") or "").strip()
    begin=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params={"begin_time":begin,"sort_order":"DESC"}
    if loc: params["location_id"]=loc
    url=SQUARE_BASE+"/v2/payments?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"Authorization":"Bearer "+token,"Square-Version":SQUARE_VERSION,"Content-Type":"application/json"})
    with urllib.request.urlopen(req,timeout=10,context=SSL_CTX) as r:
        data=json.loads(r.read().decode())
    payments=data.get("payments") or []
    good=[p for p in payments if p.get("status") in ("COMPLETED","APPROVED")]
    return len(good)

# ── LIVE ROTISSERIE STOCK ── available birds = (loaded & finished) − (sold via Square)
ROT_LIVE={"day":None,"available":0.0,"sold_today":0.0,"seen":[]}
rot_lock=threading.Lock()
_rl0=db.get("rot_live")                                   # restore across restarts so the count is NOT lost
if isinstance(_rl0,dict):
    for _k in ("day","available","sold_today","seen"):
        if _k in _rl0: ROT_LIVE[_k]=_rl0[_k]
_first_sweep=False   # on startup, sweep all of TODAY's sales once to catch up; then poll a short window
def _rot_cfg():
    r=db.get("rotisserie") or {}
    return {"open_rows":r.get("open_rows",2),"bpr":r.get("birds_per_row",4)}
def _rot_save():       # persist live counts so a server restart doesn't reset them
    with rot_lock: snap={k:ROT_LIVE[k] for k in ("day","available","sold_today","seen")}
    try:
        with data_lock: db["rot_live"]=snap; save_data(db)
    except Exception: pass
def _rot_reset_if_needed():
    c=_rot_cfg();today=datetime.now().strftime("%Y-%m-%d")
    if ROT_LIVE["day"]!=today:
        # NEW DAY only → zero the counters (the camera/bench-watcher rebuilds 'available'). A restart mid-day keeps the saved count.
        # IMPORTANT: keep 'seen' (capped) across the rollover — clearing it let yesterday's still-in-window Square
        # orders get RE-counted after midnight (phantom 'sold_today'). Dedup memory must survive the daily reset.
        ROT_LIVE.update({"day":today,"available":0.0,"sold_today":0.0})
        ROT_LIVE["seen"]=ROT_LIVE["seen"][-3000:]
def rot_reset_counts():   # manual "start fresh" — zero today's tallies but KEEP 'seen' so the same orders can't recount
    with rot_lock:
        ROT_LIVE["day"]=datetime.now().strftime("%Y-%m-%d");ROT_LIVE["available"]=0.0;ROT_LIVE["sold_today"]=0.0
        ROT_LIVE["seen"]=ROT_LIVE["seen"][-3000:]
    _rot_save()
def rot_state():
    with rot_lock:
        _rot_reset_if_needed()
        return {"available":round(ROT_LIVE["available"],2),"sold_today":round(ROT_LIVE["sold_today"],2),
                "square":bool((db.get("square_config",{}) or {}).get("access_token") and (db.get("square_config",{}) or {}).get("location_id")),
                "rows_cooking":ROTCAM.get("cooking",0),"cam":bool((db.get("rotcam_config") or {}).get("enabled")),"cam_err":ROTCAM.get("error",""),"levels":ROTCAM.get("levels",""),"done":ROTCAM.get("done","")}
def rot_put_on(rows):   # a finished row went into the warmer → add straight to available
    c=_rot_cfg()
    with rot_lock: _rot_reset_if_needed();ROT_LIVE["available"]+=rows*c["bpr"]
    _rot_save()
def rot_adjust(delta):
    with rot_lock: _rot_reset_if_needed();ROT_LIVE["available"]=max(0.0,ROT_LIVE["available"]+delta)
    _rot_save()
def rot_set(v):
    with rot_lock: _rot_reset_if_needed();ROT_LIVE["available"]=max(0.0,float(v))
    _rot_save()
def rot_deduct(birds,oids):
    with rot_lock:
        _rot_reset_if_needed();ROT_LIVE["available"]=max(0.0,ROT_LIVE["available"]-birds)
        ROT_LIVE["sold_today"]+=birds;ROT_LIVE["seen"].extend(oids);ROT_LIVE["seen"]=ROT_LIVE["seen"][-3000:]
    _rot_save()

# ── LIVE FRIED-CHICKEN STOCK (whole pieces; 18/batch, ~15 min) ──
DEFAULT_FRIED_MAP={"2 PCS FRIED CHICKEN PACK|Regular":2,"3 PCS FRIED CHICKEN PACK|Regular":3,"2 PCS PACK SPECIAL|Regular":2}
FRY_LIVE={"day":None,"available":0.0,"sold_today":0.0,"seen":[]}
fry_lock=threading.Lock()
_fl0=db.get("fry_live")                                   # restore across restarts
if isinstance(_fl0,dict):
    for _k in ("day","available","sold_today","seen"):
        if _k in _fl0: FRY_LIVE[_k]=_fl0[_k]
def _fry_cfg():
    f=db.get("fried") or {}
    return {"batch":f.get("batch",18),"open":f.get("open",18),"low_at":f.get("low_at",6)}
def _fry_save():
    with fry_lock: snap={k:FRY_LIVE[k] for k in ("day","available","sold_today","seen")}
    try:
        with data_lock: db["fry_live"]=snap; save_data(db)
    except Exception: pass
def _fry_reset_if_needed():
    c=_fry_cfg();today=datetime.now().strftime("%Y-%m-%d")
    if FRY_LIVE["day"]!=today:
        FRY_LIVE.update({"day":today,"available":float(c["open"]),"sold_today":0.0})   # keep 'seen' across rollover (see rot note)
        FRY_LIVE["seen"]=FRY_LIVE["seen"][-3000:]
def fry_reset_counts():
    with fry_lock:
        FRY_LIVE["day"]=datetime.now().strftime("%Y-%m-%d");FRY_LIVE["available"]=float(_fry_cfg()["open"]);FRY_LIVE["sold_today"]=0.0
        FRY_LIVE["seen"]=FRY_LIVE["seen"][-3000:]
    _fry_save()
def fry_state():
    with fry_lock:
        _fry_reset_if_needed();c=_fry_cfg()
        return {"available":round(FRY_LIVE["available"]),"sold_today":round(FRY_LIVE["sold_today"]),
                "low_at":c["low_at"],"batch":c["batch"],"square":bool((db.get("square_config",{}) or {}).get("access_token") and (db.get("square_config",{}) or {}).get("location_id"))}
def fry_put_on(batches):   # a finished batch went into the warmer → add straight to available
    c=_fry_cfg()
    with fry_lock: _fry_reset_if_needed();FRY_LIVE["available"]+=batches*c["batch"]
    _fry_save()
def fry_adjust(delta):
    with fry_lock: _fry_reset_if_needed();FRY_LIVE["available"]=max(0.0,FRY_LIVE["available"]+delta)
    _fry_save()
def fry_set(v):
    with fry_lock: _fry_reset_if_needed();FRY_LIVE["available"]=max(0.0,float(v))
    _fry_save()
def fry_deduct(pcs,oids):
    with fry_lock:
        _fry_reset_if_needed();FRY_LIVE["available"]=max(0.0,FRY_LIVE["available"]-pcs)
        FRY_LIVE["sold_today"]+=pcs;FRY_LIVE["seen"].extend(oids);FRY_LIVE["seen"]=FRY_LIVE["seen"][-3000:]
    _fry_save()

SALES_FEED={"day":None,"items":[]}
def sales_feed_add(oid,bbq,fried,sched,items,src,when=None):
    today=datetime.now().strftime("%Y-%m-%d")
    if SALES_FEED["day"]!=today: SALES_FEED.update({"day":today,"items":[]})
    t=when.astimezone().strftime("%H:%M:%S") if when else datetime.now().strftime("%H:%M:%S")  # actual order time, not when counted
    SALES_FEED["items"].append({"time":t,"order":(oid or "")[-6:],
        "bbq":round(bbq,2),"fried":round(fried),"sched":bool(sched),"items":items or [],"src":src or ""})
    SALES_FEED["items"]=SALES_FEED["items"][-600:]

def _parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception: return None

# ── "Hand out now" alarm: orders containing a ready-now item (salad, whole chicken…) ──
DEFAULT_NOWAIT=["SALAD","BBQ CHICKEN","WHOLE CHICKEN","DINNER PACK"]
WAIT_LIVE={"day":None,"orders":{},"done":set(),"acked":set()}
WAIT_EXPIRE_MIN=30   # drop a card after this long (assume handed out / forgotten)
def _nowait_on(): return bool((db.get("nowait_enabled")))
def _nowait_keywords():
    if not _nowait_on(): return []
    items=db.get("nowait_items")
    if not isinstance(items,list) or not items: items=DEFAULT_NOWAIT
    return [str(x).strip().upper() for x in items if str(x).strip()]
def _wait_keywords():
    # items that DO take time (burger, fried…) — an order with any of these never alarms
    items=db.get("wait_items")
    if not isinstance(items,list): return []
    return [str(x).strip().upper() for x in items if str(x).strip()]
def _exclude_delivery(): return db.get("nowait_exclude_delivery",True)!=False   # on by default
def _is_delivery_src(src):
    # delivery-platform orders are picked up by a courier, not a customer waiting at the
    # counter — they should never trigger the "waiting too long" hand-out alarm
    if not _exclude_delivery(): return False
    s=(src or "").lower().replace(" ","")
    return any(k in s for k in ("uber","doordash","menulog","deliveroo"))
def _wait_reset():
    today=datetime.now().astimezone().date().isoformat()
    if WAIT_LIVE["day"]!=today: WAIT_LIVE.update({"day":today,"orders":{},"done":set(),"acked":set()})
def wait_add(oid,items,src,eff,name="",kind="now"):
    _wait_reset()
    if not oid or oid in WAIT_LIVE["done"] or oid in WAIT_LIVE["orders"]: return
    WAIT_LIVE["orders"][oid]={"items":items,"src":src or "","name":name or "","kind":kind,"ts":(eff.timestamp() if eff else time.time()),
        "t":(eff.astimezone().strftime("%H:%M") if eff else datetime.now().strftime("%H:%M"))}
def wait_autoclear(open_ids):
    # an order that's no longer OPEN in Square = it was completed/cancelled on the pass KDS → clear it
    for oid in list(WAIT_LIVE["orders"].keys()):
        if oid not in WAIT_LIVE["done"] and oid not in open_ids:
            WAIT_LIVE["orders"].pop(oid,None); WAIT_LIVE["done"].add(oid)
def _nowait_alarm_min():
    try: return max(0,int(db.get("nowait_alarm_min",3)))
    except Exception: return 3
def _wait_alarm_min():
    try: return max(0,int(db.get("wait_alarm_min",10)))
    except Exception: return 10
def _thr_for(kind): return _wait_alarm_min() if kind=="wait" else _nowait_alarm_min()
def wait_state():
    _wait_reset()
    if not _nowait_on(): return {"orders":[],"alarm":False,"enabled":False}
    now=time.time(); out=[]
    for oid,o in list(WAIT_LIVE["orders"].items()):
        if oid in WAIT_LIVE["done"]: continue
        mins=int((now-o["ts"])/60)
        if mins>WAIT_EXPIRE_MIN: continue
        kind=o.get("kind","now"); thr=_thr_for(kind)
        out.append({"oid":oid,"name":o.get("name",""),"items":o["items"],"src":o.get("src",""),"t":o.get("t",""),
                    "mins":mins,"late":mins>=thr,"kind":kind,"acked":oid in WAIT_LIVE["acked"]})
    out.sort(key=lambda x:x["mins"],reverse=True)
    return {"orders":out,"alarm":any(o["late"] and not o["acked"] for o in out),"enabled":True,
            "alarm_min":_nowait_alarm_min(),"wait_alarm_min":_wait_alarm_min()}

def query_square_sales(cfg,minutes=40,states=("OPEN","COMPLETED")):
    # → per order: (id, bbq whole-bird equiv, fried pcs, scheduled_bool, due_datetime)
    token=(cfg.get("access_token") or "").strip();loc=(cfg.get("location_id") or "").strip()
    if not token or not loc: return []
    begin=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bm=(db.get("rotisserie") or {}).get("map") or DEFAULT_ROT_MAP
    fm=(db.get("fried") or {}).get("map") or DEFAULT_FRIED_MAP;kws=_nowait_keywords();wkws=_wait_keywords();out=[];allo=[];cursor=None
    for _pg in range(8):   # paginate so a full-day catch-up isn't truncated
        q={"location_ids":[loc],"query":{"filter":{"date_time_filter":{"created_at":{"start_at":begin}},"state_filter":{"states":list(states)}},"sort":{"sort_field":"CREATED_AT","sort_order":"ASC"}},"limit":500}
        if cursor: q["cursor"]=cursor
        req=urllib.request.Request(SQUARE_BASE+"/v2/orders/search",data=json.dumps(q).encode(),headers={"Authorization":"Bearer "+token,"Square-Version":SQUARE_VERSION,"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=12,context=SSL_CTX) as r: data=json.loads(r.read().decode())
        allo+=data.get("orders") or [];cursor=data.get("cursor")
        if not cursor: break
    for o in allo:
        oid=o.get("id");birds=0.0;pcs=0.0;matched=[];now_items=[];w_items=[]
        for li in o.get("line_items") or []:
            name=(li.get("name") or "").strip();var=(li.get("variation_name") or "").strip();key=name+"|"+var
            try: q=float(li.get("quantity") or 0)
            except Exception: q=0
            hit=False
            if key in bm: birds+=q*bm[key];hit=True
            if key in fm: pcs+=q*fm[key];hit=True
            elif name=="FRIED CHICKEN":
                mm=re.search(r"\d+",var);pcs+=q*(int(mm.group()) if mm else 1);hit=True
            if hit: matched.append(f"{int(q) if q==int(q) else q}x {name}"+(" "+var if var else ""))
            up=(name+" "+var).upper();disp=f"{int(q) if q==int(q) else q}x {name}"+(" "+var if var else "")
            if kws and any(kw in up for kw in kws): now_items.append(disp)
            if wkws and any(w in up for w in wkws): w_items.append(disp)
        # a cook-to-order item makes the WHOLE order a 'wait' order (longer timer); else ready-now (short timer)
        if w_items: nowait=w_items+[x for x in now_items if x not in w_items];kind="wait"
        elif now_items: nowait=now_items;kind="now"
        else: nowait=[];kind=""
        if not oid or (birds<=0 and pcs<=0 and not nowait): continue
        src=(o.get("source") or {}).get("name") or ""
        created=_parse_dt(o.get("created_at"))
        sched=False;due=None;rname="";ff=[]                     # only SCHEDULED pre-orders get the 15-min gate
        for fu in o.get("fulfillments") or []:
            pd=fu.get("pickup_details") or {};dd=fu.get("delivery_details") or {}
            ff.append((fu.get("state") or "").upper())
            if (pd.get("schedule_type") or dd.get("schedule_type"))=="SCHEDULED":
                d=_parse_dt(pd.get("pickup_at") or dd.get("deliver_at"))
                if d: sched=True;due=d if (due is None or d<due) else due
            rec=(pd.get("recipient") or dd.get("recipient") or {})
            if not rname and rec.get("display_name"): rname=rec.get("display_name")
        # "done on the pass KDS" = every fulfillment is marked ready/done. Square sets PREPARED when an order
        # is bumped/completed on the KDS (COMPLETED only happens at final pickup), so PREPARED clears it too.
        fulfilled=bool(ff) and all(s in ("PREPARED","COMPLETED","CANCELED","CANCELLED","FAILED") for s in ff)
        oname=(o.get("ticket_name") or "").strip() or rname or (o.get("reference_id") or "").strip() or ("#"+(oid or "")[-5:])
        out.append((oid,birds,pcs,sched,due,created,matched,src,nowait,oname,fulfilled,kind))
    return out

def square_poll_loop():
    while True:
        cfg=db.get("square_config",{}) or {}
        if cfg.get("access_token"):
            square_status["configured"]=True
            try:
                n=query_square_recent(cfg,30)
                thr=int(cfg.get("busy_threshold",3) or 3)
                square_status.update({"sales_last_30min":n,"busy":n>=thr,"error":""})
            except Exception as e:
                square_status["error"]=str(e)
            try:
                if cfg.get("location_id"):
                    global _first_sweep
                    rot_state();fry_state()  # ensure daily resets
                    now=datetime.now(timezone.utc);today=now.astimezone().date()
                    mid=now.astimezone().replace(hour=0,minute=0,second=0,microsecond=0)
                    comp_min=(int((now.astimezone()-mid).total_seconds()/60)+5) if not _first_sweep else 45  # first run = all of today
                    open_orders=query_square_sales(cfg,minutes=4320,states=("OPEN",))
                    orders=open_orders+query_square_sales(cfg,minutes=comp_min,states=("COMPLETED",))
                    _first_sweep=True
                    # count TODAY's sales when due (scheduled → at due-15min; else at order time). skip future + pre-today (stale).
                    def _due(s,d,c):
                        eff=d if (s and d) else c
                        if eff is None: return False   # no determinable time → don't count/alarm (was True: let undated stale orders slip through)
                        return now>=eff-timedelta(minutes=15) and eff.astimezone().date()==today
                    rb=fb=0.0;rids=[];fids=[]
                    for (oid,b,p,s,d,c,items,src,nowait,name,fulfilled,kind) in orders:
                        if not _due(s,d,c): continue
                        db_=b if (b>0 and oid not in ROT_LIVE["seen"]) else 0
                        dp_=p if (p>0 and oid not in FRY_LIVE["seen"]) else 0
                        if db_: rb+=db_;rids.append(oid)
                        if dp_: fb+=dp_;fids.append(oid)
                        if db_ or dp_: sales_feed_add(oid,db_,dp_,s,items,src,(d if (s and d) else c))
                    # hand-out alarm: pending = OPEN orders with a tracked item that AREN'T fulfilled yet;
                    # clear ones that left OPEN or whose fulfillment was completed on the pass KDS
                    if _nowait_on():
                        open_ids=set()
                        for (oid,b,p,s,d,c,items,src,nowait,name,fulfilled,kind) in open_orders:
                            if nowait and _due(s,d,c) and not fulfilled and not _is_delivery_src(src):
                                # start the wait timer from when the order first reaches our kitchen queue
                                # (matches the pass KDS), NOT Square's created_at — a pre-order/tab can be
                                # "created" long before it ever fires to the kitchen. Scheduled orders still
                                # count from their due time.
                                open_ids.add(oid); wait_add(oid,nowait,src,(d if (s and d) else None),name,kind)
                        wait_autoclear(open_ids)
                    if rids: rot_deduct(rb,rids)
                    if fids: fry_deduct(fb,fids)
            except Exception as e: print(f"sales-poll:{e}")
        else:
            square_status["configured"]=False
        time.sleep(3)   # how fast a KDS-bump clears the alarm; 3s is snappy without risking Square rate limits

app=Flask(__name__)
# ── Weekly Books owner login (separate from staff PINs; gates the money page on every network) ──
import hashlib, secrets as _secrets
if not db.get("_secret_key"):
    with data_lock: db["_secret_key"]=_secrets.token_hex(32); save_data(db)
app.secret_key=db["_secret_key"]
app.permanent_session_lifetime=timedelta(days=30)
def _hash_pw(pw): return hashlib.sha256((db["_secret_key"]+pw).encode()).hexdigest()
def _books_pw_set(): return bool((db.get("books_auth") or {}).get("hash"))
def _books_ok(): return bool(session.get("books_ok"))

@app.route("/manifest.json")
def manifest():
    return Response(json.dumps({"name":"Bruno's Chicken Shop","short_name":"Bruno's","start_url":"/","display":"standalone","background_color":"#0a0c10","theme_color":"#0a0c10","orientation":"landscape","icons":[{"src":"/icon.png","sizes":"512x512","type":"image/png","purpose":"any maskable"}]}),mimetype="application/manifest+json")

@app.route("/icon.png")
def app_icon():
    p=os.path.join(os.path.dirname(os.path.abspath(__file__)),"icon.png")
    if os.path.exists(p): return send_file(p,mimetype="image/png")
    return Response(status=404)

@app.route("/sw.js")
def sw():
    return Response("self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>clients.claim());self.addEventListener('fetch',e=>e.respondWith(fetch(e.request).catch(()=>caches.match(e.request))));",mimetype="application/javascript")

@app.route("/")
def index():
    ui=os.path.join(os.path.dirname(os.path.abspath(__file__)),"dashboard_ui.html")
    with open(ui,encoding="utf-8") as f: return f.read()

def _books_gate_page(setup):
    title="Set a password for Weekly Books" if setup else "Weekly Books — locked"
    sub=("Choose an owner password. You'll need it to view the figures from now on." if setup
         else "Enter the owner password to view the figures.")
    btn="Set password &amp; open" if setup else "Unlock"
    setjs="true" if setup else "false"
    return ("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Weekly Books</title>"
        "<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#0a0c10;color:#eef1f6;font-family:-apple-system,Segoe UI,Roboto,sans-serif}"
        ".c{width:320px;max-width:90vw;background:#14171e;border:1px solid rgba(255,255,255,.12);"
        "border-radius:16px;padding:26px;text-align:center}h1{font-size:18px;margin:0 0 6px}"
        "p{font-size:13px;color:#98a2b3;margin:0 0 16px}input{width:100%;box-sizing:border-box;"
        "padding:12px;border-radius:10px;border:1px solid rgba(255,255,255,.15);background:#1e222b;"
        "color:#fff;font-size:16px;margin-bottom:10px}button{width:100%;padding:12px;border:none;"
        "border-radius:10px;background:#b45309;color:#fff6e6;font-size:15px;font-weight:600;cursor:pointer}"
        ".e{color:#f87171;font-size:12px;min-height:16px;margin-top:8px}</style>"
        "<div class='c'><h1>"+title+"</h1><p>"+sub+"</p>"
        "<input id='pw' type='password' placeholder='Password' autofocus "
        "onkeydown=\"if(event.key==='Enter')go()\">"
        +("<input id='pw2' type='password' placeholder='Repeat password' onkeydown=\"if(event.key==='Enter')go()\">" if setup else "")+
        "<button onclick='go()'>"+btn+"</button><div class='e' id='err'></div></div>"
        "<script>var SET="+setjs+";async function go(){var pw=document.getElementById('pw').value;"
        "if(SET){var p2=document.getElementById('pw2').value;if(pw.length<4){err('At least 4 characters.');return;}"
        "if(pw!==p2){err('Passwords do not match.');return;}}"
        "var r=await fetch('/books/auth',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({password:pw,set:SET})});var j=await r.json();"
        "if(j.ok){location.href='/books';}else{err(j.error==='wrong'?'Wrong password.':(j.error||'Error'));}}"
        "function err(m){document.getElementById('err').textContent=m;}</script>")

@app.route("/books")
def books():
    if not _books_ok():
        return Response(_books_gate_page(setup=not _books_pw_set()),mimetype="text/html")
    p=os.path.join(os.path.dirname(os.path.abspath(__file__)),"weekly-books.html")
    if not os.path.exists(p): return Response("weekly-books.html not found",status=404)
    with open(p,encoding="utf-8") as f: return f.read()

@app.route("/books/auth",methods=["POST"])
def books_auth():
    d=request.get_json(silent=True) or {}
    pw=str(d.get("password","")); setting=bool(d.get("set"))
    if setting:
        if _books_pw_set(): return jsonify({"ok":False,"error":"already set"})
        if len(pw)<4: return jsonify({"ok":False,"error":"too short"})
        with data_lock: db["books_auth"]={"hash":_hash_pw(pw)}; save_data(db)
        session.permanent=True; session["books_ok"]=True; return jsonify({"ok":True})
    if not _books_pw_set(): return jsonify({"ok":False,"error":"not set"})
    if _hash_pw(pw)==db["books_auth"]["hash"]:
        session.permanent=True; session["books_ok"]=True; return jsonify({"ok":True})
    return jsonify({"ok":False,"error":"wrong"})

@app.route("/books/logout",methods=["POST"])
def books_logout():
    session.pop("books_ok",None); return jsonify({"ok":True})

# ── Weekly Books live-data proxy: the Books app posts {name,args}; we answer with the same
#    shape Square/Google MCP tools return. Square is wired (reuses the stored token); Google
#    (Gmail/Drive) returns benign empties until OAuth is added, so the app falls back to its
#    baked-in verified history. READ-ONLY — no money is ever moved. ──
_GOOGLE_EMPTY={"search_threads":{"threads":[]},"get_thread":{"messages":[]},
               "search_files":{"files":[]},"read_file_content":{}}
_SQ_POST={("labor","searchTimecards"):"/v2/labor/timecards/search",
          ("orders","search"):"/v2/orders/search",("payments","search"):"/v2/payments/search",
          ("team","searchMembers"):"/v2/team-members/search"}   # staff names for Weekly Books wages

def _square_proxy(args):
    cfg=db.get("square_config",{}) or {}
    token=(cfg.get("access_token") or "").strip()
    if not token: return {"errors":[{"detail":"No Square access token configured in settings."}]}
    service=str(args.get("service","")); method=str(args.get("method",""))
    reqp=args.get("request",{}) or {}; key=(service,method)
    hdr={"Authorization":"Bearer "+token,"Square-Version":SQUARE_VERSION,"Content-Type":"application/json"}
    try:
        if key==("payments","list"):
            params={k:v for k,v in reqp.items() if v not in (None,"")}
            req=urllib.request.Request(SQUARE_BASE+"/v2/payments?"+urllib.parse.urlencode(params),headers=hdr)
            with urllib.request.urlopen(req,timeout=15,context=SSL_CTX) as r: return json.loads(r.read().decode())
        if key in _SQ_POST:
            req=urllib.request.Request(SQUARE_BASE+_SQ_POST[key],data=json.dumps(reqp).encode(),headers=hdr)
            with urllib.request.urlopen(req,timeout=20,context=SSL_CTX) as r: return json.loads(r.read().decode())
        return {"errors":[{"detail":"Unsupported Square call %s.%s"%(service,method)}]}
    except Exception as e:
        rd=getattr(e,"read",None)
        if rd:
            try: return json.loads(e.read().decode())
            except Exception: pass
        return {"errors":[{"detail":str(e)}]}

# ===== SOLD-OUT / TURNED-OFF PRODUCTS (Square Catalog) =============================
def _sq_headers():
    cfg=db.get("square_config",{}) or {}; token=(cfg.get("access_token") or "").strip()
    if not token: return None
    return {"Authorization":"Bearer "+token,"Square-Version":SQUARE_VERSION,"Content-Type":"application/json"}
def _sq_offline_products():
    # returns [{id,version,item,variation}] for item-variations marked SOLD OUT at this location
    cfg=db.get("square_config",{}) or {}; loc=(cfg.get("location_id") or "").strip(); hdr=_sq_headers()
    if not hdr or not loc: return None,"Square not configured"
    out=[]
    try:
        # --- ITEM variations marked sold out at this location ---
        cursor=None
        for _ in range(25):   # paginate, safety-capped
            url=SQUARE_BASE+"/v2/catalog/list?types=ITEM"+("&cursor="+urllib.parse.quote(cursor) if cursor else "")
            with urllib.request.urlopen(urllib.request.Request(url,headers=hdr),timeout=20,context=SSL_CTX) as r:
                data=json.loads(r.read().decode())
            for obj in data.get("objects",[]) or []:
                if obj.get("type")!="ITEM": continue
                name=((obj.get("item_data") or {}).get("name")) or "?"
                for v in (obj.get("item_data") or {}).get("variations",[]) or []:
                    vd=v.get("item_variation_data") or {}
                    for ov in vd.get("location_overrides") or []:
                        if ov.get("location_id")==loc and ov.get("sold_out"):
                            out.append({"id":v.get("id"),"version":v.get("version"),"item":name,"variation":(vd.get("name") or ""),"kind":"item"})
            cursor=data.get("cursor")
            if not cursor: break
        # --- MODIFIERS (add-ons) marked sold out at this location ---
        cursor=None
        for _ in range(25):
            url=SQUARE_BASE+"/v2/catalog/list?types=MODIFIER"+("&cursor="+urllib.parse.quote(cursor) if cursor else "")
            with urllib.request.urlopen(urllib.request.Request(url,headers=hdr),timeout=20,context=SSL_CTX) as r:
                data=json.loads(r.read().decode())
            for obj in data.get("objects",[]) or []:
                if obj.get("type")!="MODIFIER": continue
                md=obj.get("modifier_data") or {}
                name=md.get("name") or "?"
                for ov in md.get("location_overrides") or []:
                    if ov.get("location_id")==loc and ov.get("sold_out"):
                        out.append({"id":obj.get("id"),"version":obj.get("version"),"item":name,"variation":"add-on","kind":"modifier"})
            cursor=data.get("cursor")
            if not cursor: break
        return out,None
    except Exception as e:
        rd=getattr(e,"read",None)
        if rd:
            try: return None,"Square error: "+e.read().decode()[:180]
            except Exception: pass
        return None,str(e)
def _sq_enable_variation(vid):
    # clear SOLD OUT for one variation OR modifier at this location (turn it back on)
    cfg=db.get("square_config",{}) or {}; loc=(cfg.get("location_id") or "").strip(); hdr=_sq_headers()
    if not hdr or not loc: return False,"Square not configured"
    try:
        with urllib.request.urlopen(urllib.request.Request(SQUARE_BASE+"/v2/catalog/object/"+urllib.parse.quote(vid),headers=hdr),timeout=15,context=SSL_CTX) as r:
            obj=(json.loads(r.read().decode()) or {}).get("object")
        if not obj: return False,"product not found"
        dkey="modifier_data" if obj.get("type")=="MODIFIER" else "item_variation_data"
        vd=obj.get(dkey) or {}; los=vd.get("location_overrides") or []; found=False
        for ov in los:
            if ov.get("location_id")==loc: ov["sold_out"]=False; ov.pop("sold_out_valid_until",None); found=True
        if not found: los.append({"location_id":loc,"sold_out":False})
        vd["location_overrides"]=los; obj[dkey]=vd
        body={"idempotency_key":_secrets.token_hex(16),"batches":[{"objects":[obj]}]}
        req=urllib.request.Request(SQUARE_BASE+"/v2/catalog/batch-upsert-catalog-objects",data=json.dumps(body).encode(),headers=hdr)
        with urllib.request.urlopen(req,timeout=20,context=SSL_CTX) as r: res=json.loads(r.read().decode())
        if res.get("errors"): return False,(res["errors"][0].get("detail") or "error")
        return True,None
    except Exception as e:
        rd=getattr(e,"read",None)
        if rd:
            try: return False,"Square error: "+e.read().decode()[:180]
            except Exception: pass
        return False,str(e)
@app.route("/api/products_offline")
def api_products_offline():
    items,err=_sq_offline_products()
    if err: return jsonify({"ok":False,"error":err})
    return jsonify({"ok":True,"off":items,"count":len(items)})
@app.route("/api/product_enable",methods=["POST"])
def api_product_enable():
    d=request.get_json(silent=True) or {}; vid=str(d.get("id",""))
    if not vid: return jsonify({"ok":False,"error":"no id"})
    ok,err=_sq_enable_variation(vid)
    return jsonify({"ok":ok,"error":err})
# ==================================================================================

# ── Google (Gmail + Drive) via OAuth refresh token, stored in db['google_config'] ──
#    {client_id, client_secret, refresh_token}. Read-only scopes (gmail.readonly, drive.readonly).
_g_tok={"access":None,"exp":0}
def _google_token():
    cfg=db.get("google_config",{}) or {}
    if not (cfg.get("client_id") and cfg.get("client_secret") and cfg.get("refresh_token")): return None
    now=time.time()
    if _g_tok["access"] and _g_tok["exp"]>now+60: return _g_tok["access"]
    data=urllib.parse.urlencode({"client_id":cfg["client_id"],"client_secret":cfg["client_secret"],
        "refresh_token":cfg["refresh_token"],"grant_type":"refresh_token"}).encode()
    req=urllib.request.Request("https://oauth2.googleapis.com/token",data=data,
        headers={"Content-Type":"application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req,timeout=15,context=SSL_CTX) as r: j=json.loads(r.read().decode())
    _g_tok["access"]=j.get("access_token"); _g_tok["exp"]=now+int(j.get("expires_in",3500))
    return _g_tok["access"]

def _g_get_json(url):
    tok=_google_token()
    if not tok: raise RuntimeError("Google not configured")
    req=urllib.request.Request(url,headers={"Authorization":"Bearer "+tok})
    with urllib.request.urlopen(req,timeout=25,context=SSL_CTX) as r: return json.loads(r.read().decode())

def _b64url(s):
    import base64
    s=s.replace('-','+').replace('_','/'); s+='='*(-len(s)%4)
    return base64.b64decode(s)

_GMAIL="https://gmail.googleapis.com/gmail/v1/users/me"
def _gmail_search(args):
    q=args.get("query",""); n=int(args.get("pageSize",20) or 20)
    j=_g_get_json(_GMAIL+"/threads?"+urllib.parse.urlencode({"q":q,"maxResults":n}))
    threads=[]
    for th in (j.get("threads") or [])[:n]:
        try: meta=_g_get_json(_GMAIL+"/threads/"+th["id"]+"?format=metadata&metadataHeaders=Subject")
        except Exception: meta={}
        msgs=[]
        for mm in (meta.get("messages") or []):
            subj=""
            for h in (mm.get("payload",{}).get("headers") or []):
                if h.get("name","").lower()=="subject": subj=h.get("value",""); break
            msgs.append({"subject":subj,"snippet":mm.get("snippet","")})
        threads.append({"id":th["id"],"snippet":th.get("snippet",""),"messages":msgs})
    return {"threads":threads}

def _gmail_thread(args):
    tid=args.get("threadId") or args.get("thread_id")
    j=_g_get_json(_GMAIL+"/threads/"+str(tid)+"?format=full")
    out=[]
    for mm in (j.get("messages") or []):
        payload=mm.get("payload",{}); subj=""
        for h in (payload.get("headers") or []):
            if h.get("name","").lower()=="subject": subj=h.get("value","")
        acc={"html":"","plain":""}
        def walk(p):
            mt=p.get("mimeType",""); body=p.get("body",{})
            if body.get("data"):
                try:
                    txt=_b64url(body["data"]).decode("utf-8","replace")
                    if mt=="text/html": acc["html"]+=txt
                    elif mt=="text/plain": acc["plain"]+=txt
                except Exception: pass
            for sp in (p.get("parts") or []): walk(sp)
        walk(payload)
        out.append({"subject":subj,"snippet":mm.get("snippet",""),"htmlBody":acc["html"],"plaintext_body":acc["plain"]})
    return {"messages":out}

_DRIVE="https://www.googleapis.com/drive/v3"
def _drive_search(args):
    q=args.get("query","")
    m=re.search(r"parentId\s*=\s*'([^']+)'",q)
    gq=("'%s' in parents and trashed=false"%m.group(1)) if m else q
    params={"q":gq,"pageSize":int(args.get("pageSize",100) or 100),
        "fields":"nextPageToken,files(id,name,mimeType,createdTime)",
        "supportsAllDrives":"true","includeItemsFromAllDrives":"true"}
    if args.get("pageToken"): params["pageToken"]=args["pageToken"]
    j=_g_get_json(_DRIVE+"/files?"+urllib.parse.urlencode(params))
    files=[{"id":f["id"],"title":f.get("name",""),"mimeType":f.get("mimeType",""),
            "createdTime":f.get("createdTime","")} for f in (j.get("files") or [])]
    return {"files":files,"nextPageToken":j.get("nextPageToken")}

def _pdf_to_text(raw):
    try:
        import io
        from pypdf import PdfReader
        rd=PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or "") for pg in rd.pages)
    except Exception as e:
        return ""

def _drive_read(args):
    fid=args.get("fileId") or args.get("file_id")
    tok=_google_token()
    if not tok: raise RuntimeError("Google not configured")
    req=urllib.request.Request(_DRIVE+"/files/"+str(fid)+"?alt=media&supportsAllDrives=true",
        headers={"Authorization":"Bearer "+tok})
    with urllib.request.urlopen(req,timeout=45,context=SSL_CTX) as r: raw=r.read()
    text=_pdf_to_text(raw)
    if not text:
        try: text=raw.decode("utf-8","replace")
        except Exception: text=""
    return {"fileContent":text}

@app.route("/api/google_config",methods=["POST"])
def api_google_config():
    # allow the local token-helper (no session) to set it, but never expose it for reading
    d=request.get_json(silent=True) or {}
    g=db.get("google_config",{}) or {}
    for k in ("client_id","client_secret","refresh_token"):
        if k in d: g[k]=str(d[k]).strip()
    with data_lock: db["google_config"]=g; save_data(db)
    _g_tok["access"]=None; _g_tok["exp"]=0
    return jsonify({"ok":True,"configured":bool(g.get("client_id") and g.get("client_secret") and g.get("refresh_token"))})

# ── Task-completion photos: grab a still from a Dahua IP camera instead of a PIN ──
PHOTOS_DIR=os.path.join(BASE_DIR,"task_photos")
try: os.makedirs(PHOTOS_DIR,exist_ok=True)
except Exception: pass

def _camera_snapshot():
    cfg=db.get("camera_config",{}) or {}
    if not cfg.get("ip"): return None,"No camera configured"
    ip=str(cfg["ip"]).strip(); port=cfg.get("port") or 80; ch=cfg.get("channel") or 1
    user=cfg.get("user","") or ""; pw=cfg.get("pass","") or ""
    url=(cfg.get("url_override") or "").strip() or ("http://%s:%s/cgi-bin/snapshot.cgi?channel=%s"%(ip,port,ch))
    try:
        pm=urllib.request.HTTPPasswordMgrWithDefaultRealm(); pm.add_password(None,url,user,pw)
        # cameras use self-signed certs on a trusted local IP — verifying the cert is meaningless and just fails
        cam_ctx=ssl.create_default_context(); cam_ctx.check_hostname=False; cam_ctx.verify_mode=ssl.CERT_NONE
        opener=urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm),
            urllib.request.HTTPBasicAuthHandler(pm),urllib.request.HTTPSHandler(context=cam_ctx))
        with opener.open(urllib.request.Request(url),timeout=8) as r: data=r.read()
        if data[:2]==b"\xff\xd8": return data,None
        return None,"Camera did not return a photo (%d bytes) — check the address/login."%len(data)
    except Exception as e:
        return None,str(e)

def _prune_photos(days=45):
    try:
        cutoff=time.time()-days*86400
        for fn in os.listdir(PHOTOS_DIR):
            if fn.startswith("prep_"): continue   # prep-item preset pictures are permanent, never prune them
            fp=os.path.join(PHOTOS_DIR,fn)
            if os.path.isfile(fp) and os.path.getmtime(fp)<cutoff: os.remove(fp)
    except Exception: pass

@app.route("/api/prep_image/<pid>",methods=["POST"])
def api_prep_image(pid):
    # upload the picture for a prep preset item; saved permanently in the photos folder
    if not re.match(r'^[A-Za-z0-9_-]{1,40}$',pid or ""): return jsonify({"ok":False,"error":"bad id"})
    f=request.files.get("file")
    if not f: return jsonify({"ok":False,"error":"no image"})
    raw=f.read()
    if not raw: return jsonify({"ok":False,"error":"empty image"})
    if len(raw)>5*1024*1024: return jsonify({"ok":False,"error":"image too big (max 5 MB)"})
    ext=(os.path.splitext(f.filename or "")[1] or "").lstrip(".").lower()
    if ext not in ("png","jpg","jpeg","webp","gif"): ext="jpg"
    try:
        for old in os.listdir(PHOTOS_DIR):
            if old.startswith("prep_"+pid+"."): os.remove(os.path.join(PHOTOS_DIR,old))
    except Exception: pass
    fn="prep_%s.%s"%(pid,ext)
    try:
        with open(os.path.join(PHOTOS_DIR,fn),"wb") as out: out.write(raw)
    except Exception as e: return jsonify({"ok":False,"error":str(e)})
    return jsonify({"ok":True,"img":fn})

@app.route("/api/capture",methods=["POST"])
def api_capture():
    data,err=_camera_snapshot()
    if err: return jsonify({"ok":False,"error":err})
    name="t%d_%s.jpg"%(int(time.time()*1000),_secrets.token_hex(3))
    try:
        with open(os.path.join(PHOTOS_DIR,name),"wb") as f: f.write(data)
    except Exception as e: return jsonify({"ok":False,"error":str(e)})
    _prune_photos()
    return jsonify({"ok":True,"photo":name})

@app.route("/api/camera_config",methods=["POST"])
def api_camera_config():
    d=request.get_json(silent=True) or {}; cfg=db.get("camera_config",{}) or {}
    for k in ("ip","user","pass","url_override"):
        if k in d: cfg[k]=str(d[k]).strip()
    for k in ("channel","port"):
        if k in d:
            try: cfg[k]=int(d[k])
            except Exception: pass
    if "enabled" in d: cfg["enabled"]=bool(d["enabled"])
    with data_lock: db["camera_config"]=cfg; save_data(db)
    return jsonify({"ok":True,"enabled":bool(cfg.get("enabled") and cfg.get("ip"))})

@app.route("/api/camera_test",methods=["POST"])
def api_camera_test():
    data,err=_camera_snapshot()
    if err: return jsonify({"ok":False,"error":err})
    import base64
    return jsonify({"ok":True,"preview":"data:image/jpeg;base64,"+base64.b64encode(data).decode()})

# ===== STORE CAMERAS (multi-camera monitoring page) ================================
def _rtsp_frame(url):
    # grab one JPEG from an RTSP camera (TP-Link VIGI / Tapo / ONVIF) via ffmpeg
    import subprocess
    try:
        p=subprocess.run(["ffmpeg","-nostdin","-rtsp_transport","tcp","-i",url,"-an","-frames:v","1","-q:v","4","-f","image2","-"],
                         capture_output=True,timeout=15)
        if p.returncode==0 and p.stdout[:2]==b"\xff\xd8": return p.stdout,None
        err=(p.stderr or b"")[-160:].decode("latin1","ignore").strip()
        return None,"ffmpeg could not grab a frame (check the RTSP URL / login)"+((" — "+err) if err else "")
    except FileNotFoundError:
        return None,"ffmpeg is not installed on the server"
    except Exception as e:
        return None,str(e)
def _snap_from(cam):
    # fetch a single JPEG: RTSP cameras via ffmpeg, otherwise Dahua-style snapshot.cgi (HTTP digest)
    cam=cam or {}
    rtsp=(cam.get("rtsp_url") or "").strip()
    if rtsp: return _rtsp_frame(rtsp)
    base=db.get("camera_config",{}) or {}   # store cams on the same recorder can reuse the saved login
    ip=str(cam.get("ip","")).strip() or str(base.get("ip","")).strip()
    if not ip and not (cam.get("url_override") or "").strip(): return None,"No camera address"
    port=cam.get("port") or base.get("port") or 80; ch=cam.get("channel") or 1
    user=(cam.get("user","") or "") or (base.get("user","") or ""); pw=(cam.get("pass","") or "") or (base.get("pass","") or "")
    scheme="https" if str(port)=="443" else "http"
    url=(cam.get("url_override") or "").strip() or ("%s://%s:%s/cgi-bin/snapshot.cgi?channel=%s"%(scheme,ip,port,ch))
    try:
        pm=urllib.request.HTTPPasswordMgrWithDefaultRealm(); pm.add_password(None,url,user,pw)
        ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        opener=urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm),
            urllib.request.HTTPBasicAuthHandler(pm),urllib.request.HTTPSHandler(context=ctx))
        with opener.open(urllib.request.Request(url),timeout=8) as r: data=r.read()
        if data[:2]==b"\xff\xd8": return data,None
        return None,"Camera did not return a photo (%d bytes) — check address/login."%len(data)
    except Exception as e:
        return None,str(e)
def _cam_by_id(cid):
    return next((c for c in (db.get("cameras") or []) if c.get("id")==cid),None)
@app.route("/api/cameras",methods=["POST"])
def api_cameras():
    d=request.get_json(silent=True) or {}; act=d.get("action","save")
    with data_lock:
        cams=list(db.get("cameras") or [])
        if act=="delete":
            cams=[c for c in cams if c.get("id")!=d.get("id")]
        else:
            cid=d.get("id") or _secrets.token_hex(4)
            existing=next((c for c in cams if c.get("id")==cid),None)
            pw=d.get("pass"); rtsp=d.get("rtsp_url"); ovr=d.get("url_override")
            cam={"id":cid,"name":str(d.get("name","Camera"))[:40],"ip":str(d.get("ip","")).strip(),
                 "port":int(d.get("port") or 80),"channel":int(d.get("channel") or 1),
                 "user":str(d.get("user","")).strip(),"enabled":bool(d.get("enabled",True)),
                 "pass":(pw if pw not in (None,"") else (existing or {}).get("pass","")),
                 "rtsp_url":(rtsp.strip() if isinstance(rtsp,str) and rtsp.strip() else (existing or {}).get("rtsp_url","")),
                 "url_override":(ovr.strip() if isinstance(ovr,str) and ovr.strip() else (existing or {}).get("url_override",""))}
            if existing: cams=[cam if c.get("id")==cid else c for c in cams]
            else: cams.append(cam)
        db["cameras"]=cams; save_data(db)
    return jsonify({"ok":True,"count":len(db.get("cameras") or [])})
@app.route("/api/cam/<cid>.jpg")
def api_cam_snap(cid):
    cam=_cam_by_id(cid)
    if not cam: return Response("not found",status=404)
    data,err=_snap_from(cam)
    if err: return Response(err,status=503)
    return Response(data,mimetype="image/jpeg",headers={"Cache-Control":"no-store"})
@app.route("/api/cam_test",methods=["POST"])
def api_cam_test():
    d=request.get_json(silent=True) or {}
    if not (d.get("pass") or "") and d.get("id"):     # editing existing & pass left blank → use stored pass
        ex=_cam_by_id(d["id"]);
        if ex: d["pass"]=ex.get("pass","")
    data,err=_snap_from(d)
    if err: return jsonify({"ok":False,"error":err})
    import base64 as _b
    return jsonify({"ok":True,"preview":"data:image/jpeg;base64,"+_b.b64encode(data).decode()})
# ==================================================================================

@app.route("/api/features",methods=["POST"])
def api_features():
    d=request.get_json(silent=True) or {}
    with data_lock:
        for k in ("stock_alarm_enabled","probe_alarm_enabled","stock_prompt_enabled","products_off_enabled"):
            if k in d: db[k]=bool(d[k])
        if "stock_prompt_hours" in d:
            try: db["stock_prompt_hours"]=max(1,min(8,int(d["stock_prompt_hours"])))
            except Exception: pass
        save_data(db)
    return jsonify({"ok":True})

# ===== REMOTE RESTART (apply downloaded backend updates without touching the PC) =====
PENDING_FILE=os.path.join(BASE_DIR,"BACKEND-UPDATE-PENDING.txt")
@app.route("/api/restart",methods=["POST"])
def api_restart():
    try:
        if os.path.exists(PENDING_FILE): os.remove(PENDING_FILE)
    except Exception: pass
    def _do():
        import subprocess
        time.sleep(0.8)                       # let the HTTP response flush first
        # If launched by the watchdog batch (start_dashboard.bat sets KDASH_WATCHDOG=1), just exit
        # cleanly — the loop relaunches us with fresh code. Bulletproof, also recovers from crashes.
        if os.environ.get("KDASH_WATCHDOG")=="1":
            os._exit(0)
        # Otherwise (plain single-run launch) fall back to a detached self-relauncher.
        try:
            script=os.path.join(BASE_DIR,"dashboard_app.py")
            if os.name=="nt":
                DETACHED=0x00000008|0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                subprocess.Popen('ping -n 3 127.0.0.1 >nul & "%s" "%s"'%(sys.executable,script),
                                 cwd=BASE_DIR,shell=True,creationflags=DETACHED,close_fds=True)
                time.sleep(0.4); os._exit(0)
            else:
                os.execv(sys.executable,[sys.executable,script])
        except Exception:
            os._exit(0)
    threading.Thread(target=_do,daemon=True).start()
    return jsonify({"ok":True})

# ===== ALARM CENTER — per-alarm settings + uploadable custom sounds =====
ALARM_SOUNDS_DIR=os.path.join(BASE_DIR,"alarm_sounds")
try: os.makedirs(ALARM_SOUNDS_DIR,exist_ok=True)
except Exception: pass
_ALARM_KEYS={"probe","stock","prodoff","timer","rotstopped","orders","service"}
_SND_OK=("mp3","wav","ogg","webm","m4a","aac")
_SND_EXT={"audio/mpeg":"mp3","audio/mp3":"mp3","audio/wav":"wav","audio/x-wav":"wav","audio/wave":"wav","audio/ogg":"ogg","audio/webm":"webm","audio/mp4":"m4a","audio/x-m4a":"m4a","audio/aac":"aac"}

@app.route("/api/alarm_settings",methods=["POST"])
def api_alarm_settings():
    d=request.get_json(silent=True) or {}
    cfg=d.get("alarm_cfg") if isinstance(d.get("alarm_cfg"),dict) else d
    with data_lock:
        store=db.get("alarm_cfg",{}) or {}
        for k,v in (cfg or {}).items():
            if k not in _ALARM_KEYS or not isinstance(v,dict): continue
            cur=store.get(k,{}) or {}
            for b in ("enabled","flash","vibrate"):
                if b in v: cur[b]=bool(v[b])
            if isinstance(v.get("color"),str): cur["color"]=v["color"][:9]
            if "sound_preset" in v: cur["sound_preset"]=str(v.get("sound_preset") or "")[:32]
            if "volume" in v:
                try: cur["volume"]=max(0.0,min(1.0,float(v["volume"])))
                except Exception: pass
            if "interval" in v:
                try: cur["interval"]=max(400,min(60000,int(v["interval"])))
                except Exception: pass
            if "autostop" in v:
                try: cur["autostop"]=max(0,min(120,int(v["autostop"])))
                except Exception: pass
            store[k]=cur
        db["alarm_cfg"]=store
        # keep the legacy on/off flags in sync so the existing alarm gates honour these toggles
        for k,legacy in (("probe","probe_alarm_enabled"),("stock","stock_alarm_enabled"),("prodoff","products_off_enabled")):
            if k in store and "enabled" in store[k]: db[legacy]=store[k]["enabled"]
        save_data(db)
    return jsonify({"ok":True,"alarm_cfg":db.get("alarm_cfg",{})})

@app.route("/api/alarm_sound/<key>",methods=["GET"])
def api_alarm_sound_get(key):
    if key not in _ALARM_KEYS: return ("bad key",404)
    fn=((db.get("alarm_cfg",{}) or {}).get(key,{}) or {}).get("sound_file")
    if not fn: return ("no custom sound",404)
    p=os.path.join(ALARM_SOUNDS_DIR,fn)
    if not os.path.exists(p): return ("missing",404)
    import mimetypes
    return send_file(p,mimetype=(mimetypes.guess_type(p)[0] or "application/octet-stream"))

@app.route("/api/alarm_sound/<key>",methods=["POST"])
def api_alarm_sound_set(key):
    if key not in _ALARM_KEYS: return jsonify({"ok":False,"error":"bad key"})
    f=request.files.get("file")
    if not f: return jsonify({"ok":False,"error":"no file"})
    raw=f.read()
    if not raw: return jsonify({"ok":False,"error":"empty file"})
    if len(raw)>3*1024*1024: return jsonify({"ok":False,"error":"file too big (max 3 MB)"})
    ext=_SND_EXT.get((f.mimetype or "").lower())
    if not ext:
        ext=(os.path.splitext(f.filename or "")[1] or "").lstrip(".").lower()
        if ext not in _SND_OK: ext="mp3"
    try:
        for old in os.listdir(ALARM_SOUNDS_DIR):
            if old.startswith(key+"."): os.remove(os.path.join(ALARM_SOUNDS_DIR,old))
    except Exception: pass
    fn=key+"."+ext
    try:
        with open(os.path.join(ALARM_SOUNDS_DIR,fn),"wb") as out: out.write(raw)
    except Exception as e: return jsonify({"ok":False,"error":str(e)})
    with data_lock:
        store=db.get("alarm_cfg",{}) or {}; cur=store.get(key,{}) or {}
        cur["sound_file"]=fn; cur["sound_v"]=int(time.time()); cur["has_custom"]=True
        store[key]=cur; db["alarm_cfg"]=store; save_data(db)
    return jsonify({"ok":True,"sound_v":db["alarm_cfg"][key]["sound_v"]})

@app.route("/api/alarm_sound/<key>",methods=["DELETE"])
def api_alarm_sound_del(key):
    if key not in _ALARM_KEYS: return jsonify({"ok":False,"error":"bad key"})
    try:
        for old in os.listdir(ALARM_SOUNDS_DIR):
            if old.startswith(key+"."): os.remove(os.path.join(ALARM_SOUNDS_DIR,old))
    except Exception: pass
    with data_lock:
        store=db.get("alarm_cfg",{}) or {}; cur=store.get(key,{}) or {}
        cur.pop("sound_file",None); cur["has_custom"]=False; cur["sound_v"]=int(time.time())
        store[key]=cur; db["alarm_cfg"]=store; save_data(db)
    return jsonify({"ok":True})

@app.route("/api/nowait_config",methods=["POST"])
def api_nowait_config():
    d=request.get_json(silent=True) or {}
    with data_lock:
        if "enabled" in d: db["nowait_enabled"]=bool(d["enabled"])
        if "exclude_delivery" in d: db["nowait_exclude_delivery"]=bool(d["exclude_delivery"])
        if "items" in d and isinstance(d["items"],list):
            db["nowait_items"]=[str(x).strip() for x in d["items"] if str(x).strip()]
        if "wait_items" in d and isinstance(d["wait_items"],list):
            db["wait_items"]=[str(x).strip() for x in d["wait_items"] if str(x).strip()]
        if "alarm_min" in d:
            try: db["nowait_alarm_min"]=max(0,int(d["alarm_min"]))
            except Exception: pass
        if "wait_alarm_min" in d:
            try: db["wait_alarm_min"]=max(0,int(d["wait_alarm_min"]))
            except Exception: pass
        save_data(db)
    return jsonify({"ok":True,"enabled":_nowait_on(),"alarm_min":_nowait_alarm_min(),"wait_alarm_min":_wait_alarm_min(),"items":db.get("nowait_items") or DEFAULT_NOWAIT})

@app.route("/api/wait_done",methods=["POST"])
def api_wait_done():
    d=request.get_json(silent=True) or {}; oid=str(d.get("oid",""))
    _wait_reset()
    if oid:
        WAIT_LIVE["done"].add(oid); WAIT_LIVE["orders"].pop(oid,None)
    return jsonify({"ok":True})

@app.route("/api/wait_ack",methods=["POST"])
def api_wait_ack():
    _wait_reset(); now=time.time()
    for oid,o in list(WAIT_LIVE["orders"].items()):
        if oid in WAIT_LIVE["done"]: continue
        if int((now-o["ts"])/60)>=_thr_for(o.get("kind","now")): WAIT_LIVE["acked"].add(oid)   # only silence the ones alarming
    return jsonify({"ok":True})

@app.route("/photos/<name>")
def serve_photo(name):
    if not re.match(r'^[A-Za-z0-9_.-]+$',name or ""): return Response("bad name",status=400)
    p=os.path.join(PHOTOS_DIR,name)
    if not os.path.exists(p): return Response("not found",status=404)
    return send_file(p,mimetype="image/jpeg")

# ── Rotisserie auto-count: grab a frame from the Tapo camera (RTSP) and count rows of chickens with Gemini ──
ROTCAM={"cooking":0,"hist":[],"last_count":None,"last_ts":0,"error":""}
# --- Gemini usage tracking (rough estimate; Google's Spend page is the source of truth) ---
_GEM_COST_PER_CALL=0.0002   # ~one downscaled image + prompt on gemini-2.5-flash (estimate, USD)
_gem_u=db.get("rotcam_usage") or {}
ROTCAM["calls_today"]=_gem_u.get("calls_today",0); ROTCAM["calls_day"]=_gem_u.get("calls_day","")
ROTCAM["calls_total"]=_gem_u.get("calls_total",0)
def _gem_count_call():
    today=datetime.now().astimezone().date().isoformat()
    if ROTCAM.get("calls_day")!=today: ROTCAM["calls_day"]=today; ROTCAM["calls_today"]=0
    ROTCAM["calls_today"]=ROTCAM.get("calls_today",0)+1
    ROTCAM["calls_total"]=ROTCAM.get("calls_total",0)+1
    if ROTCAM["calls_total"]%10==0:   # persist occasionally so the meter survives restarts
        try:
            with data_lock:
                db["rotcam_usage"]={"calls_today":ROTCAM["calls_today"],"calls_day":ROTCAM["calls_day"],"calls_total":ROTCAM["calls_total"]}
                save_data(db)
        except Exception: pass
def _downscale_jpeg(jpeg,maxw=640):
    # shrink the frame before sending to Gemini — rows are easily countable at low-res, and it cuts cost a lot
    try:
        from PIL import Image; import io
        im=Image.open(io.BytesIO(jpeg)); im.load()
        if im.mode!="RGB": im=im.convert("RGB")
        if im.width>maxw: im=im.resize((maxw,int(im.height*maxw/im.width)))
        out=io.BytesIO(); im.save(out,"JPEG",quality=72); return out.getvalue()
    except Exception:
        return jpeg   # Pillow missing/error → send original (still works, just costs a little more)
def _rotcam_cfg(): return db.get("rotcam_config",{}) or {}
def _rotcam_rtsp():
    cfg=_rotcam_cfg()
    if (cfg.get("rtsp_url") or "").strip(): return cfg["rtsp_url"].strip()
    ip=(cfg.get("ip") or "").strip()
    if not ip: return ""
    user=(cfg.get("user") or "").strip(); pw=(cfg.get("pass") or "").strip()
    stream=(cfg.get("stream") or "stream1").strip()
    auth=(urllib.parse.quote(user)+":"+urllib.parse.quote(pw)+"@") if user else ""
    return "rtsp://%s%s:554/%s"%(auth,ip,stream)
def _rotcam_grab():
    import subprocess
    url=_rotcam_rtsp()
    if not url: return None,"No rotisserie camera configured"
    try:
        p=subprocess.run(["ffmpeg","-rtsp_transport","tcp","-i",url,"-an","-frames:v","1","-q:v","4","-f","image2","-"],
                         capture_output=True,timeout=25)
        if p.returncode!=0 or not p.stdout:
            return None,"Couldn't read the camera stream (check the RTSP/camera-account login, or ffmpeg)."
        return p.stdout,None
    except FileNotFoundError:
        return None,"ffmpeg is not installed on this machine."
    except Exception as e:
        return None,str(e)
_ROT_PROMPT=("This is a vertical rotisserie chicken oven seen at a downward angle through a glass door. It has 6 horizontal "
             "spit levels stacked top to bottom (level 1 = top, level 6 = bottom, just above a stainless-steel bench). "
             "Examine EACH level one at a time, from level 1 down to level 6, and decide whether that level currently has any "
             "chickens on it. Notes for THIS oven: a bright glare/reflection strip along the very top is NOT a level; the two "
             "black door-stop bars near the top-centre are NOT chickens; bare metal spit rods with no chickens are empty; do NOT "
             "treat the stainless bench at the bottom as a level; the top level may be partly cut off at the top edge — if "
             "chickens are visible there it still counts as loaded. Reply with EXACTLY 6 characters and nothing else: one digit "
             "per level from top (level 1) to bottom (level 6), '1' if that level has chickens or '0' if it is empty. "
             "Example: 111100 means levels 1-4 have chickens and levels 5-6 are empty.")
# --- LOCKED box positions for THIS camera (fractions of frame H; x = 21%..81% of W). Boxes get
#     shorter toward the bottom because of the downward angle. Set 2026-06-13 with the owner. ---
_ROT_BOXES=[(0.005,0.155),(0.165,0.300),(0.328,0.453),(0.465,0.580),(0.590,0.695),(0.705,0.795)]
_ROT_BOX_X=(0.21,0.81)
_ROT_BOX_PROMPT=("This image shows 6 horizontal strips stacked top to bottom, each numbered 1-6 (green number, top-left of "
                 "each strip). Every strip is a close-up of ONE shelf of a rotisserie chicken oven: strip 1 = top shelf, "
                 "strip 6 = bottom shelf. For EACH strip decide: does it contain chicken (whole roast chickens / chicken "
                 "meat) = 1, OR is it empty/bare (you see only metal spit rods, a wire grid, glass, or empty dark space, no "
                 "chicken) = 0. Reply with EXACTLY 6 digits and nothing else, one per strip from top (strip 1) to bottom "
                 "(strip 6). Example: 111110 means strips 1-5 have chicken and strip 6 is empty. "
                 "IMPORTANT: if a PERSON or large object is blocking the view of ANY strip so you cannot clearly see the "
                 "shelf behind them, reply with exactly the single word BLOCKED (instead of digits) — do not guess.")
# Combined occupancy + DONENESS read (one call). Each strip → one letter by the chickens' colour, allowing for the
# bright glare off the heating elements. Trained-eye criteria from the owner's labelled on-spit photos.
_ROT_DONE_PROMPT=("This image shows 6 horizontal strips stacked top to bottom, numbered 1-6 (green number, top-left of each "
                  "strip). Each strip is a close-up of ONE shelf of a rotisserie chicken oven (strip 1 = top). There is a "
                  "bright white glare from the heating elements behind the chickens — judge the chickens themselves, not the "
                  "glare. For EACH strip output ONE character for its cooking state by colour:\n"
                  "0 = EMPTY (no chicken: only metal spit rods, wire grid, glass or dark/empty space)\n"
                  "N = NOT READY (raw/early: pale, white or pinkish, glossy skin, little or no browning)\n"
                  "A = ALMOST READY (partly cooked: patchy light-to-medium browning developing, still uneven)\n"
                  "R = READY (cooked: even golden-brown skin across the whole chicken)\n"
                  "O = OVERDONE (very dark brown / mahogany, with charred or blackened patches on the ridges)\n"
                  "Reply with EXACTLY 6 characters, one per strip from top (strip 1) to bottom (strip 6), each being one of "
                  "0 N A R O, and NOTHING else. Example: RRANO0 (strip1 ready, strip2 ready, strip3 almost, strip4 not-ready, "
                  "strip5 overdone, strip6 empty). If a PERSON or object blocks a strip so you cannot see it, reply with the "
                  "single word BLOCKED.")
_DONE_LABELS={"N":"not_ready","A":"almost_ready","R":"ready","O":"overdone"}
def _rotcam_save_crops(jpeg,donepat):
    # auto-build the on-spit training set: save each loaded row's strip into rotcam_dataset/<class>/, throttled per
    # class so the rare-but-important READY/OVERDONE shots get collected without drowning in NOT_READY frames.
    if not jpeg or not donepat or len(donepat)!=6: return
    try:
        from PIL import Image; import io
        now=time.time(); seen=ROTCAM.setdefault("crop_ts",{})
        im=None; x1=x2=None
        for i,(a,b) in enumerate(_ROT_BOXES):
            cls=_DONE_LABELS.get(donepat[i].upper())
            if not cls: continue
            gap=60 if cls in ("ready","overdone") else 240   # collect ready/overdone every 1 min, others every 4 min
            if now-seen.get(cls,0)<gap: continue
            if im is None:
                im=Image.open(io.BytesIO(jpeg)).convert("RGB"); W,H=im.size
                x1,x2=int(W*_ROT_BOX_X[0]),int(W*_ROT_BOX_X[1])
            seen[cls]=now
            d=os.path.join(BASE_DIR,"rotcam_dataset",cls); os.makedirs(d,exist_ok=True)
            im.crop((x1,int(im.height*a),x2,int(im.height*b))).save(os.path.join(d,"row%d_%d.jpg"%(i+1,int(now))),"JPEG",quality=82)
    except Exception: pass
def _rotcam_boxes_composite(jpeg):
    # crop the 6 locked shelf boxes and stack them into one labelled image — isolates each shelf
    # (no glare above, no bench below) so the AI judges 'chicken vs bare' per shelf. Returns None if Pillow missing.
    try:
        from PIL import Image, ImageDraw; import io
        im=Image.open(io.BytesIO(jpeg)).convert("RGB"); W,H=im.size
        x1,x2=int(W*_ROT_BOX_X[0]),int(W*_ROT_BOX_X[1]); cw=640; gap=6
        strips=[]
        for a,b in _ROT_BOXES:
            c=im.crop((x1,int(H*a),x2,int(H*b)))
            c=c.resize((cw,max(1,int(c.height*cw/c.width))))
            strips.append(c)
        comp=Image.new("RGB",(cw,sum(s.height for s in strips)+gap*(len(strips)+1)),(15,15,15))
        dr=ImageDraw.Draw(comp); y=gap
        for i,s in enumerate(strips,1):
            comp.paste(s,(0,y)); dr.rectangle([0,y,cw-1,y+s.height-1],outline=(0,255,0),width=2)
            dr.text((5,y+3),str(i),fill=(0,255,0)); y+=s.height+gap
        out=io.BytesIO(); comp.save(out,"JPEG",quality=78); return out.getvalue()
    except Exception:
        return None
def _rotcam_count(jpeg):
    cfg=_rotcam_cfg(); key=(cfg.get("gemini_key") or "").strip()
    if not key: return None,"No Gemini API key configured"
    _gem_count_call()                 # track usage/cost
    comp=_rotcam_boxes_composite(jpeg)
    done_mode=bool(cfg.get("doneness_enabled")) and comp is not None   # read colour/doneness per row in the same call
    if comp is not None:
        img=comp; prompt=(_ROT_DONE_PROMPT if done_mode else (cfg.get("prompt") or _ROT_BOX_PROMPT))   # per-box strips (preferred)
        ROTCAM["last_comp"]="data:image/jpeg;base64,"+__import__("base64").b64encode(comp).decode()
    else:
        img=_downscale_jpeg(jpeg); prompt=cfg.get("prompt") or _ROT_PROMPT   # fallback: whole frame
    model=(cfg.get("model") or "gemini-2.5-flash").strip()
    gencfg={"temperature":0,"maxOutputTokens":64}
    if "2.5" in model or "thinking" in model.lower():
        gencfg["thinkingConfig"]={"thinkingBudget":0}   # 2.5 is a "thinking" model — skip reasoning, just answer
    body={"contents":[{"parts":[{"text":prompt},
          {"inline_data":{"mime_type":"image/jpeg","data":__import__("base64").b64encode(img).decode()}}]}],
          "generationConfig":gencfg}
    url="https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"%(model,urllib.parse.quote(key))
    try:
        req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=30,context=SSL_CTX) as r: data=json.loads(r.read().decode())
        cand=(data.get("candidates") or [{}])[0]
        parts=((cand.get("content") or {}).get("parts")) or []
        txt="".join(p.get("text","") for p in parts if isinstance(p,dict))
        if not txt: return None,"Gemini returned no answer (finish: %s)"%cand.get("finishReason","?")
        if done_mode:                                # combined occupancy+doneness: 6 chars of 0/N/A/R/O
            dm=re.search(r"[0NAROnaro]{6}",txt)
            if "block" in txt.lower() and not dm: return None,"view blocked (skipped)"
            if dm:
                done=dm.group().upper(); ROTCAM["done"]=done
                ROTCAM["levels"]="".join("0" if c=="0" else "1" for c in done)   # derive occupancy from doneness
                _rotcam_save_crops(jpeg,done)        # auto-collect on-spit training crops, labelled by doneness
                return ROTCAM["levels"].count("1"),None
            # no doneness pattern parsed → fall through to plain occupancy parsing below
        mm=re.search(r"[01]{6}",txt)                 # per-level pattern e.g. 111100 → count the loaded levels
        if "block" in txt.lower() and not mm:        # someone standing in front → skip this read, don't touch stock
            return None,"view blocked (skipped)"
        if mm:
            ROTCAM["levels"]=mm.group()
            return mm.group().count("1"),None
        m=re.search(r"\d+",txt)                       # fallback: a plain count
        if not m: return None,"Gemini reply had no count: "+txt[:40]
        return int(m.group()),None
    except Exception as e:
        rd=getattr(e,"read",None)
        if rd:
            try: return None,"Gemini error: "+e.read().decode()[:140]
            except Exception: pass
        return None,str(e)
# ===== UNLOADING-BENCH WATCHER =====================================================
# Cooked rows are ALWAYS placed on the stainless bench (bottom of frame) before going to
# the warmer. Counting loaded shelves can't catch removals (staff slide rows up + stand in
# front), but the bench always shows the cooked row. So: watch the bench; when a fresh row
# of cooked chickens appears (confirmed over 2 reads) → +birds_per_row to available. The
# bench clearing (moved to warmer) is ignored — those birds are still available stock.
_BENCH_BOX=(0.0,1.0,0.78,1.0)   # FULL bottom strip — bench is on wheels & shifts left/right/partial; top kept just below the oven so the bottom shelf isn't counted
_BENCH_PROMPT=("This is the bottom strip of a rotisserie chicken shop, below the oven. It shows the stainless "
               "unloading bench, which is on WHEELS and may be shifted left, right, centre, or only partly in "
               "view. A 'row' is a group of roasted chickens (about 4) placed together when pulled off the spit. "
               "How many rows of cooked, GOLDEN/BROWN WHOLE CHICKENS are resting on the bench right now? "
               "IMPORTANT: ignore bare steel, floor tiles, metal spikes, wire frames/cages, tongs, trays, the "
               "fryer baskets (often on the right), and any PEOPLE (heads, bodies, arms, gloved hands) — count "
               "ONLY whole, intact, rounded roasted chickens sitting on the bench surface, wherever along the "
               "strip they are. Do NOT count BUTTERFLIED / flattened / spatchcocked / split-open / halved or "
               "quartered chickens — those are a different product and must be ignored; count only plump WHOLE "
               "birds. If the bench is rolled out of view or completely blocked, reply the single word BLOCKED. "
               "Otherwise reply ONLY one digit: 0, 1, 2 or 3.")
def _rotcam_bench_crop(jpeg):
    try:
        from PIL import Image; import io
        im=Image.open(io.BytesIO(jpeg)).convert("RGB"); W,H=im.size
        x1,x2,y1,y2=_BENCH_BOX
        c=im.crop((int(W*x1),int(H*y1),int(W*x2),int(H*y2)))
        if c.width>720: c=c.resize((720,max(1,int(c.height*720/c.width))))
        out=io.BytesIO(); c.save(out,"JPEG",quality=78); return out.getvalue()
    except Exception:
        return None
def _rotcam_bench_count(jpeg):
    cfg=_rotcam_cfg(); key=(cfg.get("gemini_key") or "").strip()
    if not key: return None
    crop=_rotcam_bench_crop(jpeg)
    if crop is None: return None
    _gem_count_call()
    ROTCAM["bench_comp"]="data:image/jpeg;base64,"+__import__("base64").b64encode(crop).decode()
    model=(cfg.get("model") or "gemini-2.5-flash").strip()
    gencfg={"temperature":0,"maxOutputTokens":32}
    if "2.5" in model or "thinking" in model.lower(): gencfg["thinkingConfig"]={"thinkingBudget":0}
    body={"contents":[{"parts":[{"text":_BENCH_PROMPT},
          {"inline_data":{"mime_type":"image/jpeg","data":__import__("base64").b64encode(crop).decode()}}]}],
          "generationConfig":gencfg}
    url="https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"%(model,urllib.parse.quote(key))
    try:
        req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=30,context=SSL_CTX) as r: data=json.loads(r.read().decode())
        cand=(data.get("candidates") or [{}])[0]
        parts=((cand.get("content") or {}).get("parts")) or []
        txt="".join(p.get("text","") for p in parts if isinstance(p,dict))
        if not txt: return None
        if "block" in txt.lower() and not re.search(r"\d",txt): return None   # bench fully hidden → skip
        m=re.search(r"\d",txt)
        if not m: return None
        return max(0,min(3,int(m.group())))
    except Exception:
        return None
_ROW_WINDOW=180   # seconds to pair a "row left the spit" (front cam) with a "new row on the bench" (side cam)
def _try_credit():
    # The COUNT comes from the ROTISSERIE (each shelf-row that leaves the spit = one row of birds).
    # The BENCH only CONFIRMS a chicken actually landed recently — it never decides the NUMBER (it sees
    # individual chickens, not clean rows, so counting bench items over-counts). So: a rotisserie misread
    # alone can't add stock (needs bench confirmation), and bench fluctuation alone can't add stock (needs
    # a real shelf-row to have left).
    off=ROTCAM.get("off_credits",0)
    bench_ok=(time.time()-ROTCAM.get("rise_ts",0))<_ROW_WINDOW and ROTCAM.get("bench_rises",0)>0
    if off>0 and bench_ok:
        rot_put_on(off)                                     # +birds_per_row per row the rotisserie saw leave
        ROTCAM["off_credits"]=0; ROTCAM["bench_rises"]=0    # consume both sides
def _note_row_off(k):
    # front camera saw k shelf-rows go empty → k rows came off the spit (await bench confirmation)
    if k<=0: return
    now=time.time()
    if now-ROTCAM.get("off_ts",0)>_ROW_WINDOW: ROTCAM["off_credits"]=0   # stale → reset
    ROTCAM["off_credits"]=ROTCAM.get("off_credits",0)+k; ROTCAM["off_ts"]=now
    _try_credit()
def _rotcam_bench_apply(n):
    if n is None: return                                   # blocked/error → hold, don't touch stock
    # DEBOUNCE: the bench is on wheels — a frame caught mid-move, motion-blurred, or with the bench
    # half out of view can misread for ONE read. Require two CONSECUTIVE equal reads before we trust a
    # count, so a transient blip never moves stock.
    last=ROTCAM.get("bench_raw_last"); ROTCAM["bench_raw_last"]=n
    if n!=last: return                                     # not confirmed yet — wait for the next read to agree
    settled=ROTCAM.get("bench_rows",0)
    if n==settled: return                                  # confirmed, but no change
    if isinstance(n,int) and n>settled:                    # confirmed NEW row(s) on the bench
        now=time.time()
        if now-ROTCAM.get("rise_ts",0)>_ROW_WINDOW: ROTCAM["bench_rises"]=0
        ROTCAM["bench_rises"]=ROTCAM.get("bench_rises",0)+(n-settled); ROTCAM["rise_ts"]=now
        ROTCAM["force_shelf"]=True   # bench saw a row land → check the spit IMMEDIATELY (don't wait for the timer)
        _try_credit()    # credits stock ONLY if the rotisserie cam recently saw a row come off (else: a warmer
                         # chicken being cut on the bench — NOT new stock — so it's ignored)
    ROTCAM["bench_rows"]=n                                  # update the settled count (rises and falls)
# --- DEDICATED SIDE-ANGLE BENCH CAMERA (2nd Tapo) -----------------------------------
# A second camera mounted to the side keeps the unloading bench in full view wherever it
# rolls, and can see rows placed side-by-side or stacked. When configured, the bench watcher
# pulls from THIS camera (whole frame, no crop) instead of cropping the front camera.
def _bench_rtsp():
    cfg=_rotcam_cfg()
    ip=(cfg.get("bench_ip") or "").strip()
    if ip:    # structured fields take priority — password gets URL-encoded (handles special chars ffmpeg chokes on)
        user=(cfg.get("bench_user") or "").strip(); pw=(cfg.get("bench_pass") or "").strip()
        stream=(cfg.get("bench_stream") or "stream1").strip()
        auth=(urllib.parse.quote(user,safe="")+":"+urllib.parse.quote(pw,safe="")+"@") if user else ""
        return "rtsp://%s%s:554/%s"%(auth,ip,stream)
    raw=(cfg.get("bench_rtsp_url") or "").strip()
    if raw:
        try:    # re-encode the embedded login so ffmpeg parses it like VLC does (handles special chars)
            from urllib.parse import urlsplit
            p=urlsplit(raw)
            if p.hostname and p.username is not None:
                host=p.hostname+(":%d"%p.port if p.port else "")
                q=("?"+p.query) if p.query else ""
                return "rtsp://%s:%s@%s%s%s"%(urllib.parse.quote(p.username,safe=""),
                                              urllib.parse.quote(p.password or "",safe=""),host,p.path or "",q)
        except Exception: pass
        return raw
    return ""
def _bench_grab():
    import subprocess
    url=_bench_rtsp()
    if not url: return None,"No bench camera configured"
    try:
        p=subprocess.run(["ffmpeg","-rtsp_transport","tcp","-i",url,"-an","-frames:v","1","-q:v","4","-f","image2","-"],
                         capture_output=True,timeout=25)
        if p.returncode!=0 or not p.stdout:
            err=(p.stderr or b"").decode("latin1","ignore")
            hint=""
            low=err.lower()
            if "401" in err or "unauthor" in low: hint=" — 401 Unauthorized: wrong RTSP login. Set a Camera Account in the Tapo app (Settings → Advanced) and use that user/pass."
            elif "404" in err or "not found" in low: hint=" — stream path not found: try /stream2 instead of /stream1."
            elif "timed out" in low or "timeout" in low: hint=" — connection timed out."
            tail=err.strip().splitlines()[-1] if err.strip() else ""
            return None,"Couldn't read the bench camera"+hint+((" ["+tail[:120]+"]") if tail else "")
        return p.stdout,None
    except FileNotFoundError: return None,"ffmpeg is not installed."
    except Exception as e: return None,str(e)
_BENCH_DETECT_PROMPT=("This is a SIDE-ANGLE view of the wheeled stainless-steel UNLOADING BENCH in a rotisserie chicken shop, where "
                    "cooked chickens are placed after coming off the spit. The bench is ON WHEELS and may be parked in front of "
                    "the rotisserie (left), in the middle, or rolled in front of the glass warming cabinet (right) — find the flat "
                    "open stainless bench wherever it is. DETECT each individual cooked, GOLDEN/BROWN WHOLE roasted CHICKEN "
                    "resting ON TOP of the flat open bench surface — put ONE box around EACH whole chicken (not around groups). "
                    "CRITICAL: only chickens sitting DIRECTLY on the open stainless bench top. Do NOT box chickens inside or on the "
                    "HOT-HOLDING / DISPLAY / WARMER CABINET (the glass-fronted cabinet along one side, holding chickens behind "
                    "glass on wire racks) — those are display stock, ignore them entirely. Also ignore bare steel, trays, tongs, "
                    "wire frames, fryer baskets, people, and BUTTERFLIED/flattened/halved/quartered chickens — only plump WHOLE "
                    "birds. ALSO do NOT box a chicken being CUT / CARVED / PORTIONED / SERVED: if a knife, cleaver, tongs or hands "
                    "are on or over it, or it is split open / on a chopping board, IGNORE it. Return ONLY a JSON array, one object "
                    "per whole chicken, each as {\"box_2d\":[ymin,xmin,ymax,xmax]} with integer coordinates normalised 0-1000 "
                    "(origin top-left). If there are no whole chickens on the open bench, return [].")
_BENCH_ZONE=(0.08,0.15,0.53,0.96)   # x1,y1,x2,y2 fraction — the unloading bench area on the LEFT (cuts off before the warmer cabinet on the right). Tunable via rotcam_config['bench_zone'].
def _bench_zone():
    z=_rotcam_cfg().get("bench_zone")
    if isinstance(z,(list,tuple)) and len(z)==4:
        try:
            v=[float(x) for x in z]
            if all(0<=x<=1 for x in v) and v[0]<v[2] and v[1]<v[3]: return (v[0],v[1],v[2],v[3])
        except Exception: pass
    return _BENCH_ZONE
def _bench_annotate(jpeg,boxes):
    # draw the detection ZONE (cyan) + a green box per detected row, on the given frame
    try:
        from PIL import Image, ImageDraw; import io
        im=Image.open(io.BytesIO(jpeg)).convert("RGB"); W,H=im.size
        dr=ImageDraw.Draw(im)
        zx1,zy1,zx2,zy2=_bench_zone()
        dr.rectangle([int(W*zx1),int(H*zy1),int(W*zx2),int(H*zy2)],outline=(0,190,255),width=2)
        for i,b in enumerate(boxes,1):
            try:
                ymin,xmin,ymax,xmax=b[0],b[1],b[2],b[3]
                x1=int(min(xmin,xmax)/1000.0*W); y1=int(min(ymin,ymax)/1000.0*H)
                x2=int(max(xmin,xmax)/1000.0*W); y2=int(max(ymin,ymax)/1000.0*H)
                dr.rectangle([x1,y1,x2,y2],outline=(0,255,0),width=3)
                dr.text((x1+4,max(0,y1+3)),"chicken %d"%i,fill=(0,255,0))
            except Exception: pass
        out=io.BytesIO(); im.save(out,"JPEG",quality=82); return out.getvalue()
    except Exception:
        return jpeg
def _benchcam_count(jpeg):
    cfg=_rotcam_cfg(); key=(cfg.get("gemini_key") or "").strip()
    if not key or not jpeg: return None
    import base64 as _b
    img=_downscale_jpeg(jpeg,1100)                 # keep detail — we send only a cropped sub-region
    zx1,zy1,zx2,zy2=_bench_zone()
    crop_jpeg=img; cropped=False
    try:                                           # crop to the bench zone so the AI never sees the cabinet/counter
        from PIL import Image; import io
        full=Image.open(io.BytesIO(img)).convert("RGB"); W,H=full.size
        crop=full.crop((int(W*zx1),int(H*zy1),int(W*zx2),int(H*zy2)))
        if crop.width>760: crop=crop.resize((760,max(1,int(crop.height*760/crop.width))))
        cb=io.BytesIO(); crop.save(cb,"JPEG",quality=82); crop_jpeg=cb.getvalue(); cropped=True
    except Exception: cropped=False
    _gem_count_call()
    model=(cfg.get("model") or "gemini-2.5-flash").strip()
    gencfg={"temperature":0,"maxOutputTokens":700}
    if "2.5" in model or "thinking" in model.lower(): gencfg["thinkingConfig"]={"thinkingBudget":0}
    body={"contents":[{"parts":[{"text":_BENCH_DETECT_PROMPT},
          {"inline_data":{"mime_type":"image/jpeg","data":_b.b64encode(crop_jpeg).decode()}}]}],
          "generationConfig":gencfg}
    url="https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"%(model,urllib.parse.quote(key))
    raw=[]
    try:
        req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=30,context=SSL_CTX) as r: data=json.loads(r.read().decode())
        cand=(data.get("candidates") or [{}])[0]
        txt="".join(p.get("text","") for p in (((cand.get("content") or {}).get("parts")) or []) if isinstance(p,dict))
        if not txt: return None
        mt=re.search(r"\[.*\]",txt,re.S)
        if mt:
            for o in (json.loads(mt.group()) or []):
                bb=o.get("box_2d") if isinstance(o,dict) else (o if isinstance(o,list) else None)
                if bb and len(bb)>=4:
                    try: raw.append([float(bb[0]),float(bb[1]),float(bb[2]),float(bb[3])])
                    except Exception: pass
    except Exception:
        return None
    boxes=[]                                       # remap crop-relative coords back to the FULL frame
    for ymin,xmin,ymax,xmax in raw[:8]:
        if cropped:
            boxes.append([ (zy1+(ymin/1000.0)*(zy2-zy1))*1000, (zx1+(xmin/1000.0)*(zx2-zx1))*1000,
                           (zy1+(ymax/1000.0)*(zy2-zy1))*1000, (zx1+(xmax/1000.0)*(zx2-zx1))*1000 ])
        else:
            boxes.append([ymin,xmin,ymax,xmax])
    ROTCAM["bench_boxes"]=boxes; ROTCAM["bench_boxes_ts"]=time.time()
    annotated=_bench_annotate(img,boxes)
    ROTCAM["bench_frame"]=annotated; ROTCAM["bench_frame_ts"]=time.time()
    ROTCAM["bench_comp"]="data:image/jpeg;base64,"+_b.b64encode(annotated).decode()
    return len(boxes)
# ===================================================================================
def _rotcam_read():
    fr=ROTCAM.get("frame")                                   # reuse the live-feed frame if fresh
    if fr and (time.time()-ROTCAM.get("frame_ts",0))<8:      # (avoids a 2nd camera connection)
        jpeg=fr
    else:
        jpeg,err=_rotcam_grab()
        if err: return None,None,err
    rows,err=_rotcam_count(jpeg)
    return rows,jpeg,err
def _rotcam_apply(rows):
    ROTCAM["last_count"]=rows; ROTCAM["last_ts"]=time.time()
    pat=ROTCAM.get("levels","")
    if not (isinstance(pat,str) and len(pat)==6 and set(pat)<=set("01")):
        # no valid per-shelf pattern → fall back to count-based smoothing
        h=ROTCAM["hist"]; h.append(rows); ROTCAM["hist"]=h[-4:]
        if len(h)>=2 and h[-1]==h[-2]:
            confirmed=h[-1]; prev=ROTCAM["cooking"]
            if confirmed!=prev:
                if confirmed<prev: rot_put_on(prev-confirmed)   # count dropped → row(s) came off the spit → credit straight to available
                ROTCAM["cooking"]=confirmed
        return
    # per-shelf: confirm the pattern over two reads (ignore one-off misreads), then act on shelves
    # that went loaded(1)→empty(0) — each one is a cooked row pulled → +birds_per_row to available.
    ph=ROTCAM.get("pat_hist",[]); ph.append(pat); ROTCAM["pat_hist"]=ph[-3:]
    if len(ph)>=2 and ph[-1]==ph[-2]:
        confirmed=ph[-1]; prev=ROTCAM.get("cooking_pat","")
        if confirmed!=prev:
            if prev and len(prev)==6:
                came_off=sum(1 for i in range(6) if prev[i]=="1" and confirmed[i]=="0")
                if came_off>0: rot_put_on(came_off)   # shelf went loaded→empty → cooked row(s) pulled off the spit → credit straight to available (rotisserie alone; bench cam is just the live view)
            ROTCAM["cooking_pat"]=confirmed
            ROTCAM["cooking"]=confirmed.count("1")
def _hm_to_min(s,d):
    try: h,m=str(s).split(":"); return int(h)*60+int(m)
    except Exception: return d
def rotcam_loop():
    while True:
        cfg=_rotcam_cfg()
        try: iv=max(3,int(cfg.get("interval",120) or 120))            # shelf-count cadence (rows-cooking display)
        except Exception: iv=120
        try: bench_iv=max(3,int(cfg.get("bench_interval",6) or 6))     # bench cadence (faster → catches each row as it lands)
        except Exception: bench_iv=6
        nowt=datetime.now().astimezone(); mins=nowt.hour*60+nowt.minute
        a=_hm_to_min(cfg.get("active_start"),595)   # default 09:55
        b=_hm_to_min(cfg.get("active_end"),1200)    # default 20:00
        open_now=a<=mins<b   # only count during the active window → controls cost
        if cfg.get("enabled") and open_now and (cfg.get("gemini_key") or "").strip() and _rotcam_rtsp():
            try:
                jpeg=ROTCAM.get("frame")                                # reuse the live-feed frame if fresh, else grab one
                if not (jpeg and (time.time()-ROTCAM.get("frame_ts",0))<8):
                    jpeg,gerr=_rotcam_grab()
                    if gerr:
                        ROTCAM["error"]=gerr; jpeg=None
                        if "429" in gerr or "quota" in gerr.lower(): time.sleep(900); continue
                if jpeg:
                    ROTCAM["error"]=""
                    if cfg.get("bench_enabled",False):                  # BENCH auto-count
                        try:
                            if _bench_rtsp():                           # dedicated side-angle bench camera (always sees the bench)
                                # Tapo allows ONE connection. Reuse the live-feed stream frame if it's fresh
                                # (a viewer is watching); only open our own grab when the feed stream is idle.
                                bframe=ROTCAM.get("bench_live") if (ROTCAM.get("bench_live") and (time.time()-ROTCAM.get("bench_live_ts",0))<8) else None
                                watching=(time.time()-ROTCAM.get("bench_want",0))<20
                                if bframe is None and not watching:
                                    bframe,berr=_bench_grab()
                                    if berr: ROTCAM["bench_error"]=berr
                                if bframe: _rotcam_bench_apply(_benchcam_count(bframe))
                            else:                                       # fallback: crop the bottom of the front camera frame
                                _rotcam_bench_apply(_rotcam_bench_count(jpeg))
                        except Exception: pass
                    if ROTCAM.pop("force_shelf",False) or time.time()-ROTCAM.get("last_shelf_ts",0)>=iv:   # SHELF count: on the timer, OR immediately when the bench just saw a row land
                        ROTCAM["last_shelf_ts"]=time.time()
                        rows,cerr=_rotcam_count(jpeg)
                        if cerr and ("429" in cerr or "quota" in cerr.lower()): time.sleep(900); continue
                        if not cerr: _rotcam_apply(rows)
            except Exception as e: ROTCAM["error"]=str(e)
        time.sleep(bench_iv)

@app.route("/api/rotcam_config",methods=["POST"])
def api_rotcam_config():
    d=request.get_json(silent=True) or {}; cfg=db.get("rotcam_config",{}) or {}
    for k in ("ip","user","pass","rtsp_url","stream","gemini_key","model","prompt","active_start","active_end",
              "bench_ip","bench_user","bench_pass","bench_rtsp_url","bench_stream"):
        if k in d: cfg[k]=str(d[k]).strip()
    if "interval" in d:
        try: cfg["interval"]=max(3,int(d["interval"]))
        except Exception: pass
    if "bench_interval" in d:
        try: cfg["bench_interval"]=max(3,int(d["bench_interval"]))
        except Exception: pass
    if "bench_zone" in d and isinstance(d["bench_zone"],(list,tuple)) and len(d["bench_zone"])==4:
        try: cfg["bench_zone"]=[max(0.0,min(1.0,float(x))) for x in d["bench_zone"]]
        except Exception: pass
    for bk in ("enabled","feed_enabled","spin_enabled","doneness_enabled","bench_enabled"):
        if bk in d: cfg[bk]=bool(d[bk])
    with data_lock: db["rotcam_config"]=cfg; save_data(db)
    return jsonify({"ok":True,"enabled":bool(cfg.get("enabled") and cfg.get("gemini_key") and _rotcam_rtsp())})

@app.route("/api/rotcam_test",methods=["POST"])
def api_rotcam_test():
    rows,jpeg,err=_rotcam_read()
    out={"ok":err is None,"rows":rows,"levels":ROTCAM.get("levels","")}
    if err: out["error"]=err
    if ROTCAM.get("last_comp"): out["preview"]=ROTCAM["last_comp"]   # show the cropped per-shelf strips
    elif jpeg:
        import base64 as _b
        out["preview"]="data:image/jpeg;base64,"+_b.b64encode(jpeg).decode()
    if jpeg and not _bench_rtsp():                                    # front-crop bench read (only if no dedicated bench cam)
        try: out["bench_rows"]=_rotcam_bench_count(jpeg)
        except Exception: out["bench_rows"]=None
        if ROTCAM.get("bench_comp"): out["bench_preview"]=ROTCAM["bench_comp"]
    return jsonify(out)

@app.route("/api/benchcam_test",methods=["POST"])
def api_benchcam_test():
    if not _bench_rtsp(): return jsonify({"ok":False,"error":"No bench camera configured (add its RTSP/IP first)."})
    jpeg=ROTCAM.get("bench_live") if (ROTCAM.get("bench_live") and (time.time()-ROTCAM.get("bench_live_ts",0))<8) else None
    if jpeg is None:                                   # reuse the live-feed frame if running (Tapo = 1 connection)
        jpeg,err=_bench_grab()
        if err: return jsonify({"ok":False,"error":err})
    rows=_benchcam_count(jpeg)
    out={"ok":True,"bench_rows":rows}
    if ROTCAM.get("bench_comp"): out["preview"]=ROTCAM["bench_comp"]
    else:
        import base64 as _b; out["preview"]="data:image/jpeg;base64,"+_b.b64encode(_downscale_jpeg(jpeg,720)).decode()
    return jsonify(out)

# --- live feed: one persistent ffmpeg pulls MJPEG and keeps the latest frame ready, so
#     requests are served instantly (no ~2s per-grab connect) → near-live, not laggy.
#     The stream only runs while a viewer is actually watching (last_want kept fresh by polling). ---
def rotcam_stream_loop():
    import subprocess
    while True:
        if (time.time()-ROTCAM.get("last_want",0))>20 or not _rotcam_rtsp():
            time.sleep(1); continue   # nobody watching → don't burn the camera/CPU
        url=_rotcam_rtsp(); p=None
        try:
            p=subprocess.Popen(["ffmpeg","-nostdin","-rtsp_transport","tcp","-i",url,
                                "-an","-r","8","-q:v","6","-f","mjpeg","-"],
                               stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=10**7)
            ROTCAM["error"]=""; buf=b""
            while True:
                if (time.time()-ROTCAM.get("last_want",0))>20: break   # viewer left → stop
                chunk=p.stdout.read(65536)
                if not chunk: break
                buf+=chunk
                # drain every COMPLETE jpeg (SOI..EOI) in order, keep the latest as the live frame.
                # (must scan front-to-back: a partial next frame always trails the last full one.)
                while True:
                    s=buf.find(b"\xff\xd8")
                    if s<0: break
                    e=buf.find(b"\xff\xd9", s+2)
                    if e<0: break
                    ROTCAM["frame"]=buf[s:e+2]; ROTCAM["frame_ts"]=time.time(); buf=buf[e+2:]
                if len(buf)>4*10**6: buf=buf[-10**6:]
        except FileNotFoundError:
            ROTCAM["error"]="ffmpeg is not installed on this machine."; time.sleep(5)
        except Exception as ex:
            ROTCAM["error"]=str(ex)
        finally:
            if p:
                try: p.kill()
                except Exception: pass
        time.sleep(0.5)

def benchcam_stream_loop():
    # same persistent-MJPEG trick as the rotisserie cam, for the side bench camera → smooth, no per-grab lag
    import subprocess
    while True:
        if (time.time()-ROTCAM.get("bench_want",0))>20 or not _bench_rtsp():
            time.sleep(1); continue
        url=_bench_rtsp(); p=None
        try:
            p=subprocess.Popen(["ffmpeg","-nostdin","-rtsp_transport","tcp","-i",url,
                                "-an","-r","8","-q:v","6","-f","mjpeg","-"],
                               stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=10**7)
            buf=b""
            while True:
                if (time.time()-ROTCAM.get("bench_want",0))>20: break
                chunk=p.stdout.read(65536)
                if not chunk: break
                buf+=chunk
                got=False
                while True:
                    s=buf.find(b"\xff\xd8")
                    if s<0: break
                    e=buf.find(b"\xff\xd9", s+2)
                    if e<0: break
                    ROTCAM["bench_live"]=buf[s:e+2]; ROTCAM["bench_live_ts"]=time.time(); buf=buf[e+2:]; got=True
                if len(buf)>4*10**6: buf=buf[-10**6:]
                # annotate ONCE here (throttled, shared by all viewers) instead of per-frame per-viewer in the
                # stream generator — re-encoding every frame per connection was pinning the GIL → whole app laggy.
                nowr=time.time()
                if got and (nowr-ROTCAM.get("bench_render_ts",0))>=0.22:
                    ROTCAM["bench_render"]=_bench_render(ROTCAM.get("bench_live")); ROTCAM["bench_render_ts"]=nowr
        except Exception: time.sleep(5)
        finally:
            if p:
                try: p.kill()
                except Exception: pass
        time.sleep(0.5)

@app.route("/api/rotcam_stream")
def api_rotcam_stream():
    # smooth MJPEG video (multipart/x-mixed-replace) — the browser <img> plays it like a webcam,
    # no per-frame reloads, so it looks like live TV instead of a stuttering snapshot.
    if not _rotcam_rtsp(): return Response("camera not configured",status=404)
    def gen():
        last_ts=0; start=time.time()
        while True:
            ROTCAM["last_want"]=time.time()           # keep the background puller alive while watched
            fr=ROTCAM.get("frame"); ts=ROTCAM.get("frame_ts",0)
            if fr and ts!=last_ts:
                last_ts=ts
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+str(len(fr)).encode()+b"\r\n\r\n"+fr+b"\r\n"
            else:
                time.sleep(0.04)
            if not ROTCAM.get("frame") and (time.time()-start)>10: break   # camera never produced a frame → end
    return Response(gen(),mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control":"no-store","Connection":"close"})

@app.route("/api/rotcam_frame")
def api_rotcam_frame():
    if not _rotcam_rtsp(): return Response("camera not configured",status=404)
    ROTCAM["last_want"]=time.time()   # tell the streamer a viewer is here
    for _ in range(70):               # wait up to ~7s for the first frame (stream spinning up)
        fr=ROTCAM.get("frame")
        if fr and (time.time()-ROTCAM.get("frame_ts",0))<8:
            return Response(fr,mimetype="image/jpeg",headers={"Cache-Control":"no-store"})
        time.sleep(0.1)
    jpeg,err=_rotcam_grab()           # fallback: one-shot grab if the stream hasn't produced a frame
    if err: return Response(err,status=503)
    ROTCAM["frame"]=jpeg; ROTCAM["frame_ts"]=time.time()
    return Response(jpeg,mimetype="image/jpeg",headers={"Cache-Control":"no-store"})

def _bench_render(jpeg,maxw=720):
    # one PIL pass: downscale + draw the zone + the latest detection boxes (for the live feed)
    try:
        from PIL import Image, ImageDraw; import io
        im=Image.open(io.BytesIO(jpeg)).convert("RGB")
        if im.width>maxw: im=im.resize((maxw,max(1,int(im.height*maxw/im.width))))
        W,H=im.size; dr=ImageDraw.Draw(im)
        zx1,zy1,zx2,zy2=_bench_zone()
        dr.rectangle([int(W*zx1),int(H*zy1),int(W*zx2),int(H*zy2)],outline=(0,190,255),width=2)
        boxes=ROTCAM.get("bench_boxes") or []
        if boxes and (time.time()-ROTCAM.get("bench_boxes_ts",0))<30:
            for i,b in enumerate(boxes,1):
                try:
                    ymin,xmin,ymax,xmax=b[0],b[1],b[2],b[3]
                    x1=int(min(xmin,xmax)/1000.0*W); y1=int(min(ymin,ymax)/1000.0*H)
                    x2=int(max(xmin,xmax)/1000.0*W); y2=int(max(ymin,ymax)/1000.0*H)
                    dr.rectangle([x1,y1,x2,y2],outline=(0,255,0),width=3)
                    dr.text((x1+4,max(0,y1+3)),"chicken %d"%i,fill=(0,255,0))
                except Exception: pass
        out=io.BytesIO(); im.save(out,"JPEG",quality=72); return out.getvalue()
    except Exception:
        return jpeg
@app.route("/api/benchcam_stream")
def api_benchcam_stream():
    # smooth MJPEG for the bench (like the rotisserie) — zone + boxes drawn on each frame
    if not _bench_rtsp(): return Response("bench camera not configured",status=404)
    def gen():
        last_ts=0; start=time.time()
        while True:
            ROTCAM["bench_want"]=time.time()
            img=ROTCAM.get("bench_render"); ts=ROTCAM.get("bench_render_ts",0)
            now=time.time()
            if img and ts!=last_ts:    # serve the pre-rendered frame — NO PIL here (rendering happens once in the puller loop)
                last_ts=ts
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "+str(len(img)).encode()+b"\r\n\r\n"+img+b"\r\n"
            else:
                time.sleep(0.04)
            if not ROTCAM.get("bench_live") and (now-start)>10: break
    return Response(gen(),mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control":"no-store","Connection":"close"})

@app.route("/api/benchcam_frame")
def api_benchcam_frame():
    if not _bench_rtsp(): return Response("bench camera not configured",status=404)
    ROTCAM["bench_want"]=time.time()                              # tell the bench streamer a viewer is here
    for _ in range(50):                                          # wait up to ~5s for the first rendered frame
        rnd=ROTCAM.get("bench_render")
        if rnd and (time.time()-ROTCAM.get("bench_render_ts",0))<8:
            return Response(rnd,mimetype="image/jpeg",headers={"Cache-Control":"no-store"})  # reuse the shared render — no per-request PIL
        time.sleep(0.1)
    jpeg,err=_bench_grab()                                        # fallback one-shot grab
    if err: return Response(err,status=503)
    return Response(_bench_render(jpeg),mimetype="image/jpeg",headers={"Cache-Control":"no-store"})

@app.route("/api/mcp",methods=["POST"])
def api_mcp():
    if not _books_ok(): return Response('{"errors":[{"detail":"locked"}]}',status=401,mimetype="application/json")
    d=request.get_json(silent=True) or {}
    name=str(d.get("name","")); args=d.get("args",{}) or {}
    if name.endswith("make_api_request"): return jsonify(_square_proxy(args))
    g=db.get("google_config",{}) or {}
    google_on=bool(g.get("client_id") and g.get("client_secret") and g.get("refresh_token"))
    try:
        if name.endswith("search_threads"):    return jsonify(_gmail_search(args) if google_on else _GOOGLE_EMPTY["search_threads"])
        if name.endswith("get_thread"):        return jsonify(_gmail_thread(args) if google_on else _GOOGLE_EMPTY["get_thread"])
        if name.endswith("search_files"):      return jsonify(_drive_search(args) if google_on else _GOOGLE_EMPTY["search_files"])
        if name.endswith("read_file_content"): return jsonify(_drive_read(args) if google_on else _GOOGLE_EMPTY["read_file_content"])
    except Exception as e:
        return jsonify({"errors":[{"detail":str(e)}]})
    return jsonify({})

@app.route("/temps")
def temps():
    with probe_lock: t=dict(probe_temps)
    with state_lock: s={k:{kk:vv for kk,vv in v.items() if kk!="removal_timer"} for k,v in probe_state.items()}
    return Response(json.dumps({"probes":t,"states":s,"names":probe_names,"status":ble_status["message"],"connected":ble_status["connected"],"settings":settings,"timer_triggers":dict(timer_triggers),"timers":timers_snapshot(),"wait":wait_state(),"drop_times":{"bbq":avg_cook_time("bbq",settings["bbq_drop_minutes"]),"fried":avg_cook_time("fried",settings["fried_drop_minutes"])},"rot":rot_state(),"fry":fry_state()}),mimetype="application/json")

@app.route("/set_name",methods=["POST"])
def set_name():
    d=request.get_json(silent=True) or {};pid=int(d.get("probe_id",0))
    if 1<=pid<=4: probe_names[pid]=str(d.get("name",""))[:30];probe_state[pid]["printed"]=False
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/set_settings",methods=["POST"])
def set_settings():
    d=request.get_json(silent=True) or {}
    for k in ["cooked_temp","almost_temp","overdone_temp"]:
        if k in d:
            try: settings[k]=float(d[k])
            except (TypeError,ValueError): pass
    for k in ["use_by_minutes","quality_minutes","bbq_drop_minutes","fried_drop_minutes","bbq_pieces","fried_pieces"]:
        if k in d:
            try: settings[k]=int(d[k])
            except (TypeError,ValueError): pass
    if "printer_ip" in d: settings["printer_ip"]=str(d["printer_ip"])
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/ack_probe",methods=["POST"])
def ack_probe():
    d=request.get_json(silent=True) or {};pid=int(d.get("probe_id",0))
    if 1<=pid<=4:
        with state_lock: probe_state[pid]["alerted"]=True
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/reset_probe",methods=["POST"])
def reset_probe():
    d=request.get_json(silent=True) or {};pid=int(d.get("probe_id",0))
    if 1<=pid<=4:
        with state_lock:
            if probe_state[pid]["removal_timer"]: probe_state[pid]["removal_timer"].cancel()
            probe_state[pid]={"status":"idle","alerted":False,"printed":False,"peak_temp":None,"removed":False,"removal_timer":None,"cook_start":None}
        timer_triggers[pid]=False
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/clear_timer_trigger",methods=["POST"])
def clear_timer_trigger():
    d=request.get_json(silent=True) or {};pid=int(d.get("probe_id",0))
    if 1<=pid<=4: timer_triggers[pid]=False
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/api/timer",methods=["POST"])
def api_timer():
    d=request.get_json(silent=True) or {}
    try: i=int(d.get("id",-1))
    except (TypeError,ValueError): i=-1
    if not (0<=i<4): return jsonify({"ok":False,"error":"bad id"})
    action=str(d.get("action","")); now=time.time()
    try: val=int(d.get("value",0))
    except (TypeError,ValueError): val=0
    with data_lock:
        t=db["timers"][i]
        cur=max(0,int(round(t["end_at"]-now))) if (t.get("running") and t.get("end_at")) else int(t.get("remaining",0))
        if action=="start":
            rem=cur if cur>0 else t["total"]
            t.update({"running":True,"expired":False,"remaining":rem,"end_at":now+rem})
        elif action=="pause":
            t.update({"running":False,"end_at":None,"remaining":cur})
        elif action=="adjust":
            nr=max(0,cur+val); t["total"]=max(60,t["total"]+val); t["remaining"]=nr
            if t.get("running"): t["end_at"]=now+nr
            if val>0 and t.get("expired"): t["expired"]=False
        elif action in ("end","confirm"):
            t.update({"running":False,"expired":False,"end_at":None,"remaining":t["total"]})
        elif action=="label":
            t["label"]=str(d.get("value",""))[:40]
        elif action=="settotal":
            if val>=60 and not t.get("running"): t.update({"total":val,"remaining":val,"expired":False})
        else:
            return jsonify({"ok":False,"error":"bad action"})
        save_data(db)
    return jsonify({"ok":True})

@app.route("/test_print",methods=["POST"])
def test_print():
    d=request.get_json(silent=True) or {};pid=int(d.get("probe_id",1))
    ub=datetime.now()+timedelta(minutes=settings["use_by_minutes"])
    threading.Thread(target=print_ticket,args=(pid,probe_names.get(pid,f"Probe {pid}"),probe_temps.get(pid) or 80.0,ub),daemon=True).start()
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/api/data")
def get_db():
    # never ship secrets to the browser (Google refresh token, books password hash, session key, camera login)
    safe={k:v for k,v in db.items() if k not in ("google_config","books_auth","_secret_key","camera_config","rotcam_config","cameras")}
    safe["cameras_public"]=[]
    for c in (db.get("cameras") or []):
        item={k:c.get(k) for k in ("id","name","ip","port","channel","enabled")}
        item["has_rtsp"]=bool((c.get("rtsp_url") or "").strip())
        safe["cameras_public"].append(item)
    cc=db.get("camera_config") or {}
    safe["camera_enabled"]=bool(cc.get("enabled") and cc.get("ip"))   # boolean only
    safe["camera_public"]={k:cc.get(k) for k in ("ip","port","channel","url_override","enabled")}  # no user/pass
    rc=db.get("rotcam_config") or {}
    safe["rotcam_public"]={k:rc.get(k) for k in ("ip","stream","model","interval","enabled","active_start","active_end","feed_enabled","spin_enabled","doneness_enabled","bench_enabled","bench_interval","bench_ip","bench_stream")}  # no pass/key
    safe["rotcam_public"]["bench_configured"]=bool((rc.get("bench_rtsp_url") or rc.get("bench_ip") or "").strip())
    safe["rotcam_public"]["bench_user"]=rc.get("bench_user","")   # username is not secret — show it so it can be verified
    safe["rotcam_public"]["bench_has_pass"]=bool((rc.get("bench_pass") or "").strip() or (rc.get("bench_rtsp_url") or "").strip())
    safe["rotcam_has_key"]=bool((rc.get("gemini_key") or "").strip())
    try: safe["backend_update_pending"]=os.path.exists(PENDING_FILE)
    except Exception: safe["backend_update_pending"]=False
    _ct=ROTCAM.get("calls_today",0); _tt=ROTCAM.get("calls_total",0)
    safe["rotcam_usage"]={"today":_ct,"cost_today":round(_ct*_GEM_COST_PER_CALL,2),"total":_tt,"cost_total":round(_tt*_GEM_COST_PER_CALL,2),"per_call":_GEM_COST_PER_CALL}
    return jsonify(safe)

@app.route("/api/data",methods=["POST"])
def update_db():
    with data_lock: db.update(request.get_json(silent=True) or {});save_data(db)
    return jsonify({"ok":True})

@app.route("/webhook/square",methods=["POST"])
def square_webhook():
    # Acknowledged so Square stops retrying; busy/quiet state is driven by square_poll_loop.
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/api/square_activity")
def square_activity():
    return jsonify(square_status)

@app.route("/api/test_square",methods=["POST"])
def test_square():
    try:
        n=query_square_recent(db.get("square_config",{}) or {},30)
        return jsonify({"ok":True,"count":n})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})

@app.route("/api/cook_log")
def cook_log(): return jsonify(db.get("cook_log",[]))

@app.route("/api/rot_live")
def api_rot_live(): return jsonify(rot_state())

@app.route("/api/fry_live")
def api_fry_live(): return jsonify(fry_state())

@app.route("/api/sales_feed")
def api_sales_feed():
    items=list(reversed(SALES_FEED.get("items",[])))
    return jsonify({"items":items,"bbq_total":round(sum(i["bbq"] for i in items),2),"fried_total":round(sum(i["fried"] for i in items)),"count":len(items)})

@app.route("/api/fry_put_on",methods=["POST"])
def api_fry_put_on():
    d=request.get_json(silent=True) or {};fry_put_on(int(d.get("batches",1) or 1));return jsonify(fry_state())

@app.route("/api/fry_adjust",methods=["POST"])
def api_fry_adjust():
    d=request.get_json(silent=True) or {}
    if "set" in d: fry_set(d.get("set",0))
    else: fry_adjust(float(d.get("delta",0) or 0))
    return jsonify(fry_state())

@app.route("/api/rot_put_on",methods=["POST"])
def api_rot_put_on():
    d=request.get_json(silent=True) or {};rot_put_on(int(d.get("rows",1) or 1));return jsonify(rot_state())

@app.route("/api/rot_adjust",methods=["POST"])
def api_rot_adjust():
    d=request.get_json(silent=True) or {}
    if "set" in d: rot_set(d.get("set",0))
    else: rot_adjust(float(d.get("delta",0) or 0))
    return jsonify(rot_state())

@app.route("/api/rot_reset",methods=["POST"])
def api_rot_reset(): rot_reset_counts();return jsonify(rot_state())

@app.route("/api/fry_reset",methods=["POST"])
def api_fry_reset(): fry_reset_counts();return jsonify(fry_state())

@app.route("/api/rotisserie_upload",methods=["POST"])
def rotisserie_upload():
    try:
        text=request.get_data(as_text=True)
        rot=db.get("rotisserie") or {}
        mapping=rot.get("map") or DEFAULT_ROT_MAP
        profile,weeks,total=build_rotisserie_profile(text,mapping)
        if total<=0: return jsonify({"ok":False,"error":"No matching BBQ chicken sales found in that file."})
        with data_lock:
            rot.update({"profile":profile,"weeks":weeks,"total":total,"updated":datetime.now().strftime("%Y-%m-%d %H:%M")})
            for k,v in {"birds_per_row":4,"rows_max":6,"open_rows":2,"open_hour":10,"close_hour":21,"stop_load_hour":19,"safety":2,"cook_min":65}.items(): rot.setdefault(k,v)
            rot["map"]=mapping;db["rotisserie"]=rot;save_data(db)
        return jsonify({"ok":True,"weeks":weeks,"total":total})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/log_drop",methods=["POST"])
def log_drop():
    d=request.get_json(silent=True) or {};bt=d.get("type");mins=d.get("mins")
    if bt in ("bbq","fried") and isinstance(mins,(int,float)) and mins>0: record_batch(bt,mins)
    return Response('{"ok":true}',mimetype="application/json")

@app.route("/api/daily_report")
def daily_report_preview(): return Response(compose_daily_report(),mimetype="text/plain")

@app.route("/api/send_report",methods=["POST"])
def send_report():
    cfg=db.get("report_config",{}) or {}
    to=(request.get_json(silent=True) or {}).get("to") or cfg.get("to") or (db.get("email_config",{}) or {}).get("smtp_user","")
    try:
        send_email(to,"Bruno's — Daily Report "+datetime.now().strftime("%d/%m/%Y"),compose_daily_report())
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

@app.route("/api/send_orders",methods=["POST"])
def send_orders():
    data=request.get_json(silent=True) or {};orders=data.get("orders",{});suppliers=data.get("suppliers",[])
    now=datetime.now().strftime("%A %d %B %Y, %H:%M");days_str=data.get("days","")
    errors=[];sent=0
    for sup_id,items in orders.items():
        sup=next((s for s in suppliers if s["id"]==sup_id),None)
        if not sup: continue
        lines="\n".join(f"  - {i['name']}: {i['qty']} x {i['unit']}" for i in items)
        covering=f"\nCovering: {days_str}\n" if days_str else ""
        body=f"Hi {sup['name']},\n\nPlease process the following order:\n\n{lines}\n{covering}\nOrder placed: {now}\n\nThank you."
        try: send_email(sup["email"],f"Stock Order — {now}",body);sent+=1
        except Exception as e: errors.append(f"{sup['name']}: {e}")
    return jsonify({"ok":not errors,"error":"; ".join(errors),"sent":sent})

@app.route("/api/send_reminder",methods=["POST"])
def send_reminder():
    data=request.get_json(silent=True) or {};msg=data.get("message","Reminder");to=data.get("to","")
    errors=[]
    if to:
        try: send_email(to,"Kitchen Reminder",msg)
        except Exception as e: errors.append(str(e))
    return jsonify({"ok":not errors,"error":"; ".join(errors)})

@app.route("/api/test_email",methods=["POST"])
def test_email_route():
    data=request.get_json(silent=True) or {}
    try:
        send_email(data.get("to") or db.get("email_config",{}).get("smtp_user",""),"Kitchen Dashboard — Test Email","Your Kitchen Operations Dashboard email is working correctly.")
        return jsonify({"ok":True})
    except Exception as e: return jsonify({"ok":False,"error":str(e)})

# ===== SONOS music control (local LAN, via the SoCo library) =======================
_SONOS={"dev":None}
def _sonos_coordinator():
    try:
        import soco
    except Exception:
        return None,"Sonos library not installed (run: pip install soco, then restart)"
    d=_SONOS.get("dev")
    if d is not None:
        try: _=d.player_name            # touch it — if it errors, re-discover
        except Exception: d=None
    if d is None:
        ip=(db.get("sonos_ip") or "").strip()
        if ip:
            try: cand=soco.SoCo(ip); _=cand.player_name; d=cand
            except Exception: d=None
        if d is None:
            try: d=soco.discovery.any_soco()
            except Exception: d=None
        if d is not None:
            _SONOS["dev"]=d
            try:
                with data_lock: db["sonos_ip"]=d.ip_address; save_data(db)
            except Exception: pass
    if d is None: return None,"No Sonos found on the network"
    try: return d.group.coordinator, None      # control the whole group together
    except Exception: return d, None
@app.route("/api/sonos/status")
def api_sonos_status():
    c,err=_sonos_coordinator()
    if err: return jsonify({"ok":False,"error":err})
    out={"ok":True}
    try:
        ti=c.get_current_track_info() or {}
        out["title"]=ti.get("title") or ""; out["artist"]=ti.get("artist") or ""
        out["album_art"]=ti.get("album_art") or ""
    except Exception: pass
    try: out["state"]=(c.get_current_transport_info() or {}).get("current_transport_state","")
    except Exception: out["state"]=""
    try: out["volume"]=c.group.volume
    except Exception:
        try: out["volume"]=c.volume
        except Exception: out["volume"]=None
    try: out["favorites"]=[f.title for f in c.music_library.get_sonos_favorites()]
    except Exception: out["favorites"]=[]
    return jsonify(out)
@app.route("/api/sonos/cmd",methods=["POST"])
def api_sonos_cmd():
    c,err=_sonos_coordinator()
    if err: return jsonify({"ok":False,"error":err})
    d=request.get_json(silent=True) or {}; a=str(d.get("action",""))
    def setvol(v):
        try: c.group.volume=max(0,min(100,int(v)))
        except Exception: c.volume=max(0,min(100,int(v)))
    def getvol():
        try: return c.group.volume
        except Exception: return c.volume
    try:
        if a=="play": c.play()
        elif a=="pause": c.pause()
        elif a=="next": c.next()
        elif a=="previous": c.previous()
        elif a=="vol_up": setvol(getvol()+5)
        elif a=="vol_down": setvol(getvol()-5)
        elif a=="set_volume": setvol(d.get("volume",30))
        elif a=="play_favorite":
            favs=c.music_library.get_sonos_favorites()
            i=int(d.get("index",0))
            if 0<=i<len(favs):
                fav=favs[i]
                meta=getattr(fav,"resource_meta_data","") or ""
                c.play_uri(uri=fav.reference.get_uri(), meta=meta)
                c.play()
        else: return jsonify({"ok":False,"error":"unknown action"})
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)})
# ==================================================================================

if __name__=="__main__":
    import webbrowser
    print("="*55)
    print("  Kitchen Operations Dashboard v2")
    print("="*55)
    print("  Local:  http://127.0.0.1:8080")
    print("  Tablet: http://<YOUR-IP>:8080")
    print("  Ctrl+C to stop")
    print("="*55)
    threading.Thread(target=lambda:app.run(host="0.0.0.0",port=8080,debug=False,use_reloader=False,threaded=True),daemon=True).start()
    threading.Thread(target=square_poll_loop,daemon=True).start()
    threading.Thread(target=report_loop,daemon=True).start()
    threading.Thread(target=rotcam_loop,daemon=True).start()
    threading.Thread(target=rotcam_stream_loop,daemon=True).start()
    threading.Thread(target=benchcam_stream_loop,daemon=True).start()
    threading.Timer(2.0,lambda:webbrowser.open("http://127.0.0.1:8080")).start()
    asyncio.run(ble_loop())
