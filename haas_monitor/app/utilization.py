"""Production utilization, independent of HA and MQTT.

Only minute duration totals and current period counters are stored. Unobserved
scheduled time stays in the denominator, never in confirmed production time.
"""
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .schedule import ScheduleStore, defaults, day_shifts

UTC = timezone.utc


def shifts(day, zone, schedule=None):
    config=schedule or defaults().model_copy(update={"timezone":str(zone)})
    return day_shifts(day,config)


def segments(start,end,zone,schedule=None):
    day=datetime.fromtimestamp(start,zone).date()-timedelta(days=1)
    last=datetime.fromtimestamp(end,zone).date()
    while day<=last:
        for identity,a,b,breaks in shifts(day,zone,schedule):
            cursor=a
            for c,d in breaks+[(b,b)]:
                left,right=max(start,cursor),min(end,c)
                if right>left: yield identity,left,right
                cursor=d
        day+=timedelta(days=1)


def planned(start,end,zone):
    return sum(b-a for _,a,b in segments(start,end,zone)) if end>start else 0


class Utilization:
    def __init__(self, path, timezone_name="America/Chicago", schedule=None):
        self.schedule=schedule or ScheduleStore()
        if schedule is None and timezone_name!="America/Chicago":
            self.schedule.epochs[0]=(0,defaults().model_copy(update={"timezone":timezone_name}))
        self.db = sqlite3.connect(path)
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0,1):
            self.db.close()
            raise ValueError("Unsupported utilization database; restore a backup")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS machines(id INTEGER PRIMARY KEY, uuid TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS minutes(machine INTEGER, minute INTEGER, production REAL NOT NULL,
          observed REAL NOT NULL, PRIMARY KEY(machine,minute)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS periods(machine INTEGER, period TEXT, start REAL NOT NULL,
          production REAL NOT NULL, observed REAL NOT NULL, PRIMARY KEY(machine,period)) WITHOUT ROWID;
        PRAGMA user_version=1;
        ''')
        self.previous = {}
        self.rules = {}
        self.last_flush = time.monotonic()
        self.last_cleanup = 0

    @property
    def zone(self): return ZoneInfo(self.schedule.current.timezone)

    def segments(self,start,end):
        for a,b,config in self.schedule.windows(start,end):
            yield from segments(a,b,ZoneInfo(config.timezone),config)

    def calendar_label(self,name,stamp,zone=None):
        local=datetime.fromtimestamp(stamp,zone or ZoneInfo(self.schedule.at(stamp).timezone))
        return local.date().isoformat() if name=="week" else local.strftime("%Y-%m")

    def machine(self, identity):
        self.db.execute("INSERT OR IGNORE INTO machines(uuid) VALUES (?)", (identity,))
        return self.db.execute("SELECT id FROM machines WHERE uuid=?", (identity,)).fetchone()[0]

    def boundaries(self, stamp):
        config=self.schedule.at(stamp)
        zone=ZoneInfo(config.timezone)
        local = datetime.fromtimestamp(stamp, zone)
        day = local.date()
        week = datetime.combine(day - timedelta(days=day.weekday()), datetime.min.time(), zone)
        month = datetime.combine(day.replace(day=1), datetime.min.time(), zone)
        # Shift ownership defines the production week/month, including Friday's overnight tail.
        for _, a, b, _ in shifts(day - timedelta(days=1), zone,config):
            if a <= stamp < b:
                anchor = datetime.fromtimestamp(a, zone).date()
                week = datetime.combine(anchor - timedelta(days=anchor.weekday()), datetime.min.time(), zone)
                month = datetime.combine(anchor.replace(day=1), datetime.min.time(), zone)
        return {"week": week.timestamp(), "month": month.timestamp()}

    def observe(self, identity, signals, stamp=None, max_gap=2, actual_feed_published=True):
        stamp = time.time() if stamp is None else stamp
        rule = "confirmed_feed" if actual_feed_published else "automatic_cycle"
        if self.rules.get(identity,rule) != rule:
            self.gap(identity)
        self.rules[identity] = rule
        required = ("mode", "execution", "feed_rate") if actual_feed_published else ("mode", "execution")
        known = all(n in signals and signals[n].get("available",True) for n in required)
        known = known and signals["mode"]["value"] in ("AUTOMATIC","MANUAL","MANUAL_DATA_INPUT") and signals["execution"]["value"] in ("ACTIVE","READY","STOPPED","INTERRUPTED","FEED_HOLD","PROGRAM_STOPPED","PROGRAM_COMPLETED","OPTIONAL_STOP")
        production = known and signals["mode"]["value"] == "AUTOMATIC" and signals["execution"]["value"] == "ACTIVE" and (not actual_feed_published or signals["feed_rate"]["value"] > 0)
        old = self.previous.get(identity)
        self.previous[identity] = (stamp, known, production)
        if old and known and old[1] and 0 < stamp-old[0] <= max_gap:
            machine = self.machine(identity)
            for shift, left, right in self.segments(old[0], stamp):
                cursor = left
                while cursor < right:
                    end = min(right, (int(cursor // 60)+1)*60)
                    duration = end-cursor
                    # Both bounding observations must confirm production.
                    running = duration if production and old[2] else 0
                    minute = int(cursor // 60)*60
                    self.db.execute('''INSERT INTO minutes VALUES (?,?,?,?) ON CONFLICT(machine,minute)
                        DO UPDATE SET production=production+excluded.production, observed=observed+excluded.observed''',
                        (machine, minute, running, duration))
                    buckets = self.boundaries(cursor)
                    anchor = datetime.fromisoformat(shift[:10]).date()
                    config=self.schedule.at(cursor)
                    shift_start=next(a for label,a,b,br in day_shifts(anchor,config) if label==shift)
                    for name,start in {**buckets,"shift_"+shift.rsplit("_",1)[1]:shift_start}.items():
                        if name in ("week","month"):
                            previous=self.db.execute("SELECT start FROM periods WHERE machine=? AND period=?",(machine,name)).fetchone()
                            if previous and self.calendar_label(name,previous[0])==self.calendar_label(name,start,ZoneInfo(config.timezone)):
                                start=previous[0]
                        self.db.execute('''INSERT INTO periods VALUES (?,?,?,?,?) ON CONFLICT(machine,period)
                            DO UPDATE SET start=excluded.start,
                            production=CASE WHEN start=excluded.start THEN production+excluded.production ELSE excluded.production END,
                            observed=CASE WHEN start=excluded.start THEN observed+excluded.observed ELSE excluded.observed END''',
                            (machine, name, start, running, duration))
                    cursor = end
        self.maintain(stamp)

    def gap(self, identity):
        self.previous.pop(identity, None)

    def maintain(self, stamp):
        if stamp - self.last_cleanup >= 60:
            cutoff = int((stamp - 7*86400) // 60 + 1)*60
            self.db.execute("DELETE FROM minutes WHERE minute < ?", (cutoff,))
            self.last_cleanup = stamp
            self.schedule.prune(stamp)
        if time.monotonic() - self.last_flush >= 15:
            self.flush()

    def summary(self, identity, stamp=None):
        stamp = time.time() if stamp is None else stamp
        machine = self.machine(identity)
        self.maintain(stamp)
        windows = {"24h": (stamp-86400, stamp), **{k:(v,stamp) for k,v in self.boundaries(stamp).items()}}
        day = datetime.fromtimestamp(stamp,self.zone).date()
        for number in range(1,len(self.schedule.current.shifts)+1):
            candidates = [item for offset in range(8) for item in shifts(day-timedelta(days=offset),self.zone,self.schedule.current)
                          if item[0].endswith('_'+str(number)) and item[1] <= stamp]
            if candidates:
                _,a,b,_ = max(candidates,key=lambda s:s[1])
                windows['shift_'+str(number)] = (a,min(b,stamp))
        result = {}
        for name,(start,end) in windows.items():
            if name in ("week","month"):
                row=self.db.execute("SELECT start,production,observed FROM periods WHERE machine=? AND period=?",(machine,name)).fetchone()
                if row and self.calendar_label(name,row[0])==self.calendar_label(name,start,self.zone):
                    start=row[0]
                else: row=None
            denominator=sum(b-a for _,a,b in self.segments(start,end))
            if name in ("week","month"):
                anchor=datetime.fromtimestamp(start,ZoneInfo(self.schedule.at(start).timezone)).date().isoformat()
                denominator=sum(b-a for shift,a,b in self.segments(start,end) if shift[:10]>=anchor)
            if name == '24h' or name.startswith('shift_'):
                rows = self.db.execute("SELECT minute,production,observed FROM minutes WHERE machine=? AND minute+60>? AND minute<?",(machine,start,end)).fetchall()
                # Minute totals give rolling-window boundary accuracy of one minute.
                p = sum(p*(max(0,m+60-start)/60 if m<start else 1) for m,p,o in rows)
                o = sum(o*(max(0,m+60-start)/60 if m<start else 1) for m,p,o in rows)
            else:
                p,o = row[1:] if row else (0,0)
            p,o = min(p,denominator),min(o,denominator)
            result[name] = {"production_seconds": round(p,2), "planned_seconds": round(denominator,2),
                            "unknown_seconds": round(max(0,denominator-o),2),
                            "utilization": round(100*p/denominator,2) if denominator else 0,
                            "coverage": round(100*o/denominator,2) if denominator else 0,
                            "start": datetime.fromtimestamp(start,UTC).isoformat(),
                            "end": datetime.fromtimestamp(end,UTC).isoformat()}
        return result

    def signals(self, identity, stamp=None):
        # HA gets the requested OEE percentages only. Detailed duration/coverage
        # accounting stays internal and is available through diagnostics.
        result = {}
        labels={"24h":"OEE - 24hr","week":"OEE - Week","month":"OEE - Month"}
        ordinals={1:"1st",2:"2nd",3:"3rd"}
        for period,values in self.summary(identity,stamp).items():
            if period.startswith("shift_"):
                number=int(period.split("_")[1])
                label="OEE - "+ordinals.get(number,str(number)+"th")+" Shift"
            else: label=labels[period]
            result[f"production_{period}_utilization"] = {"label":label,"value":values["utilization"],"unit":"%","kind":"sensor","source":"monitor."+self.rules.get(identity,"confirmed_feed")+"_utilization","group":"utilization", "available":values["coverage"]>0,
                "details":{"production_rule":self.rules.get(identity,"confirmed_feed"), "automatic_warmup_counts":self.rules.get(identity)=="automatic_cycle"}}
        return result

    def remove(self, identity):
        row = self.db.execute("SELECT id FROM machines WHERE uuid=?",(identity,)).fetchone()
        if row:
            for table in ('minutes','periods'):
                self.db.execute(f"DELETE FROM {table} WHERE machine=?",(row[0],))
            self.db.execute("DELETE FROM machines WHERE id=?",(row[0],))
        self.gap(identity)
        self.rules.pop(identity,None)
        self.flush()

    def flush(self):
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self.last_flush = time.monotonic()

    def close(self):
        self.flush()
        self.db.close()
