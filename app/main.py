"""Exit Poll: durable ingestion, offline-friendly API and retryable Sheets export."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
LOG = logging.getLogger("exit_poll")
PARTIES = [
    ("1", "Единая Россия"), ("2", "Яблоко"), ("3", "ЛДПР"),
    ("4", "Партия прямой демократии"), ("5", "Зелёные"),
    ("6", "Справедливая Россия"), ("7", "Родина"), ("8", "КПРФ"),
    ("9", "Партия пенсионеров"), ("10", "Коммунисты России"),
    ("11", "Новые люди"), ("spoiled", "Испортил бюллетень"),
    ("refused", "Отказался отвечать"),
]
LABELS = dict(PARTIES)
AGES = ["18–24", "25–34", "35–44", "45–60", "61+"]
HEADERS = ["ID анкеты", "Время заполнения", "Дата смены", "Фамилия интервьюера",
           "Имя интервьюера", "ID УИК", "УИК", "Ответ", "Пол", "Возраст",
           "ID смены", "Получено сервером", "ТИК"]


class Settings:
    def __init__(self):
        self.production = os.getenv("APP_ENV", "development") == "production"
        self.access_code = os.getenv("ACCESS_CODE", "")
        self.secret = os.getenv("SESSION_SECRET", "") or secrets.token_hex(32)
        if self.production and (len(self.access_code) < 12 or len(self.secret) < 32
                                or not os.getenv("SESSION_SECRET")):
            raise RuntimeError("Production requires ACCESS_CODE (12+) and SESSION_SECRET (32+).")
        self.db = Path(os.getenv("DATABASE_PATH", str(ROOT / "data/exit_poll.sqlite3")))
        self.zone = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        self.precincts = json.loads(Path(os.getenv("PRECINCTS_FILE", str(ROOT / "precincts.json"))).read_text())
        if not self.precincts or len({p["id"] for p in self.precincts}) != len(self.precincts):
            raise RuntimeError("Precinct list must be nonempty with unique ids.")
        if self.production and any(p["id"].startswith("demo-") for p in self.precincts):
            raise RuntimeError("Replace demonstration precincts with the verified official list before production.")
        self.refusal_demographics = os.getenv("REFUSAL_DEMOGRAPHICS", "true").lower() == "true"
        self.spreadsheet = os.getenv("GOOGLE_SPREADSHEET_ID", "")
        self.sheet = os.getenv("GOOGLE_SHEET_NAME", "Анкеты")
        self.dashboard_code = os.getenv("DASHBOARD_CODE", "")
        self.sms_url = os.getenv("SMS_GATEWAY_URL", "")
        self.sms_token = os.getenv("SMS_GATEWAY_TOKEN", "")
        self.sms_number = os.getenv("SMS_REQUEST_NUMBER", "")
        if self.sms_url and not self.sms_url.startswith("https://"):
            raise RuntimeError("SMS_GATEWAY_URL must use HTTPS.")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Profile(StrictModel):
    id: UUID
    surname: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=80)
    precinct: str = Field(min_length=1, max_length=100)
    day: date
    tik: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("surname", "name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Введите имя без управляющих символов")
        return value


class Survey(StrictModel):
    id: UUID
    profile: Profile
    created_at: datetime
    party: str
    gender: str | None = None
    age: str | None = None

    @field_validator("created_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Timezone required")
        return value

    @field_validator("party")
    @classmethod
    def allowed_party(cls, value):
        if value not in LABELS or value == "2":
            raise ValueError("Недоступный вариант ответа")
        return value

    @field_validator("gender")
    @classmethod
    def allowed_gender(cls, value):
        if value not in (None, "male", "female"):
            raise ValueError("Недопустимый пол")
        return value

    @field_validator("age")
    @classmethod
    def allowed_age(cls, value):
        if value is not None and value not in AGES:
            raise ValueError("Недопустимый возраст")
        return value

    @model_validator(mode="after")
    def require_demographics(self):
        if self.party != "refused" and (not self.gender or not self.age):
            raise ValueError("Укажите пол и возраст")
        return self


class Login(StrictModel):
    code: str = Field(max_length=200)


class DashboardLogin(StrictModel):
    code: str = Field(min_length=1, max_length=200)


class SmsRequest(StrictModel):
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


def connect(settings):
    db = sqlite3.connect(settings.db, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def initialize(settings):
    settings.db.parent.mkdir(parents=True, exist_ok=True)
    with connect(settings) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS surveys (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                received_at TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sms_attempts (
                phone_hash TEXT PRIMARY KEY, attempted_at REAL NOT NULL
            );
        """)


