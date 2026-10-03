import json
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

BASE = Path(__file__).parent
SEED_FILE = BASE / "schedule.json"
engine = create_engine(f"sqlite:///{BASE / 'schedule.db'}", connect_args={"check_same_thread": False})

DAYS = ["mon", "tue", "wed", "thu", "fri"]
QUARTER_CLOSE_MONTHS = {1, 4, 7, 10}
OTHER_CATEGORIES = {"Admin", "Close", "Lunch", "Wrap-up"}
NON_WORK = {"Lunch"}


# ---------------- Models ----------------
class SettingsIn(SQLModel):
    timezone: str = "America/New_York"
    buffer_bds: int = 2                 # internal due date = client deadline minus N business days
    min_hours_per_day: float = 8.0
    biweekly_anchor: date               # a date in a week when biweekly blocks run

class Settings(SettingsIn, table=True):
    id: int = Field(default=1, primary_key=True)

class ClientIn(SQLModel):
    code: str
    notes: str = ""
    target_hours_per_week: Optional[float] = None
    close_day_type: str = "BD"          # BD = business day, CD = calendar day
    close_day: int
    quarter_close_day_type: Optional[str] = None   # override in Jan/Apr/Jul/Oct
    quarter_close_day: Optional[int] = None
    buffer_exempt: bool = False         # True = due on the actual deadline (BAR)

class Client(ClientIn, table=True):
    code: str = Field(primary_key=True)

class BlockIn(SQLModel):
    day: str                            # mon..fri
    start: time
    end: time
    category: str                       # client code or Admin / Close / Lunch / Wrap-up
    label: str
    tasks: str = ""
    biweekly: bool = False

