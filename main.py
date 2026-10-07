import json
import logging
import os
import smtplib
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

log = logging.getLogger(__name__)
ALERT_TO = "katie.druckenbrodt@wgcpa.com"
scheduler = BackgroundScheduler()

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

class CustomTaskIn(SQLModel):
    text: str
    phase: str = "Tasks"
    one_off_date: Optional[date] = None     # set → one-off on that date
    recur_days: Optional[str] = None        # null = every weekday; "mon,wed,fri" = specific days
    recur_end: Optional[date] = None        # optional cutoff for recurring tasks

class CustomTask(CustomTaskIn, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

class CustomTaskDone(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("task_id", "day"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="customtask.id")
    day: date
    done_at: datetime

class BlockDone(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("block_id", "week_start"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    block_id: int = Field(foreign_key="block.id")
    week_start: date          # always the Monday of the week
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

def custom_task_on(t: "CustomTask", d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if t.one_off_date is not None:
        return t.one_off_date == d
    if t.recur_end and d > t.recur_end:
        return False
    if t.recur_days:
        return DAYS[d.weekday()] in t.recur_days.split(",")
    return True  # all weekdays

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


# ---------------- Daily alert ----------------
def send_daily_alert():
    email_user = os.getenv("EMAIL_USER")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if not email_user or not email_pass:
        log.warning("EMAIL_USER / EMAIL_PASSWORD not set — skipping alert")
        return

    today = date.today()
    if today.weekday() >= 5:
        return

    with Session(engine) as s:
        items = s.exec(select(ChecklistItem).order_by(ChecklistItem.sort_order)).all()
        done_ids = {c.item_id for c in s.exec(
            select(ChecklistDone).where(ChecklistDone.day == today)).all()}
        items = [i for i in items if not i.fridays_only or today.weekday() == 4]
        incomplete = [i for i in items if i.id not in done_ids]

        custom = s.exec(select(CustomTask)).all()
        cdone = {c.task_id for c in s.exec(
            select(CustomTaskDone).where(CustomTaskDone.day == today)).all()}
        for t in custom:
            if custom_task_on(t, today) and t.id not in cdone:
                incomplete.append(type("Item", (), {"phase": t.phase, "text": t.text})())

    if not incomplete:
        log.info("Daily alert: all checklist items complete — nothing to send")
        return

    date_str = today.strftime("%A, %B %-d, %Y")
    count = len(incomplete)
    subject = f"Reminder: {count} checklist item{'s' if count != 1 else ''} still incomplete"

    phases = list(dict.fromkeys(i.phase for i in incomplete))
    rows = ""
    for phase in phases:
        rows += f"""
        <tr><td colspan="2" style="padding:12px 0 4px;font-size:11px;font-weight:700;
            text-transform:uppercase;letter-spacing:.06em;color:#64748B;
            border-top:1px solid #E2E8F0">{phase}</td></tr>"""
        for item in [i for i in incomplete if i.phase == phase]:
            rows += f"""
        <tr><td style="padding:6px 0;font-size:14px;color:#1E293B;vertical-align:top;
            width:20px">☐</td>
            <td style="padding:6px 0 6px 8px;font-size:14px;color:#1E293B;line-height:1.4">{item.text}</td></tr>"""

    html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,
'Segoe UI',Roboto,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#1E293B">
  <h2 style="margin:0 0 4px">📋 3 PM Checklist Reminder</h2>
  <p style="margin:0 0 24px;color:#64748B;font-size:14px">{date_str}</p>
  <p style="margin:0 0 16px;font-size:15px">
    <strong>{count}</strong> item{'s' if count != 1 else ''} still incomplete:
  </p>
  <table style="width:100%;border-collapse:collapse">{rows}
  </table>
  <hr style="border:none;border-top:1px solid #E2E8F0;margin:24px 0">
  <p style="font-size:12px;color:#94A3B8;margin:0">
    Sent from your Schedule app · <a href="https://your-app.up.railway.app" style="color:#94A3B8">Open app</a>
  </p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = email_user
    msg["To"]      = ALERT_TO
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(email_user, email_pass)
            smtp.send_message(msg)
        log.info("Daily alert sent: %d incomplete items", count)
    except Exception as e:
        log.error("Failed to send daily alert: %s", e)


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
    scheduler.add_job(
        send_daily_alert,
        CronTrigger(hour=15, minute=0, day_of_week="mon-fri", timezone="America/Chicago"),
        id="daily_alert",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="Remote Accounting Associate – Schedule", lifespan=lifespan)

@app.get("/", response_class=HTMLResponse, tags=["health"])
def root():
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")

app.include_router(crud(Client, ClientIn, "/clients", "code", str, validate_client))
app.include_router(crud(Block, BlockIn, "/blocks", "id", int, validate_block))
app.include_router(crud(MonthlyTask, MonthlyTaskIn, "/monthly-tasks", "id", int, validate_monthly))
app.include_router(crud(ChecklistItem, ChecklistItemIn, "/checklist-items", "id", int))
app.include_router(crud(Holiday, HolidayIn, "/holidays", "day", date))
app.include_router(crud(CustomTask, CustomTaskIn, "/custom-tasks", "id", int))


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

@app.post("/admin/test-alert", tags=["settings"])
def test_alert():
    """Send the daily alert right now (for testing)."""
    send_daily_alert()
    return {"status": "sent (check logs if nothing arrived)"}

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
def week(week_start: Optional[date] = None, s: Session = Depends(get_session)):
    if week_start is None:
        today = local_now(s).date()
        week_start = today - timedelta(days=today.weekday())
    st = settings(s)
    day_offset = {d: i for i, d in enumerate(DAYS)}
    blocks = s.exec(select(Block).order_by(Block.start)).all()
    done_ids = {bd.block_id for bd in s.exec(
        select(BlockDone).where(BlockDone.week_start == week_start)).all()}
    result = {}
    for d in DAYS:
        day_date = week_start + timedelta(days=day_offset[d])
        result[d] = [{**block_view(b, day_date, st.biweekly_anchor), "done": b.id in done_ids}
                     for b in blocks if b.day == d]
    return {"week_start": str(week_start), "days": result}

@app.post("/schedule/week/{week_start}/{block_id}", status_code=201, tags=["schedule"])
def mark_block_done(week_start: date, block_id: int, s: Session = Depends(get_session)):
    if not s.get(Block, block_id):
        fail(f"block {block_id} not found", 404)
    rec = s.exec(select(BlockDone).where(
        BlockDone.week_start == week_start, BlockDone.block_id == block_id)).first()
    if not rec:
        rec = BlockDone(block_id=block_id, week_start=week_start, done_at=local_now(s))
        s.add(rec); s.commit(); s.refresh(rec)
    return rec

@app.delete("/schedule/week/{week_start}/{block_id}", status_code=204, tags=["schedule"])
def unmark_block_done(week_start: date, block_id: int, s: Session = Depends(get_session)):
    rec = s.exec(select(BlockDone).where(
        BlockDone.week_start == week_start, BlockDone.block_id == block_id)).first()
    if rec:
        s.delete(rec); s.commit()

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
    done  = {c.item_id: c.done_at for c in s.exec(select(ChecklistDone).where(ChecklistDone.day == d)).all()}
    out   = [{**i.model_dump(), "item_type": "checklist", "done": i.id in done, "done_at": done.get(i.id)}
             for i in items if not i.fridays_only or d.weekday() == 4]

    custom      = s.exec(select(CustomTask)).all()
    custom_done = {c.task_id: c.done_at for c in s.exec(select(CustomTaskDone).where(CustomTaskDone.day == d)).all()}
    for t in custom:
        if custom_task_on(t, d):
            out.append({"id": t.id, "item_type": "custom", "phase": t.phase, "text": t.text,
                        "fridays_only": False, "sort_order": 9999,
                        "done": t.id in custom_done, "done_at": custom_done.get(t.id)})

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

@app.post("/checklist/{d}/custom/{task_id}", status_code=201, tags=["checklist"])
def mark_custom_done(d: date, task_id: int, s: Session = Depends(get_session)):
    if not s.get(CustomTask, task_id):
        fail(f"custom task {task_id} not found", 404)
    rec = s.exec(select(CustomTaskDone).where(
        CustomTaskDone.day == d, CustomTaskDone.task_id == task_id)).first()
    if not rec:
        rec = CustomTaskDone(task_id=task_id, day=d, done_at=local_now(s))
        s.add(rec); s.commit(); s.refresh(rec)
    return rec

@app.delete("/checklist/{d}/custom/{task_id}", status_code=204, tags=["checklist"])
def unmark_custom_done(d: date, task_id: int, s: Session = Depends(get_session)):
    rec = s.exec(select(CustomTaskDone).where(
        CustomTaskDone.day == d, CustomTaskDone.task_id == task_id)).first()
    if rec:
        s.delete(rec); s.commit()