def canonical(survey):
    payload = survey.model_dump(mode="json")
    if payload["profile"]["tik"] is None:
        del payload["profile"]["tik"]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def sheets_values(row, settings):
    item = json.loads(row["payload"])
    profile = item["profile"]
    precinct = next((p for p in settings.precincts if p["id"] == profile["precinct"]), {})
    return [item["id"], item["created_at"], profile["day"], profile["surname"],
            profile["name"], profile["precinct"], precinct.get("label", profile["precinct"]), LABELS[item["party"]],
            {"male": "Мужской", "female": "Женский"}.get(item["gender"], ""),
            item["age"] or "", profile["id"], row["received_at"], precinct.get("tik", profile.get("tik", ""))]


def sheet_batch(rows, settings, existing_ids):
    """Locate surveys by UUID, preserving rows after an ephemeral database reset.

    One exporter owns the sheet. Read failure must abort before any write.
    """
    sheet = "'" + settings.sheet.replace("'", "''") + "'"
    positions = {str(values[0]): index + 2 for index, values in enumerate(existing_ids) if values and values[0]}
    next_row = len(existing_ids) + 2
    data = [{"range": f"{sheet}!A1:M1", "values": [HEADERS]}]
    for row in rows:
        number = positions.get(row["id"])
        if number is None:
            number = next_row
            next_row += 1
            positions[row["id"]] = number
        data.append({"range": f"{sheet}!A{number}:M{number}", "values": [sheets_values(row, settings)]})
    return {"valueInputOption": "RAW", "data": data}


def export_once(settings, transport=None, read_ids=None):
    if not settings.spreadsheet:
        return 0
    with connect(settings) as db:
        rows = db.execute("SELECT * FROM surveys WHERE exported=0 ORDER BY seq LIMIT 100").fetchall()
    if not rows:
        return 0
    if transport:
        transport(sheet_batch(rows, settings, read_ids() if read_ids else []))
    else:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(settings.spreadsheet, safe='')}/values"
        sheet = "'" + settings.sheet.replace("'", "''") + "'"
        with AuthorizedSession(credentials) as session:
            existing = session.get(base + "/" + quote(f"{sheet}!A2:A", safe=""), timeout=20)
            existing.raise_for_status()
            body = sheet_batch(rows, settings, existing.json().get("values", []))
            response = session.post(base + ":batchUpdate", json=body, timeout=20)
            response.raise_for_status()
    with connect(settings) as db:
        db.executemany("UPDATE surveys SET exported=1 WHERE id=?", [(r["id"],) for r in rows])
    return len(rows)


def read_sheet(settings):
    """Read the survey sheet. The service account is the only Sheets client."""
    if not settings.spreadsheet:
        raise RuntimeError("Google Sheets is not configured")
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheet = "'" + settings.sheet.replace("'", "''") + "'"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/"
           f"{quote(settings.spreadsheet, safe='')}/values/{quote(f'{sheet}!A2:M', safe='')}")
    with AuthorizedSession(credentials) as session:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        return response.json().get("values", [])


