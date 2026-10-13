"""Validated shared schedule and bounded configuration revisions.

Revisions preserve the planned-time denominator across edits; they are settings,
not an archive of machine readings. Historical minute data stays limited to 7 days.
"""
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .store import atomic_json


def minute(value):
    h,m=map(int,value.split(':'))
    return h*60+m


class Break(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    start: str
    minutes: int=Field(ge=1,le=240)

    @field_validator('start')
    @classmethod
    def valid_time(cls,value):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value):
            raise ValueError('Use HH:MM in 24-hour time')
        return value


class Shift(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    name: str=Field(min_length=1,max_length=80)
    start: str
    end: str
    breaks: list[Break]=Field(default_factory=list,max_length=12)
    valid_time=field_validator('start','end')(Break.valid_time.__func__)

    @model_validator(mode='after')
    def valid_intervals(self):
        duration=(minute(self.end)-minute(self.start))%1440
        if not duration: raise ValueError('Shift start and end must differ')
        ranges=sorted(((minute(b.start)-minute(self.start))%1440,b.minutes) for b in self.breaks)
        end=0
        for offset,length in ranges:
            if offset<end or offset+length>duration:
                raise ValueError('Breaks must be inside the shift and must not overlap')
            end=offset+length
        return self


class ScheduleInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    timezone: str='America/Chicago'
    weekdays: list[int]=Field(default_factory=lambda:[0,1,2,3,4],min_length=1,max_length=7)
    shifts: list[Shift]=Field(min_length=1,max_length=8)
    revision: int | None=Field(default=None,ge=1)

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls,value):
        try: ZoneInfo(value)
        except (KeyError,ValueError): raise ValueError('Enter an IANA timezone such as America/Chicago')
        return value

    @field_validator('weekdays')
    @classmethod
    def valid_days(cls,value):
        if len(set(value))!=len(value) or any(day<0 or day>6 for day in value):
            raise ValueError('Select unique working days, Monday=0 through Sunday=6')
        return sorted(value)

    @model_validator(mode='after')
    def no_overlap(self):
        intervals=[]
        for day in self.weekdays:
            for shift in self.shifts:
                a=day*1440+minute(shift.start)
                intervals.append((a,a+(minute(shift.end)-minute(shift.start))%1440))
        # Include the next week to detect Sunday overnight / Monday overlap.
        intervals=sorted(intervals+[(a+10080,b+10080) for a,b in intervals])
        if any(a[1]>b[0] for a,b in zip(intervals,intervals[1:])):
            raise ValueError('Shifts must not overlap, including overnight shifts')
        return self


def defaults():
    return ScheduleInput(shifts=[
        Shift(name='First shift',start='06:00',end='16:00',breaks=[Break(start='08:30',minutes=10),Break(start='11:00',minutes=30),Break(start='13:30',minutes=10)]),
        Shift(name='Second shift',start='16:00',end='02:00',breaks=[Break(start='18:30',minutes=10),Break(start='21:00',minutes=30),Break(start='23:30',minutes=10)])])


def day_shifts(day,schedule):
    if day.weekday() not in schedule.weekdays: return []
    midnight=datetime.combine(day,datetime.min.time(),ZoneInfo(schedule.timezone))
    result=[]
    for number,shift in enumerate(schedule.shifts,1):
        start=midnight+timedelta(minutes=minute(shift.start))
        end=start+timedelta(minutes=(minute(shift.end)-minute(shift.start))%1440)
        breaks=[]
        for item in sorted(shift.breaks,key=lambda b:(minute(b.start)-minute(shift.start))%1440):
            a=start+timedelta(minutes=(minute(item.start)-minute(shift.start))%1440)
            breaks.append((a.timestamp(),(a+timedelta(minutes=item.minutes)).timestamp()))
        result.append((f'{day.isoformat()}_{number}',start.timestamp(),end.timestamp(),breaks))
    return result


class ScheduleStore:
    def __init__(self,path=None):
        self.path=path
        raw=json.loads(path.read_text()) if path and path.exists() else {'version':1,'revision':1,'epochs':[{'effective':0,'schedule':defaults().model_dump(exclude={'revision'})}]}
        if raw.get('version')!=1 or not raw.get('epochs'): raise ValueError('Damaged shared schedule; restore a backup')
        self.revision=raw['revision']
        self.epochs=[(item['effective'],ScheduleInput.model_validate(item['schedule'])) for item in raw['epochs']]
        if self.epochs!=sorted(self.epochs,key=lambda item:item[0]): raise ValueError('Invalid schedule revision order')

    @property
    def current(self): return self.epochs[-1][1]

    def view(self):
        return {**self.current.model_dump(exclude={'revision'}),'revision':self.revision,'effective':self.epochs[-1][0]}

    def at(self,stamp):
        return next((value for effective,value in reversed(self.epochs) if effective<=stamp),self.epochs[0][1])

    def windows(self,start,end):
        cursor=start
        for effective,_ in self.epochs:
            if start<effective<end:
                yield cursor,effective,self.at(cursor)
                cursor=effective
        if end>cursor: yield cursor,end,self.at(cursor)

    def save(self,value,stamp=None):
        if value.revision!=self.revision: raise ValueError('Schedule changed; reopen it before saving')
        stamp=time.time() if stamp is None else stamp
        if value.model_dump(exclude={'revision'})==self.current.model_dump(exclude={'revision'}): return False
        self.epochs.append((stamp,value.model_copy(update={'revision':None})))
        self.revision+=1
        self.prune(stamp)
        self.persist()
        return True

    def prune(self,stamp):
        zone=ZoneInfo(self.current.timezone)
        first=datetime.fromtimestamp(stamp,zone).replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()
        cutoff=min(first-2*86400,stamp-8*86400)
        index=max((i for i,(effective,_) in enumerate(self.epochs) if effective<=cutoff),default=0)
        if index:
            self.epochs=self.epochs[index:]
            self.persist()

    def persist(self):
        if self.path:
            atomic_json(self.path,{'version':1,'revision':self.revision,'epochs':[{'effective':effective,'schedule':value.model_dump(exclude={'revision'})} for effective,value in self.epochs]})