class Block(BlockIn, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

class MonthlyTaskIn(SQLModel):
    client: str
    timing: str                         # BD = nth business day, DOM = day of month
    day: int
    tasks: str

class MonthlyTask(MonthlyTaskIn, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

class ChecklistItemIn(SQLModel):
    phase: str
    text: str
    fridays_only: bool = False
    sort_order: int = 0

class ChecklistItem(ChecklistItemIn, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

class HolidayIn(SQLModel):
    day: date
    name: str = ""

class Holiday(HolidayIn, table=True):
    day: date = Field(primary_key=True)

class ChecklistDone(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("item_id", "day"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="checklistitem.id")
    day: date
    done_at: datetime


# ---------------- Helpers ----------------
def get_session():
    with Session(engine) as s:
        yield s

def fail(msg, code=422):
    raise HTTPException(code, msg)

def make(model, in_model, data: dict):
    return model(**in_model(**data).model_dump())

def settings(s: Session) -> Settings:
    return s.get(Settings, 1)

def local_now(s):
    return datetime.now(ZoneInfo(settings(s).timezone))

def hours(b: Block) -> float:
    return (datetime.combine(date.min, b.end) - datetime.combine(date.min, b.start)).seconds / 3600

def holiday_set(s):
    return {h.day for h in s.exec(select(Holiday)).all()}

def is_bd(d, hol):
    return d.weekday() < 5 and d not in hol

def workday(d, n, hol):
    """Same as Excel WORKDAY(d, n)."""
    step = 1 if n > 0 else -1
    for _ in range(abs(n)):
        d += timedelta(days=step)
        while not is_bd(d, hol):
            d += timedelta(days=step)
    return d

def nth_day(first, kind, n, hol):
    return workday(first - timedelta(days=1), n, hol) if kind == "BD" else first + timedelta(days=n - 1)

def biweekly_on(d, anchor):
    monday = lambda x: x - timedelta(days=x.weekday())
    return ((monday(d) - monday(anchor)).days // 7) % 2 == 0


# ---------------- Validation ----------------
def validate_client(s, c):
    if c.close_day_type not in ("BD", "CD"):
        fail("close_day_type must be BD or CD")
    if c.quarter_close_day_type not in (None, "BD", "CD"):
        fail("quarter_close_day_type must be BD, CD or null")
    if (c.quarter_close_day_type is None) != (c.quarter_close_day is None):
        fail("set both quarter_close fields or neither")

def validate_block(s, b):
    if b.day not in DAYS:
        fail(f"day must be one of {DAYS}")
    if b.start >= b.end:
        fail("start must be before end")
    if b.category not in OTHER_CATEGORIES and not s.get(Client, b.category):
        fail(f"category must be a client code or one of {sorted(OTHER_CATEGORIES)}")
    for o in s.exec(select(Block).where(Block.day == b.day, Block.id != b.id)).all():
        if b.start < o.end and o.start < b.end:
            fail(f"overlaps block {o.id} ({o.start:%H:%M}-{o.end:%H:%M} {o.label})", 409)

def validate_monthly(s, m):
    if m.timing not in ("BD", "DOM"):
        fail("timing must be BD or DOM")
    if not s.get(Client, m.client):
        fail(f"unknown client {m.client}")


# ---------------- Generic CRUD ----------------
def crud(model, in_model, prefix, pk_name, pk_type, validate=None):
    r = APIRouter(prefix=prefix, tags=[prefix.strip("/")])

    def _get(s, pk):
        obj = s.get(model, pk)
        if not obj:
            fail(f"{model.__name__} {pk} not found", 404)
        return obj

    @r.get("", response_model=list[model])
    def list_all(s: Session = Depends(get_session)):
        return s.exec(select(model)).all()

    @r.get("/{pk}", response_model=model)
    def read(pk: pk_type, s: Session = Depends(get_session)):
        return _get(s, pk)

    @r.post("", response_model=model, status_code=201)
    def create(data: in_model, s: Session = Depends(get_session)):
        obj = model(**data.model_dump())
        pk = getattr(obj, pk_name)
        if pk is not None and s.get(model, pk):
            fail(f"{model.__name__} {pk} already exists", 409)
        if validate:
            validate(s, obj)
        s.add(obj); s.commit(); s.refresh(obj)
        return obj

    @r.put("/{pk}", response_model=model)
    def update(pk: pk_type, data: in_model, s: Session = Depends(get_session)):
        obj = _get(s, pk)
        for k, v in data.model_dump().items():
            if k != pk_name:
                setattr(obj, k, v)
        if validate:
            validate(s, obj)
        s.add(obj); s.commit(); s.refresh(obj)
        return obj

    @r.delete("/{pk}", status_code=204)
    def delete(pk: pk_type, s: Session = Depends(get_session)):
        s.delete(_get(s, pk)); s.commit()

    return r


# ---------------- Seeding ----------------
def seed():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if s.get(Settings, 1):
            return
        data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        s.add(make(Settings, SettingsIn, data["settings"]))
        for h in data.get("holidays", []):
            s.add(make(Holiday, HolidayIn, h))
        for c in data["clients"]:
            s.add(make(Client, ClientIn, c))
        for m in data["monthly_tasks"]:
            s.add(make(MonthlyTask, MonthlyTaskIn, m))
        for i, item in enumerate(data["checklist_items"]):
            s.add(make(ChecklistItem, ChecklistItemIn, {"sort_order": i, **item}))
        templates = data.get("task_templates", {})
        for b in data["blocks"]:
            b = dict(b)
            key = b.pop("task", None)
            if key:
                b["tasks"] = templates[key]
            s.add(make(Block, BlockIn, b))
        s.commit()

@asynccontextmanager
async def lifespan(app):
    seed()
    yield

app = FastAPI(title="Remote Accounting Associate – Schedule", lifespan=lifespan)
app.include_router(crud(Client, ClientIn, "/clients", "code", str, validate_client))
app.include_router(crud(Block, BlockIn, "/blocks", "id", int, validate_block))
app.include_router(crud(MonthlyTask, MonthlyTaskIn, "/monthly-tasks", "id", int, validate_monthly))
app.include_router(crud(ChecklistItem, ChecklistItemIn, "/checklist-items", "id", int))
app.include_router(crud(Holiday, HolidayIn, "/holidays", "day", date))


# ---------------- Settings ----------------
@app.get("/settings", response_model=Settings, tags=["settings"])
def get_settings(s: Session = Depends(get_session)):
    return settings(s)

@app.put("/settings", response_model=Settings, tags=["settings"])
def update_settings(data: SettingsIn, s: Session = Depends(get_session)):
    st = settings(s)
    for k, v in data.model_dump().items():
        setattr(st, k, v)
    s.add(st); s.commit(); s.refresh(st)
    return st

@app.post("/admin/reseed", tags=["settings"])
def reseed():
    """Drops ALL data (incl. checklist history) and reloads schedule.json."""
    SQLModel.metadata.drop_all(engine)
    seed()
    return {"status": "reseeded from schedule.json"}


# ---------------- Calendar ----------------
def close_rows(s, year, month, hol):
    st, first, rows = settings(s), date(year, month, 1), []
    for c in s.exec(select(Client)).all():
        kind, n = c.close_day_type, c.close_day
        if month in QUARTER_CLOSE_MONTHS and c.quarter_close_day_type:
            kind, n = c.quarter_close_day_type, c.quarter_close_day
        deadline = nth_day(first, kind, n, hol)
        early = 0 if c.buffer_exempt else st.buffer_bds
        due = workday(deadline, -early, hol)
        rows.append({"client": c.code, "rule": f"{kind} {n}", "client_deadline": deadline,
                     "bds_early": early, "due_date": due, "due_day": due.strftime("%a"), "notes": c.notes})
    return sorted(rows, key=lambda r: (r["due_date"], r["client"]))

def monthly_rows(s, year, month, hol):
    first, rows = date(year, month, 1), []
    for m in s.exec(select(MonthlyTask)).all():
        d = nth_day(first, "BD", m.day, hol) if m.timing == "BD" else date(year, month, min(m.day, monthrange(year, month)[1]))
        rows.append({"id": m.id, "client": m.client, "timing": f"{m.timing} {m.day}", "date": d,
                     "day": d.strftime("%a"), "on_business_day": is_bd(d, hol), "tasks": m.tasks})
    return sorted(rows, key=lambda r: r["date"])

@app.get("/calendar/{year}/{month}", tags=["calendar"])
def month_calendar(year: int, month: int, s: Session = Depends(get_session)):
    """year/month = the month the close is worked in (e.g. 2026/10 for the September close)."""
    if not 1 <= month <= 12:
        fail("month must be 1-12")
    hol = holiday_set(s)
    return {"close_month": f"{year}-{month:02d}", "buffer_bds": settings(s).buffer_bds,
            "monthly_tasks": monthly_rows(s, year, month, hol),
            "close_deadlines": close_rows(s, year, month, hol)}


# ---------------- Schedule ----------------
def block_view(b, d=None, anchor=None):
    out = {**b.model_dump(), "hours": round(hours(b), 2)}
    if b.biweekly and d is not None:
        out["active"] = biweekly_on(d, anchor)
        if not out["active"]:
            out["note"] = "Off week – use this time for closing & project work"
    return out

@app.get("/schedule/week", tags=["schedule"])
def week(s: Session = Depends(get_session)):
    blocks = s.exec(select(Block).order_by(Block.start)).all()
    return {d: [block_view(b) for b in blocks if b.day == d] for d in DAYS}

@app.get("/schedule/today", tags=["schedule"])
def today(s: Session = Depends(get_session)):
    return schedule_for_date(local_now(s).date(), s)

@app.get("/schedule/date/{d}", tags=["schedule"])
def schedule_for_date(d: date, s: Session = Depends(get_session)):
    hol, st = holiday_set(s), settings(s)
    base = {"date": d, "weekday": d.strftime("%A")}
    if d.weekday() > 4 or d in hol:
        return {**base, "workday": False, "holiday": d in hol, "blocks": []}
    blocks = s.exec(select(Block).where(Block.day == DAYS[d.weekday()]).order_by(Block.start)).all()
    return {**base, "workday": True,
            "blocks": [block_view(b, d, st.biweekly_anchor) for b in blocks],
            "monthly_tasks_today": [r for r in monthly_rows(s, d.year, d.month, hol) if r["date"] == d],
            "close_due_today": [r for r in close_rows(s, d.year, d.month, hol) if r["due_date"] == d]}

@app.get("/hours/summary", tags=["schedule"])
def hours_summary(s: Session = Depends(get_session)):
    st, blocks = settings(s), s.exec(select(Block)).all()
    targets = {c.code: c.target_hours_per_week for c in s.exec(select(Client)).all()}
    cats = {}
    for b in blocks:
        cats.setdefault(b.category, {d: 0.0 for d in DAYS})[b.day] += hours(b)
    rows = []
    for cat, byday in cats.items():
        total, t = sum(byday.values()), targets.get(cat)
        rows.append({"category": cat, **{d: round(v, 2) for d, v in byday.items()}, "week": round(total, 2),
                     "target": t, "variance": round(total - t, 2) if t is not None else None})
    worked = {d: round(sum(hours(b) for b in blocks if b.day == d and b.category not in NON_WORK), 2) for d in DAYS}
    return {"by_category": rows, "worked_hours": worked, "min_hours_per_day": st.min_hours_per_day,
            "meets_minimum": {d: worked[d] >= st.min_hours_per_day for d in DAYS}}


# ---------------- Checklist tracking ----------------
def _done_rec(s, d, item_id):
    return s.exec(select(ChecklistDone).where(ChecklistDone.day == d, ChecklistDone.item_id == item_id)).first()

@app.get("/checklist/today", tags=["checklist"])
def checklist_today(s: Session = Depends(get_session)):
    return checklist(local_now(s).date(), s)

@app.get("/checklist/{d}", tags=["checklist"])
def checklist(d: date, s: Session = Depends(get_session)):
    items = s.exec(select(ChecklistItem).order_by(ChecklistItem.sort_order)).all()
    done = {c.item_id: c.done_at for c in s.exec(select(ChecklistDone).where(ChecklistDone.day == d)).all()}
    out = [{**i.model_dump(), "done": i.id in done, "done_at": done.get(i.id)}
           for i in items if not i.fridays_only or d.weekday() == 4]
    return {"date": d, "complete": all(x["done"] for x in out),
            "remaining": sum(not x["done"] for x in out), "items": out}

@app.post("/checklist/{d}/{item_id}", status_code=201, tags=["checklist"])
def mark_done(d: date, item_id: int, s: Session = Depends(get_session)):
    if not s.get(ChecklistItem, item_id):
        fail(f"checklist item {item_id} not found", 404)
    rec = _done_rec(s, d, item_id)
    if not rec:
        rec = ChecklistDone(item_id=item_id, day=d, done_at=local_now(s))
        s.add(rec); s.commit(); s.refresh(rec)
    return rec

@app.delete("/checklist/{d}/{item_id}", status_code=204, tags=["checklist"])
def unmark_done(d: date, item_id: int, s: Session = Depends(get_session)):
    rec = _done_rec(s, d, item_id)
    if rec:
        s.delete(rec); s.commit()