def dashboard_snapshot(values, settings, requested_day=None, requested_tik=None, requested_precinct=None):
    """Build a small, privacy-conscious aggregate from rows in Google Sheets."""
    rows = []
    for source in values:
        row = list(source[:13]) + [""] * max(0, 13 - len(source))
        if not row[0] or row[0] == "ID анкеты":
            continue
        rows.append({
            "id": str(row[0]), "created_at": str(row[1]), "day": str(row[2]),
            "surname": str(row[3]), "name": str(row[4]), "precinct_id": str(row[5]),
            "precinct": str(row[6]), "answer": str(row[7]), "gender": str(row[8]),
            "age": str(row[9]), "shift_id": str(row[10]), "received_at": str(row[11]),
            "tik": str(row[12]),
        })

    dates = sorted({row["day"] for row in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["day"])}, reverse=True)
    selected_day = requested_day or (dates[0] if dates else datetime.now(settings.zone).date().isoformat())
    filtered = [row for row in rows if row["day"] == selected_day]
    if requested_tik:
        filtered = [row for row in filtered if row["tik"] == requested_tik]
    if requested_precinct:
        filtered = [row for row in filtered if row["precinct_id"] == requested_precinct]

    answers = Counter(row["answer"] for row in filtered)
    genders = Counter(row["gender"] for row in filtered if row["gender"])
    ages = Counter(row["age"] for row in filtered if row["age"])
    shifts = {row["shift_id"] or f'{row["surname"]}|{row["name"]}' for row in filtered}
    total = len(filtered)

    catalog_tiks = sorted({p.get("tik", "") for p in settings.precincts if p.get("tik")})
    tik_rows = defaultdict(list)
    for row in [r for r in rows if r["day"] == selected_day]:
        tik_rows[row["tik"]].append(row)
    tik_stats = []
    for tik in catalog_tiks:
        items = tik_rows.get(tik, [])
        tik_stats.append({
            "tik": tik,
            "total": len(items),
            "refusals": sum(x["answer"] == "Отказался отвечать" for x in items),
            "spoiled": sum(x["answer"] == "Испортил бюллетень" for x in items),
            "uiks": len({x["precinct_id"] for x in items}),
            "interviewers": len({x["shift_id"] or f'{x["surname"]}|{x["name"]}' for x in items}),
        })
    tik_stats.sort(key=lambda item: (-item["total"], item["tik"]))

    uik_stats = []
    if requested_tik:
        by_uik = defaultdict(list)
        for row in [r for r in rows if r["day"] == selected_day and r["tik"] == requested_tik]:
            by_uik[row["precinct_id"]].append(row)
        for precinct in [p for p in settings.precincts if p.get("tik") == requested_tik]:
            items = by_uik.get(precinct["id"], [])
            uik_stats.append({
                "id": precinct["id"], "label": precinct["label"], "total": len(items),
                "refusals": sum(x["answer"] == "Отказался отвечать" for x in items),
                "spoiled": sum(x["answer"] == "Испортил бюллетень" for x in items),
                "interviewers": len({x["shift_id"] or f'{x["surname"]}|{x["name"]}' for x in items}),
            })
        uik_stats.sort(key=lambda item: (-item["total"], item["label"]))
        if requested_precinct:
            uik_stats = [item for item in uik_stats if item["id"] == requested_precinct]

    hours = Counter()
    for row in filtered:
        try:
            moment = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            hours[moment.astimezone(settings.zone).hour] += 1
        except (ValueError, TypeError):
            pass

    by_interviewer = defaultdict(list)
    for row in filtered:
        by_interviewer[row["shift_id"] or f'{row["surname"]}|{row["name"]}'].append(row)
    interviewers = []
    for items in by_interviewer.values():
        first = items[0]
        interviewers.append({
            "name": (first["surname"] + " " + first["name"]).strip(),
            "tik": first["tik"], "precinct": first["precinct"], "total": len(items),
            "refusals": sum(x["answer"] == "Отказался отвечать" for x in items),
        })
    interviewers.sort(key=lambda item: (-item["total"], item["name"]))

    def moment_key(row):
        try:
            return datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0

    recent = []
    for row in sorted(filtered, key=moment_key, reverse=True)[:12]:
        try:
            moment = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            display_time = moment.astimezone(settings.zone).strftime("%H:%M")
        except (ValueError, TypeError):
            display_time = "—"
        recent.append({"time": display_time, "tik": row["tik"], "precinct": row["precinct"], "answer": row["answer"]})

    party_order = [label for party_id, label in PARTIES if party_id != "2"]
    return {
        "generated_at": datetime.now(settings.zone).isoformat(),
        "selected_day": selected_day, "selected_tik": requested_tik or "",
        "selected_precinct": requested_precinct or "", "available_dates": dates,
        "filters": {"tiks": catalog_tiks,
                    "precincts": ([{"id": p["id"], "label": p["label"]}
                                   for p in settings.precincts if p.get("tik") == requested_tik]
                                  if requested_tik else [])},
        "summary": {"total": total, "refusals": answers["Отказался отвечать"],
                    "spoiled": answers["Испортил бюллетень"], "interviewers": len(shifts),
                    "uiks": len({row["precinct_id"] for row in filtered}),
                    "tiks": len({row["tik"] for row in filtered if row["tik"]})},
        "parties": [{"label": label, "count": answers[label],
                     "percent": round(answers[label] * 100 / total, 1) if total else 0} for label in party_order],
        "genders": [{"label": label, "count": genders[label],
                     "percent": round(genders[label] * 100 / total, 1) if total else 0} for label in ("Мужской", "Женский")],
        "ages": [{"label": label, "count": ages[label],
                  "percent": round(ages[label] * 100 / total, 1) if total else 0} for label in AGES],
        "hours": [{"hour": f"{hour:02d}:00", "count": hours[hour]} for hour in range(7, 24)],
        "tik_stats": tik_stats, "uik_stats": uik_stats,
        "interviewers": interviewers[:100], "recent": recent,
    }


