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
import time
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

    @app.get("/sw.js")
    def worker():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