def create_app(settings=None):
    settings = settings or Settings()
    initialize(settings)

    async def exporter():
        while True:
            try:
                await asyncio.to_thread(export_once, settings)
            except Exception as exc:
                # Do not log survey payloads, credentials, or provider response bodies.
                LOG.warning("Sheets export failed (%s); retry in 30 seconds", type(exc).__name__)
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(exporter())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="Exit Poll", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    failures = {}
    dashboard_cache = {"at": 0.0, "values": []}
    dashboard_lock = threading.Lock()

    def signed(value):
        return hmac.new(settings.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authorized(request: Request):
        if not settings.access_code:
            return
        token = request.cookies.get("exit_poll_session", "")
        try:
            expiry, signature = token.split(".")
            if int(expiry) >= time.time() and hmac.compare_digest(signature, signed(expiry)):
                return
        except (ValueError, TypeError):
            pass
        raise HTTPException(401, "Введите код доступа")

    def dashboard_authorized(request: Request):
        if not settings.dashboard_code:
            raise HTTPException(503, "Задайте DASHBOARD_CODE в настройках Render")
        token = request.cookies.get("exit_poll_dashboard_session", "")
        try:
            expiry, signature = token.split(".")
            expected = signed("dashboard:" + expiry)
            if int(expiry) >= time.time() and hmac.compare_digest(signature, expected):
                return
        except (ValueError, TypeError):
            pass
        raise HTTPException(401, "Введите код координатора")

    def cached_sheet_values():
        with dashboard_lock:
            if time.monotonic() - dashboard_cache["at"] < 25:
                return dashboard_cache["values"]
            values = read_sheet(settings)
            dashboard_cache.update(at=time.monotonic(), values=values)
            return values

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return Response("Cross-origin request rejected", status_code=403)
            if int(request.headers.get("content-length", "0")) > 32768:
                return Response("Request too large", status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        return {"precincts": settings.precincts, "timezone": str(settings.zone),
                "parties": [{"id": i, "label": label, "disabled": i == "2"} for i, label in PARTIES],
                "ages": AGES, "refusal_demographics": settings.refusal_demographics,
                "auth_required": bool(settings.access_code), "sheets_configured": bool(settings.spreadsheet),
                "sms_configured": bool(settings.sms_url and settings.sms_token),
                "sms_request_number": settings.sms_number,
                "demo": not settings.production}

    @app.get("/api/session", dependencies=[Depends(authorized)])
    def session():
        return {"ok": True}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        now = time.time()
        key = request.client.host if request.client else "unknown"
        for old in list(failures):
            if now - failures[old][1] >= 300:
                del failures[old]
        count, since = failures.get(key, (0, now))
        if count >= 10:
            raise HTTPException(429, "Повторите через 5 минут")
        if settings.access_code and not hmac.compare_digest(body.code.encode(), settings.access_code.encode()):
            failures[key] = (count + 1, since)
            raise HTTPException(401, "Неверный код доступа")
        failures.pop(key, None)
        expiry = str(int(now + 30 * 86400))
        response.set_cookie("exit_poll_session", expiry + "." + signed(expiry),
                            httponly=True, secure=settings.production, samesite="strict", max_age=30 * 86400)
        return {"ok": True}

    @app.get("/api/dashboard/session", dependencies=[Depends(dashboard_authorized)])
    def dashboard_session():
        return {"ok": True}

    @app.post("/api/dashboard/login")
    def dashboard_login(body: DashboardLogin, request: Request, response: Response):
        now = time.time()
        key = "dashboard:" + (request.client.host if request.client else "unknown")
        count, since = failures.get(key, (0, now))
        if now - since >= 300:
            count, since = 0, now
        if count >= 10:
            raise HTTPException(429, "Повторите через 5 минут")
        if not settings.dashboard_code:
            raise HTTPException(503, "Задайте DASHBOARD_CODE в настройках Render")
        if not hmac.compare_digest(body.code.encode(), settings.dashboard_code.encode()):
            failures[key] = (count + 1, since)
            raise HTTPException(401, "Неверный код координатора")
        failures.pop(key, None)
        expiry = str(int(now + 12 * 3600))
        response.set_cookie("exit_poll_dashboard_session", expiry + "." + signed("dashboard:" + expiry),
                            httponly=True, secure=settings.production, samesite="strict", max_age=12 * 3600)
        return {"ok": True}

    @app.get("/api/dashboard/data", dependencies=[Depends(dashboard_authorized)])
    def dashboard_data(day: str | None = None, tik: str | None = None, precinct: str | None = None):
        if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(422, "Неверная дата")
        if tik and tik not in {p.get("tik") for p in settings.precincts}:
            raise HTTPException(422, "Неизвестный ТИК")
        if precinct and precinct not in {p["id"] for p in settings.precincts}:
            raise HTTPException(422, "Неизвестный УИК")
        try:
            return dashboard_snapshot(cached_sheet_values(), settings, day, tik, precinct)
        except Exception as exc:
            LOG.warning("Dashboard Sheets read failed (%s)", type(exc).__name__)
            raise HTTPException(502, "Не удалось прочитать Google Таблицу")

    @app.post("/api/surveys", dependencies=[Depends(authorized)])
    def submit(body: Survey):
        precinct = next((p for p in settings.precincts if p["id"] == body.profile.precinct), None)
        # Keep pre-update demo surveys deliverable from the offline queue.
        legacy_demo = body.profile.precinct in {"demo-001", "demo-002", "demo-003"} and body.profile.tik is None
        if precinct is None and not legacy_demo:
            raise HTTPException(422, "Неизвестный УИК")
        if body.profile.tik is not None and (not precinct or body.profile.tik != precinct.get("tik")):
            raise HTTPException(422, "УИК не относится к выбранному ТИК")
        if body.created_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise HTTPException(422, "Проверьте дату и время на телефоне")
        if body.created_at.astimezone(settings.zone).date() != body.profile.day:
            raise HTTPException(422, "Дата анкеты не совпадает с датой смены")
        if settings.refusal_demographics and (not body.gender or not body.age):
            raise HTTPException(422, "Укажите пол и возраст")
        payload = canonical(body)
        with connect(settings) as db:
            db.execute("INSERT OR IGNORE INTO surveys(id,payload,received_at) VALUES(?,?,?)",
                       (str(body.id), payload, datetime.now(timezone.utc).isoformat()))
            row = db.execute("SELECT payload,exported FROM surveys WHERE id=?", (str(body.id),)).fetchone()
            if row["payload"] != payload:
                raise HTTPException(409, "Анкета с этим ID уже содержит другие ответы")
        return {"id": str(body.id), "saved": True, "sheets_synced": bool(row["exported"])}

    @app.post("/api/sms/request", dependencies=[Depends(authorized)])
    async def request_sms(body: SmsRequest):
        if not settings.sms_url or not settings.sms_token:
            raise HTTPException(503, "SMS-сервис ещё не подключён")
        hashed = signed("phone:" + body.phone)
        now = time.time()
        with connect(settings) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT attempted_at FROM sms_attempts WHERE phone_hash=?", (hashed,)).fetchone()
            if row and now - row[0] < 900:
                raise HTTPException(429, "Повторный запрос SMS доступен через 15 минут")
            recent = db.execute("SELECT COUNT(*) FROM sms_attempts WHERE attempted_at>?", (now - 3600,)).fetchone()[0]
            if recent >= 100:
                raise HTTPException(429, "Лимит SMS. Обратитесь к координатору")
            db.execute("INSERT OR REPLACE INTO sms_attempts VALUES(?,?)", (hashed, now))
        text = ("Exit Poll. За какую партию Вы проголосовали? "
                + "; ".join(f"{i}: {label}" for i, label in PARTIES if i not in ("2", "spoiled", "refused"))
                + ". 12: испортил бюллетень; 13: отказ. Пол: М/Ж. Возраст: 18–24, 25–34, 35–44, 45–60, 61+. "
                "Сохраните ответы в приложении. Это SMS-памятка; ответы на SMS не обрабатываются.")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                result = await client.post(settings.sms_url,
                    headers={"Authorization": "Bearer " + settings.sms_token},
                    json={"to": body.phone, "text": text, "request_id": secrets.token_hex(16)})
                result.raise_for_status()
                if result.json().get("accepted") is not True:
                    raise ValueError("Not accepted")
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "Не удалось подтвердить отправку SMS. Продолжайте офлайн; повторите запрос через 15 минут")
        return {"accepted": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/dashboard")
    def dashboard():
        return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def worker():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
