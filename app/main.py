"""Exit Poll: durable ingestion, offline-friendly API and retryable Sheets export."""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import zlib
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
ALL_DAYS = "all"
# Dashboard-only grouping of the 56 TIKs into their 12 electoral okrugs (source: coordinator's PDF).
# Not a Sheets column; derived purely for dashboard aggregation from the existing "tik" value.
OKRUGS = {
    "118": ["ТИК города Балашиха", "ТИК города Реутов"],
    "119": ["ТИК города Лобня", "ТИК города Солнечногорск", "ТИК города Дмитров", "ТИК города Химки"],
    "120": ["ТИК города Коломна", "ТИК города Воскресенск", "ТИК города Кашира",
            "ТИК города Луховицы", "ТИК города Зарайск", "ТИК поселка Серебряные Пруды"],
    "121": ["ТИК города Красногорск", "ТИК города Истра", "ТИК города Клин", "ТИК города Волоколамск",
            "ТИК поселка Шаховская", "ТИК поселка Лотошино", "ТИК поселка Восход"],
    "122": ["ТИК города Люберцы", "ТИК города Видное", "ТИК города Раменское",
            "ТИК города Котельники", "ТИК города Жуковский"],
    "123": ["ТИК города Королев", "ТИК города Долгопрудный", "ТИК города Мытищи"],
    "124": ["ТИК города Одинцово", "ТИК города Наро-Фоминск", "ТИК города Руза", "ТИК поселка Молодёжный",
            "ТИК города Краснознаменск", "ТИК города Можайск", "ТИК поселка Власиха"],
    "125": ["ТИК города Электросталь", "ТИК города Орехово-Зуево", "ТИК города Павловский Посад",
            "ТИК города Егорьевск", "ТИК города Шатура"],
    "126": ["ТИК города Подольск", "ТИК города Домодедово", "ТИК города Лыткарино"],
    "127": ["ТИК города Сергиев Посад", "ТИК города Дубна", "ТИК города Талдом", "ТИК города Пушкино"],
    "128": ["ТИК города Серпухов", "ТИК города Чехов", "ТИК города Ступино", "ТИК города Бронницы"],
    "129": ["ТИК города Ногинск", "ТИК города Фрязино", "ТИК города Щелково", "ТИК города Лосино-Петровский",
            "ТИК города Черноголовка", "ТИК поселка Звёздный городок"],
}
TIK_TO_OKRUG = {tik: okrug for okrug, tiks in OKRUGS.items() for tik in tiks}


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
        unmapped = {p["tik"] for p in self.precincts if p.get("tik")} - set(TIK_TO_OKRUG)
        if unmapped:
            raise RuntimeError(f"TIKs missing from OKRUGS mapping: {sorted(unmapped)}")
        self.refusal_demographics = os.getenv("REFUSAL_DEMOGRAPHICS", "true").lower() == "true"
        self.spreadsheet = os.getenv("GOOGLE_SPREADSHEET_ID", "")
        self.sheet = os.getenv("GOOGLE_SHEET_NAME", "Анкеты")
        self.anomaly_sheet = os.getenv("GOOGLE_ANOMALY_SHEET_NAME", "Аномалии")
        self.dashboard_code = os.getenv("DASHBOARD_CODE", "")
        self.roster_code = os.getenv("ROSTER_CODE", "")
        self.dashboard_refresh_seconds = max(10, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "60")))
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


MAX_SURVEY_BATCH = 50
MAX_BATCH_BYTES = 262144


class SurveyBatch(StrictModel):
    """Items stay raw here so that one malformed survey does not reject the other 49."""
    surveys: list[dict] = Field(min_length=1, max_length=MAX_SURVEY_BATCH)


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


class AnomalyStatus(StrictModel):
    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    status: str = Field(pattern=r"^(open|clarified|resolved)$")
    note: str = Field(default="", max_length=500)


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


ANOMALY_STATUS_LABELS = {"open": "Открыта", "clarified": "Уточнена", "resolved": "Устранена"}
ANOMALY_STATUS_CODES = {label: code for code, label in ANOMALY_STATUS_LABELS.items()}
ANOMALY_HEADERS = ["ID аномалии", "Статус", "Комментарий", "Обновлено", "Интервьюер",
                   "Правило", "Дата смены", "ТИК", "УИК"]
ANOMALY_RULES = {
    "fast": "Слишком быстрый темп",
    "run": "Одинаковые ответы подряд",
    "dominant": "Один ответ почти во всех анкетах",
    "uniform": "Однородные респонденты",
    "refusals": "Аномальный процент отказов",
    "hours": "Анкеты вне рабочего времени",
    "late": "Поздняя отправка",
}
FAST_GAP_SECONDS = 20
RUN_MIN = 5
DOMINANT_MIN_ANSWERS = 15
DOMINANT_SHARE = 0.9
UNIFORM_SHARE = 0.8
REFUSAL_MIN_SURVEYS = 15
REFUSAL_Z = 3.5
WORK_START_HOUR, WORK_END_HOUR = 8, 21
LATE_HOURS = 12
SHEETS_WRITE_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]


def parse_sheet_rows(values):
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
    return rows


def parse_moment(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def detect_anomalies(rows, settings):
    """Flag interviewer shifts (one interviewer on one day) whose data looks unusual.

    Deliberately independent of dashboard filters so an anomaly keeps the same id and verdict
    whatever the coordinator is currently looking at.
    """
    day_totals = defaultdict(lambda: [0, 0])
    groups = defaultdict(list)
    for row in rows:
        refused = row["answer"] == "Отказался отвечать"
        day_totals[row["day"]][0] += 1
        day_totals[row["day"]][1] += refused
        key = (row["shift_id"] or f'{row["surname"]}|{row["name"]}', row["day"])
        groups[key].append((parse_moment(row["created_at"]), row))

    found = []
    for (shift_key, day), items in groups.items():
        first = items[0][1]
        n = len(items)
        ordered = sorted((pair for pair in items if pair[0]), key=lambda pair: pair[0])

        def add(rule, severity, detail):
            found.append({
                "id": hashlib.sha1(f"{rule}|{shift_key}|{day}".encode()).hexdigest()[:16],
                "rule": rule, "severity": severity, "title": ANOMALY_RULES[rule], "detail": detail,
                "interviewer": (first["surname"] + " " + first["name"]).strip(),
                "day": day, "tik": first["tik"], "okrug": TIK_TO_OKRUG.get(first["tik"], ""),
                "precinct_id": first["precinct_id"], "precinct": first["precinct"], "count": n,
            })

        if len(ordered) >= 6:
            gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(ordered, ordered[1:])]
            fast = [gap for gap in gaps if gap < FAST_GAP_SECONDS]
            ratio = len(fast) / len(gaps)
            if len(fast) >= 3 and ratio >= 0.3:
                add("fast", "high" if ratio >= 0.6 else "medium",
                    f"{len(fast)} из {len(gaps)} интервалов между анкетами короче {FAST_GAP_SECONDS} с; "
                    f"медиана {median(gaps):.0f} с")

        sequence = [(row["answer"], row["gender"], row["age"]) for _, row in (ordered or items)]
        longest = current = 0
        previous = None
        for triple in sequence:
            current = current + 1 if triple == previous else 1
            previous = triple
            longest = max(longest, current)
        if longest >= RUN_MIN:
            add("run", "high" if longest >= 8 else "medium",
                f"{longest} анкет подряд с одинаковыми ответом, полом и возрастом")

        answered = [row["answer"] for _, row in items
                    if row["answer"] not in ("Отказался отвечать", "Испортил бюллетень")]
        if len(answered) >= DOMINANT_MIN_ANSWERS:
            answer, count = Counter(answered).most_common(1)[0]
            share = count / len(answered)
            if share >= DOMINANT_SHARE:
                add("dominant", "high" if share >= 0.97 else "medium",
                    f"«{answer}» — {count} из {len(answered)} ответов ({share * 100:.0f}%)")

        combos = [(row["gender"], row["age"]) for _, row in items if row["gender"] and row["age"]]
        if len(combos) >= DOMINANT_MIN_ANSWERS:
            combo, count = Counter(combos).most_common(1)[0]
            share = count / len(combos)
            if share >= UNIFORM_SHARE:
                add("uniform", "medium",
                    f"{share * 100:.0f}% респондентов — одна группа ({combo[0]}, {combo[1]}): "
                    f"{count} из {len(combos)}")

        total_day, refusals_day = day_totals[day]
        baseline = refusals_day / total_day
        if n >= REFUSAL_MIN_SURVEYS and 0 < baseline < 1:
            rate = sum(row["answer"] == "Отказался отвечать" for _, row in items) / n
            z = (rate - baseline) / math.sqrt(baseline * (1 - baseline) / n)
            if abs(z) >= REFUSAL_Z:
                add("refusals", "high" if abs(z) >= 5 else "medium",
                    f"{rate * 100:.0f}% отказов при среднем {baseline * 100:.0f}% за день (анкет: {n})")

        outside = sum(1 for moment, _ in ordered
                      if not WORK_START_HOUR <= moment.astimezone(settings.zone).hour < WORK_END_HOUR)
        if outside >= 3:
            add("hours", "high" if outside >= 6 else "medium",
                f"{outside} анкет создано вне {WORK_START_HOUR}:00–{WORK_END_HOUR}:00")

        late = 0
        for created, row in items:
            received = parse_moment(row["received_at"])
            if created and received and (received - created).total_seconds() > LATE_HOURS * 3600:
                late += 1
        if late >= 3:
            add("late", "medium", f"{late} анкет попало на сервер позже чем через {LATE_HOURS} ч после заполнения")
    return found


def _google_call(session, callback):
    if session is not None:
        return callback(session)
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=SHEETS_WRITE_SCOPE)
    with AuthorizedSession(credentials) as live:
        return callback(live)


def read_anomaly_statuses(settings, session=None):
    """id -> {status, note, updated_at} from the anomaly tab; empty while that tab does not exist."""
    if not settings.spreadsheet:
        return {}
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(settings.spreadsheet, safe='')}"
    ref = "'" + settings.anomaly_sheet.replace("'", "''") + "'"

    def run(client):
        response = client.get(f"{base}/values/{quote(ref + '!A2:D', safe='')}", timeout=20)
        if response.status_code == 400:
            return {}
        response.raise_for_status()
        statuses = {}
        for values in response.json().get("values", []):
            padded = list(values) + [""] * (4 - len(values))
            code = ANOMALY_STATUS_CODES.get(str(padded[1]))
            if padded[0] and code:
                statuses[str(padded[0])] = {"status": code, "note": str(padded[2]), "updated_at": str(padded[3])}
        return statuses
    return _google_call(session, run)


def write_anomaly_status(settings, record, session=None):
    """Upsert one anomaly status row by id, creating the tab on first use. RAW: text never becomes a formula."""
    if not settings.spreadsheet:
        raise RuntimeError("Google Sheets is not configured")
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(settings.spreadsheet, safe='')}"
    ref = "'" + settings.anomaly_sheet.replace("'", "''") + "'"

    def run(client):
        meta = client.get(f"{base}?fields=sheets.properties.title", timeout=20)
        meta.raise_for_status()
        titles = {sheet["properties"]["title"] for sheet in meta.json().get("sheets", [])}
        if settings.anomaly_sheet not in titles:
            created = client.post(f"{base}:batchUpdate", timeout=20, json={
                "requests": [{"addSheet": {"properties": {"title": settings.anomaly_sheet}}}]})
            created.raise_for_status()
        existing = client.get(f"{base}/values/{quote(ref + '!A2:A', safe='')}", timeout=20)
        existing.raise_for_status()
        ids = existing.json().get("values", [])
        positions = {str(values[0]): index + 2 for index, values in enumerate(ids) if values and values[0]}
        number = positions.get(record["id"], len(ids) + 2)
        row = [record["id"], ANOMALY_STATUS_LABELS[record["status"]], record["note"], record["updated_at"],
               record["interviewer"], ANOMALY_RULES[record["rule"]], record["day"], record["tik"],
               record["precinct"]]
        body = {"valueInputOption": "RAW", "data": [
            {"range": f"{ref}!A1:I1", "values": [ANOMALY_HEADERS]},
            {"range": f"{ref}!A{number}:I{number}", "values": [row]}]}
        client.post(f"{base}/values:batchUpdate", json=body, timeout=20).raise_for_status()
    _google_call(session, run)


FORECAST_PARTIES = [label for pid, label in PARTIES if pid not in ("2", "spoiled", "refused")]
FORECAST_CATEGORIES = FORECAST_PARTIES + ["Испортил бюллетень"]
FORECAST_DEFF = 2.0
FORECAST_TILT = 1.25
SHRINK_MARGIN, SHRINK_CELL, SHRINK_OKRUG, SHRINK_TIK = 30, 15, 50, 50
JACKKNIFE_GROUPS = 10
HISTORY_FRACTIONS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
YOUNG_AGES, OLDER_AGES = ("18–24", "25–34", "35–44"), ("45–60", "61+")
# Context from the news report used for the remote-voting (DEG) scenarios. It is a dated snapshot, not live data.
DEG_CONTEXT = {"source": "РИА Новости", "as_of": "18:00 МСК 18.09.2026", "share": 0.397, "turnout": 19.67}
_FORECAST_CACHE = {}


BASELINE_FILE = ROOT / "app" / "baseline_2021.json"
BASELINE = json.loads(BASELINE_FILE.read_text(encoding="utf-8")) if BASELINE_FILE.exists() else None
MSK = timezone(timedelta(hours=3))
# Dated turnout snapshots from news reports (day 1 of voting), used to describe how much of the flow the poll saw.
FLOW_CONTEXT = {"day": "2026-09-18", "as_of": "18:00", "as_of_hour": 18, "paper_turnout_as_of": 13.47,
                "paper_turnout_close": 18.23, "paper_voters_as_of": 730820, "source": "РИА Новости, 360.ru"}


def baseline_shares(okrug=None):
    """2021 party-list shares, renormalised over the parties the poll offers (an okrug or the whole area)."""
    if not BASELINE:
        return {}
    entries = [BASELINE["okrugs"][okrug]] if okrug else list(BASELINE["okrugs"].values())
    votes = Counter()
    for entry in entries:
        for label in FORECAST_PARTIES:
            votes[label] += entry["votes"].get(label, 0)
    total = sum(votes.values())
    return {label: round(count * 100 / total, 2) for label, count in votes.items() if count} if total else {}


def territory_weights(settings):
    """Weight of every TIK: its okrug's 2021 electorate, split between the okrug's TIKs by their UIK count."""
    uiks = Counter(p["tik"] for p in settings.precincts if p.get("tik"))
    if not BASELINE:
        return dict(uiks)
    okrug_uiks = Counter()
    for tik, count in uiks.items():
        okrug_uiks[TIK_TO_OKRUG.get(tik, "")] += count
    voters = {okrug: entry["voters"] for okrug, entry in BASELINE["okrugs"].items()}
    total = sum(voters.values())
    return {tik: voters.get(TIK_TO_OKRUG.get(tik), 0) / total * count / okrug_uiks[TIK_TO_OKRUG.get(tik, "")]
            for tik, count in uiks.items()}


def _flow_snapshot(rows):
    """How much of the first day's flow at polling stations the poll could have seen."""
    context = FLOW_CONTEXT
    moments = [m for m in (parse_moment(r["created_at"]) for r in rows if r["day"] == context["day"]) if m]
    if not moments:
        return None
    voted_share = context["paper_turnout_as_of"] / context["paper_turnout_close"] * 100
    by_cutoff = sum(1 for m in moments if m.astimezone(MSK).hour < context["as_of_hour"])
    last = max(moments).astimezone(MSK)
    return {"day": context["day"], "as_of": context["as_of"], "source": context["source"],
            "voted_by_share": round(voted_share, 1), "evening_share": round(100 - voted_share, 1),
            "paper_voters": context["paper_voters_as_of"], "anket_by_as_of": by_cutoff,
            "sample_fraction": round(by_cutoff * 100 / context["paper_voters_as_of"], 2), "last_at": last.isoformat()}


def _blend(counts, prior, strength):
    total = sum(counts)
    return [(counts[i] + strength * prior[i]) / (total + strength) for i in range(len(prior))]


def _prepare_forecast_rows(rows):
    position = {label: i for i, label in enumerate(FORECAST_CATEGORIES)}
    prepared = []
    for row in rows:
        if row["answer"] in position:
            kind = position[row["answer"]]
        elif row["answer"] == "Отказался отвечать":
            kind = -1
        else:
            continue
        tik = row["tik"]
        prepared.append((tik, TIK_TO_OKRUG.get(tik, ""), row["gender"], row["age"], kind,
                         row["precinct_id"], parse_moment(row["created_at"])))
    return prepared


def _forecast_core(prepared, weights, detail=False):
    """One pass of the estimate. Returns vectors over FORECAST_PARTIES (fractions) or None without data."""
    size, count_parties = len(FORECAST_CATEGORIES), len(FORECAST_PARTIES)
    overall = [0] * size
    by_demo = defaultdict(lambda: [0] * size)
    by_okrug = defaultdict(lambda: [0] * size)
    by_cell = defaultdict(lambda: [0] * size)
    by_tik = defaultdict(lambda: [0] * size)
    by_age = defaultdict(lambda: [0.0] * size)
    refusers = Counter()
    for tik, okrug, gender, age, kind, _precinct, _moment in prepared:
        if kind >= 0:
            overall[kind] += 1
            by_okrug[okrug][kind] += 1
            by_cell[(okrug, gender, age)][kind] += 1
            by_tik[tik][kind] += 1
            if gender and age:
                by_demo[(gender, age)][kind] += 1
            if detail:
                by_age[age][kind] += 1
        else:
            refusers[(tik, okrug, gender, age)] += 1
    valid = sum(overall[:count_parties])
    if not valid:
        return None
    prior = [count / sum(overall) for count in overall]

    pooled = [0.0] * size
    alloc_tik = defaultdict(lambda: [0.0] * size)
    tilt_up, tilt_down = [0.0] * count_parties, [0.0] * count_parties
    cells = {}
    for (tik, okrug, gender, age), count in refusers.items():
        key = (okrug, gender, age)
        if key not in cells:
            by_demographics = _blend(by_demo[(gender, age)], prior, SHRINK_MARGIN) if gender and age else prior
            by_geography = _blend(by_okrug[okrug], prior, SHRINK_MARGIN)
            expected = [by_demographics[i] * by_geography[i] / prior[i] if prior[i] else 0.0 for i in range(size)]
            norm = sum(expected) or 1.0
            cell = _blend(by_cell[key], [value / norm for value in expected], SHRINK_CELL)
            up = [cell[i] * FORECAST_TILT / (cell[i] * FORECAST_TILT + 1 - cell[i]) for i in range(count_parties)]
            down = [cell[i] / FORECAST_TILT / (cell[i] / FORECAST_TILT + 1 - cell[i]) for i in range(count_parties)]
            cells[key] = (cell, up, down)
        cell, up, down = cells[key]
        for i in range(size):
            pooled[i] += count * cell[i]
            alloc_tik[tik][i] += count * cell[i]
            if detail:
                by_age[age][i] += count * cell[i]
        for i in range(count_parties):
            tilt_up[i] += count * up[i]
            tilt_down[i] += count * down[i]

    adjusted = [overall[i] + pooled[i] for i in range(count_parties)]
    adjusted_total = sum(adjusted)
    with_refusals = [value / adjusted_total for value in adjusted]
    raw = [overall[i] / valid for i in range(count_parties)]
    tilt = [(abs((overall[i] + tilt_up[i]) / adjusted_total - with_refusals[i])
             + abs(with_refusals[i] - (overall[i] + tilt_down[i]) / adjusted_total)) / 2
            for i in range(count_parties)]

    final, covered, tiks_with_data = with_refusals, 0.0, 0
    if weights:
        vectors = {tik: [by_tik[tik][i] + alloc_tik[tik][i] for i in range(count_parties)]
                   for tik in set(by_tik) | set(alloc_tik)}
        okrug_totals = defaultdict(lambda: [0.0] * count_parties)
        for tik, vector in vectors.items():
            if tik in TIK_TO_OKRUG:
                for i in range(count_parties):
                    okrug_totals[TIK_TO_OKRUG[tik]][i] += vector[i]
        okrug_share = {okrug: _blend(vector, with_refusals, SHRINK_OKRUG) for okrug, vector in okrug_totals.items()}
        weight_total = sum(weights.values())
        accumulated = [0.0] * count_parties
        for tik, weight in weights.items():
            vector = vectors.get(tik)
            parent = okrug_share.get(TIK_TO_OKRUG.get(tik, ""), with_refusals)
            if vector and sum(vector):
                share = _blend(vector, parent, SHRINK_TIK)
                covered += weight
                tiks_with_data += 1
            else:
                share = parent
            for i in range(count_parties):
                accumulated[i] += weight * share[i]
        final = [value / weight_total for value in accumulated]
        covered /= weight_total

    result = {"raw": raw, "with_refusals": with_refusals, "final": final, "tilt": tilt, "valid": valid,
              "refusers": sum(refusers.values()), "covered": covered, "tiks_with_data": tiks_with_data}
    if detail:
        result["by_age"] = {age: vector[:count_parties] for age, vector in by_age.items()}
    return result


def forecast_shares(rows, weights=None):
    """Forecast of the final split among valid ballots, from all survey rows (independent of dashboard filters).

    Steps: respondents -> refusers spread over okrug x gender x age cells -> territories weighted by their
    number of UIKs (TIKs without data borrow their okrug's estimate) -> flow diagnostics (how the estimate
    moved as data arrived) -> uncertainty from a delete-a-group jackknife over UIKs (cluster design) combined
    with sensitivity to refusers voting differently.
    """
    prepared = _prepare_forecast_rows(rows)
    fingerprint = (hash(tuple((t[0], t[2], t[3], t[4], t[5], t[6]) for t in prepared)),
                   hash(tuple(sorted((weights or {}).items()))))
    if fingerprint in _FORECAST_CACHE:
        return _FORECAST_CACHE[fingerprint]
    full = _forecast_core(prepared, weights, detail=True)
    if full is None:
        return None
    parties, count_parties = FORECAST_PARTIES, len(FORECAST_PARTIES)

    groups = {}
    for item in prepared:
        groups.setdefault(item[5], zlib.crc32(item[5].encode()) % JACKKNIFE_GROUPS)
    replicates = []
    for group in sorted(set(groups.values())):
        part = _forecast_core([item for item in prepared if groups[item[5]] != group], weights)
        if part:
            replicates.append(part["final"])
    # Never below plain sampling error (which is also the fallback when there are too few UIK groups).
    floor = [math.sqrt(max(p, 1 / full["valid"]) * (1 - max(p, 1 / full["valid"])) / full["valid"]) for p in full["final"]]
    standard_error = list(floor)
    if len(replicates) >= 5:
        count = len(replicates)
        for i in range(count_parties):
            mean = sum(rep[i] for rep in replicates) / count
            jackknife = math.sqrt((count - 1) / count * sum((rep[i] - mean) ** 2 for rep in replicates))
            standard_error[i] = max(jackknife, floor[i])
    else:
        standard_error = [error * math.sqrt(FORECAST_DEFF) for error in floor]

    ordered = sorted(prepared, key=lambda item: item[6] or datetime.min.replace(tzinfo=timezone.utc))
    history = []
    for fraction in HISTORY_FRACTIONS:
        size = max(1, math.ceil(len(ordered) * fraction))
        part = full if fraction == 1.0 else _forecast_core(ordered[:size], weights)
        if part:
            moment = ordered[size - 1][6]
            history.append({"fraction": fraction, "n": size, "at": moment.isoformat() if moment else "",
                            "final": [round(value * 100, 1) for value in part["final"]]})

    baseline_2021 = baseline_shares()
    order = sorted(range(count_parties), key=lambda i: -full["final"][i])
    trend_point = next((point for point in history if point["fraction"] == 0.7), None)
    rows_out = []
    for i in order:
        forecast = full["final"][i] * 100
        margin = math.hypot(1.96 * standard_error[i] * 100, full["tilt"][i] * 100)
        rows_out.append({
            "label": parties[i], "answered": round(full["raw"][i] * 100, 1),
            "with_refusals": round(full["with_refusals"][i] * 100, 1), "forecast": round(forecast, 1),
            "delta": round(forecast - full["raw"][i] * 100, 1), "margin": round(margin, 1),
            "y2021": baseline_2021.get(parties[i]),
            "trend": round(forecast - trend_point["final"][i], 1) if trend_point else None})

    def group_share(ages):
        vector = [sum(full["by_age"].get(age, [0.0] * count_parties)[i] for age in ages) for i in range(count_parties)]
        total = sum(vector)
        return [value / total for value in vector] if total else None

    scenarios = []
    for key, title, ages in (("young", "ДЭГ как избиратели 18–44 лет", YOUNG_AGES), ("older", "ДЭГ как избиратели 45+", OLDER_AGES)):
        share = group_share(ages)
        if share:
            mixed = [(1 - DEG_CONTEXT["share"]) * full["final"][i] + DEG_CONTEXT["share"] * share[i]
                     for i in range(count_parties)]
            scenarios.append({"key": key, "title": title, "values": {parties[i]: round(mixed[i] * 100, 1) for i in order}})

    age_totals = Counter(item[3] for item in prepared if item[3])
    refusal_by_age = Counter(item[3] for item in prepared if item[3] and item[4] == -1)
    sample_with_age = sum(age_totals.values())
    ages = []
    for age in AGES:
        share = group_share((age,))
        if age_totals[age] and share:
            best = max(range(count_parties), key=lambda i: share[i])
            ages.append({"age": age, "sample_share": round(age_totals[age] * 100 / sample_with_age, 1),
                         "refusal_rate": round(refusal_by_age[age] * 100 / age_totals[age], 1),
                         "leader": parties[best], "leader_share": round(share[best] * 100, 1)})

    moments = [item[6] for item in prepared if item[6]]
    result = {
        "rows": rows_out,
        "scope": {"anket": len(prepared), "respondents": full["valid"], "refusers": full["refusers"],
                  "tiks_with_data": full["tiks_with_data"], "tiks_total": len(weights or {}),
                  "covered_share": round(full["covered"] * 100), "days": sorted({r["day"] for r in rows if r["day"]}),
                  "last_at": max(moments).isoformat() if moments else ""},
        "history": {"points": [{k: v for k, v in point.items() if k != "final"} for point in history],
                    "series": [{"label": parties[i], "values": [point["final"][i] for point in history]}
                               for i in order[:4]]},
        "ages": ages,
        "flow": _flow_snapshot(rows),
        "deg": {**DEG_CONTEXT, "scenarios": scenarios},
    }
    if len(_FORECAST_CACHE) >= 8:
        _FORECAST_CACHE.clear()
    _FORECAST_CACHE[fingerprint] = result
    return result


SERVICE_ANSWERS = ("Испортил бюллетень", "Отказался отвечать")
DETAIL_MIN_N = 30
TIME_BLOCKS = (("до 12:00", 0, 12), ("12:00–16:00", 12, 16), ("с 16:00", 16, 24))
GENDER_LABELS = ("Мужской", "Женский")
DETAIL_KINDS = ("party", "gender", "age", "okrug", "hour", "kpi")
DEFAULT_FOCUS = "Новые люди"
Z95 = 1.96


def _fmt_int(value):
    return f"{int(value):,}".replace(",", " ")


def _pct(value):
    return f"{round(value, 1):g}%"


def _pp(value):
    return f"{value:+.1f} п.п."


def _share(count, total):
    return round(count * 100 / total, 1) if total else 0


def _people(count):
    return "человека" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "человек"


def scope_rows(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct):
    """Rows visible under the dashboard filters. Returns (dates, selected_day, rows of the day, filtered rows)."""
    dates = sorted({row["day"] for row in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["day"])}, reverse=True)
    selected_day = requested_day or (dates[0] if dates else datetime.now(settings.zone).date().isoformat())
    day_rows = rows if selected_day == ALL_DAYS else [row for row in rows if row["day"] == selected_day]
    filtered = day_rows
    if requested_okrug:
        filtered = [row for row in filtered if TIK_TO_OKRUG.get(row["tik"]) == requested_okrug]
    if requested_tik:
        filtered = [row for row in filtered if row["tik"] == requested_tik]
    if requested_precinct:
        filtered = [row for row in filtered if row["precinct_id"] == requested_precinct]
    return dates, selected_day, day_rows, filtered


def _time_block(row, settings):
    moment = parse_moment(row["created_at"])
    if not moment:
        return None
    hour = moment.astimezone(settings.zone).hour
    return next(name for name, low, high in TIME_BLOCKS if low <= hour < high)


def _wilson(hit, n):
    """95% interval for a share, with the effective sample size reduced by the cluster design effect."""
    n_eff = n / FORECAST_DEFF
    if n_eff <= 0:
        return 0.0, 0.0
    p = hit / n
    denominator = 1 + Z95 ** 2 / n_eff
    centre = (p + Z95 ** 2 / (2 * n_eff)) / denominator
    half = Z95 * math.sqrt(p * (1 - p) / n_eff + Z95 ** 2 / (4 * n_eff ** 2)) / denominator
    return centre - half, centre + half


def _z_prop(hit_a, n_a, hit_b, n_b):
    """Difference between two independent groups' shares, in standard errors."""
    if not n_a or not n_b:
        return 0.0
    pooled = (hit_a + hit_b) / (n_a + n_b)
    error = math.sqrt(FORECAST_DEFF * pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    return (hit_a / n_a - hit_b / n_b) / error if error else 0.0


def _z_same_sample(hit_a, hit_b, n):
    """Difference between two options' shares inside one sample (multinomial), in standard errors."""
    if not n:
        return 0.0
    pa, pb = hit_a / n, hit_b / n
    variance = FORECAST_DEFF * (pa + pb - (pa - pb) ** 2) / n
    return (pa - pb) / math.sqrt(variance) if variance > 0 else 0.0


def _standardize(subset, everyone, label, control):
    """Observed vs expected rate for a group if it behaved like everyone else with the same control mix."""
    cells = defaultdict(lambda: [0, 0])
    for r in everyone:
        cell = cells[control(r)]
        cell[1] += 1
        cell[0] += r["answer"] == label
    overall = sum(c[0] for c in cells.values()) / len(everyone) if everyone else 0
    expected = variance = 0.0
    for r in subset:
        hit, n = cells[control(r)]
        p = hit / n if n >= 10 else overall
        expected += p
        variance += p * (1 - p)
    observed = sum(r["answer"] == label for r in subset)
    z = (observed - expected) / math.sqrt(FORECAST_DEFF * variance) if variance else 0.0
    return observed * 100 / len(subset), expected * 100 / len(subset), z


def _verdict(z, n):
    if n < DETAIL_MIN_N:
        return "мало данных", "muted"
    if z >= Z95:
        return "значимо выше", "up"
    if z <= -Z95:
        return "значимо ниже", "down"
    return "в пределах погрешности", ""


def _shift_key(row):
    return row["shift_id"] or f'{row["surname"]}|{row["name"]}'


def _shift_concentration(scope, label, service=False):
    """Do a few shifts hold a disproportionate part of this answer? (interviewer or precinct effect)"""
    pool = scope if service else [r for r in scope if r["answer"] not in SERVICE_ANSWERS]
    hits = Counter(_shift_key(r) for r in pool if r["answer"] == label)
    everything = Counter(_shift_key(r) for r in pool)
    total_hits, total_pool = sum(hits.values()), sum(everything.values())
    if total_hits < DETAIL_MIN_N or len(everything) < 5:
        return None
    top = [key for key, _ in hits.most_common(3)]
    hit_share = sum(hits[k] for k in top) * 100 / total_hits
    pool_share = sum(everything[k] for k in top) * 100 / total_pool
    return {"shifts": len(hits), "hit_share": hit_share, "pool_share": pool_share,
            "flag": hit_share >= 1.8 * pool_share and hit_share >= 40}


def _dim_labels(dim):
    return sorted(OKRUGS) if dim == "okrug" else {"age": AGES, "gender": GENDER_LABELS,
                                                   "time": [name for name, _, _ in TIME_BLOCKS]}[dim]


def _dim_value(dim, row, settings):
    if dim == "okrug":
        return TIK_TO_OKRUG.get(row["tik"], "")
    return _time_block(row, settings) if dim == "time" else row["gender" if dim == "gender" else "age"]


def _dim_label(dim, raw):
    return f"Округ {raw}" if dim == "okrug" else raw


DIM_TITLES = {"age": "по возрасту", "gender": "по полу", "okrug": "по округам", "time": "по времени суток"}


def _segment_stats(rows, settings, dim, label):
    """Share of one answer in each segment of a dimension, with index, significance and contribution."""
    service = label in SERVICE_ANSWERS
    pool_all = rows if service else [r for r in rows if r["answer"] not in SERVICE_ANSWERS]
    total_pool, total_hit = len(pool_all), sum(r["answer"] == label for r in pool_all)
    base = total_hit / total_pool if total_pool else 0
    groups = defaultdict(list)
    for r in rows:
        groups[_dim_value(dim, r, settings)].append(r)
    stats = []
    for raw in _dim_labels(dim):
        segment = groups.get(raw)
        if not segment:
            continue
        pool = segment if service else [r for r in segment if r["answer"] not in SERVICE_ANSWERS]
        n, hit = len(pool), sum(r["answer"] == label for r in pool)
        refusals = sum(r["answer"] == "Отказался отвечать" for r in segment)
        low, high = _wilson(hit, n)
        stats.append({
            "dim": dim, "raw": raw, "label": _dim_label(dim, raw), "n": n, "all": len(segment), "hit": hit,
            "rate": hit * 100 / n if n else 0, "index": (hit / n) / base * 100 if n and base else 0,
            "z": _z_prop(hit, n, total_hit - hit, total_pool - n), "margin": (high - low) / 2 * 100,
            "contribution": hit * 100 / total_hit if total_hit else 0, "refusals": refusals,
            "refusal_rate": refusals * 100 / len(segment)})
    return stats, base * 100


def _cell(text, cls="", bar=None):
    return {"t": text, "cls": cls, "bar": bar}


def _table(title, columns, rows, note=""):
    return {"title": title, "columns": columns, "rows": rows, "note": note}


def _answer_table(dim, stats, label, service):
    unit = "анкет" if service else "назвавших"
    rows = []
    for s in stats:
        text, cls = _verdict(s["z"], s["n"])
        rows.append([_cell(s["label"]), _cell(_fmt_int(s["n"])), _cell(f"{s['rate']:.1f}%", bar=s["rate"]),
                     _cell(f"{s['index']:.0f}" if s["n"] >= DETAIL_MIN_N else "—", cls),
                     _cell(text, cls)])
    return _table(f"«{label}» {DIM_TITLES[dim]}", ["Сегмент", f"База ({unit})", "Доля", "Индекс", "Вывод"], rows,
                  "Индекс: 100 — как в среднем по области; выше — группа поддерживает сильнее.")


def _section(title, findings=None, tables=None, note=""):
    return {"title": title, "findings": findings or [], "tables": tables or [], "note": note}


def _find(level, text):
    return {"level": level, "text": text}


def _quality_findings(scope, focus_or_answer, service=False):
    findings = []
    people = {_shift_key(r) for r in scope}
    precincts = {r["precinct_id"] for r in scope}
    findings.append(_find("info", f"База анализа: {_fmt_int(len(scope))} анкет, {len(precincts)} УИК, {len(people)} смен интервьюеров."))
    concentration = _shift_concentration(scope, focus_or_answer, service)
    if concentration and concentration["flag"]:
        findings.append(_find("warning", f"Три смены дают {concentration['hit_share']:.0f}% этих ответов при {concentration['pool_share']:.0f}% всех анкет: "
                                         "возможен эффект интервьюера или участка, результат стоит перепроверить."))
    return findings


def _rank_table(named, focus):
    counts = Counter(r["answer"] for r in named)
    order = [name for name, _ in counts.most_common()]
    total, mine = len(named), counts[focus]
    rows = []
    for name in order[:8]:
        low, high = _wilson(counts[name], total)
        gap = (counts[name] - mine) * 100 / total
        z = _z_same_sample(counts[name], mine, total)
        text = "это «" + focus + "»" if name == focus else (f"{_pp(gap)}, " + ("значимо" if abs(z) >= Z95 else "в пределах погрешности"))
        rows.append([_cell(name, "focus" if name == focus else ""), _cell(f"{counts[name] * 100 / total:.1f}%", bar=counts[name] * 100 / total),
                     _cell(f"± {(high - low) / 2 * 100:.1f}"), _cell(text, "" if name == focus or abs(z) < Z95 else ("down" if gap > 0 else "up"))])
    return order, counts, _table("Расстановка сил среди назвавших партию", ["Партия", "Доля", "Погрешность, п.п.", "Отрыв от «" + focus + "»"], rows)


def _neighbour_findings(order, counts, total, focus):
    findings = []
    if focus not in order:
        return findings
    place = order.index(focus)
    for other, word in ((order[place - 1] if place else None, "выше"), (order[place + 1] if place + 1 < len(order) else None, "ниже")):
        if not other:
            continue
        gap = (counts[other] - counts[focus]) * 100 / total
        z = _z_same_sample(counts[other], counts[focus], total)
        significant = abs(z) >= Z95
        findings.append(_find("notable" if not significant else "info",
                              f"Ближайшая партия {word}: {other}, разрыв {abs(gap):.1f} п.п. — "
                              + ("статистически значимо, место надёжно." if significant else
                                 "в пределах погрешности, за это место идёт борьба, порядок может поменяться.")))
    return findings


def _forecast_row(forecast, label):
    return next((r for r in forecast["rows"] if r["label"] == label), None) if forecast else None


def _classify(stat, refusal_all):
    if stat["n"] < DETAIL_MIN_N:
        return "small"
    if stat["index"] >= 115 and stat["z"] >= Z95:
        return "core"
    if stat["index"] <= 85 and stat["z"] <= -Z95:
        return "weak"
    if stat["refusal_rate"] >= refusal_all + 5 and stat["index"] >= 90:
        return "reserve"
    return "mid"


def _reserve(stats, dim):
    rows = []
    for s in sorted(stats, key=lambda s: -s["refusals"])[:6]:
        expected = s["refusals"] * s["rate"] / 100 if s["n"] >= DETAIL_MIN_N else None
        rows.append([_cell(s["label"]), _cell(_fmt_int(s["refusals"])), _cell(f"{s['refusal_rate']:.1f}%"),
                     _cell(f"{s['rate']:.1f}%" if s["n"] >= DETAIL_MIN_N else "—"),
                     _cell(f"≈ {_fmt_int(round(expected))}" if expected is not None else "—")])
    return _table(f"Где сосредоточены отказавшиеся ({DIM_TITLES[dim]})",
                  ["Сегмент", "Отказались", "Доля отказов", "Ставка партии", "Дали бы голосов"], rows,
                  "«Дали бы голосов» — сколько получила бы партия, если бы отказавшиеся голосовали, как остальные в этом сегменте.")


def focus_detail(scope, focus, settings, forecast):
    """The focus party's own picture: core, growth reserve and weak spots with significance."""
    named = [r for r in scope if r["answer"] not in SERVICE_ANSWERS]
    votes = sum(r["answer"] == focus for r in named)
    metrics = [{"label": "Назвали партию", "value": _fmt_int(len(named))}, {"label": "Голосов", "value": _fmt_int(votes)}]
    if not votes:
        return {"headline": {"level": "info", "text": f"«{focus}»: в выбранной области голосов нет."},
                "metrics": metrics, "sections": []}
    low, high = _wilson(votes, len(named))
    share = votes * 100 / len(named)
    order, counts, rank_table = _rank_table(named, focus)
    place = order.index(focus) + 1
    metrics = [{"label": "Доля среди назвавших", "value": f"{share:.1f}% ± {(high - low) / 2 * 100:.1f}"},
               {"label": "Место", "value": f"{place} из {len(order)}"}, {"label": "Голосов", "value": _fmt_int(votes)}]
    row = _forecast_row(forecast, focus)
    if row:
        metrics.append({"label": "Прогноз", "value": f"{row['forecast']}% ± {row['margin']:g}"})
    refusal_all = sum(r["answer"] == "Отказался отвечать" for r in scope) * 100 / len(scope)

    tables, everything = [], []
    for dim in ("age", "gender", "okrug", "time"):
        stats, _ = _segment_stats(scope, settings, dim, focus)
        everything += [dict(s, klass=_classify(s, refusal_all)) for s in stats]
        rows = []
        for s in sorted(stats, key=lambda s: (s["n"] < DETAIL_MIN_N, -s["index"])):
            text, cls = _verdict(s["z"], s["n"])
            rows.append([_cell(s["label"]), _cell(_fmt_int(s["n"])),
                         _cell(f"{s['rate']:.1f}% ± {s['margin']:.1f}" if s["n"] >= DETAIL_MIN_N else f"{s['rate']:.1f}%", bar=s["rate"]),
                         _cell(f"{s['index']:.0f}" if s["n"] >= DETAIL_MIN_N else "—", cls),
                         _cell(f"{s['contribution']:.0f}%"), _cell(f"{s['refusal_rate']:.0f}%"), _cell(text, cls)])
        tables.append(_table(f"Поддержка «{focus}» {DIM_TITLES[dim]}",
                             ["Сегмент", "Назвали", "Доля ± погр.", "Индекс", "Вклад", "Отказы", "Вывод"], rows,
                             "Вклад — какая часть всех голосов партии приходится на сегмент."))

    cross = defaultdict(lambda: [0, 0])
    for r in named:
        if r["age"] and r["gender"]:
            cell = cross[(r["age"], r["gender"])]
            cell[1] += 1
            cell[0] += r["answer"] == focus
    cross_rows = []
    for age in AGES:
        line = [_cell(age)]
        for gender in GENDER_LABELS:
            hit, n = cross[(age, gender)]
            line.append(_cell(f"{hit * 100 / n:.1f}% ({n})" if n >= DETAIL_MIN_N else f"мало данных ({n})",
                              "" if n < DETAIL_MIN_N else ("up" if hit * 100 / n >= share * 1.15 else "down" if hit * 100 / n <= share * 0.85 else ""),
                              bar=hit * 100 / n if n >= DETAIL_MIN_N else None))
        cross_rows.append(line)
    tables.append(_table(f"Возраст × пол: «{focus}»", ["Возраст", "Мужчины", "Женщины"], cross_rows,
                         "Зелёным — заметно выше средней доли партии, красным — заметно ниже."))

    core = sorted((s for s in everything if s["klass"] == "core"), key=lambda s: -s["index"])
    weak = sorted((s for s in everything if s["klass"] == "weak"), key=lambda s: s["index"])
    reserve = sorted((s for s in everything if s["klass"] == "reserve" and s["dim"] != "time"), key=lambda s: -s["refusals"] * s["rate"])
    findings = []
    for s in core[:3]:
        findings.append(_find("info", f"Ядро — {s['label']} ({DIM_TITLES[s['dim']]}): {s['rate']:.1f}% при среднем {share:.1f}%, индекс {s['index']:.0f}, "
                                      f"даёт {s['contribution']:.0f}% всех голосов партии."))
    for s in weak[:3]:
        findings.append(_find("notable", f"Слабое место — {s['label']} ({DIM_TITLES[s['dim']]}): {s['rate']:.1f}%, индекс {s['index']:.0f}, значимо ниже остальных."))
    for s in reserve[:2]:
        expected = s["refusals"] * s["rate"] / 100
        findings.append(_find("info", f"Резерв — {s['label']} ({DIM_TITLES[s['dim']]}): отказалось {_fmt_int(s['refusals'])} ({s['refusal_rate']:.0f}%), "
                                      f"ставка партии здесь {s['rate']:.1f}%; при ней отказавшиеся дали бы ≈ {_fmt_int(round(expected))} голосов."))
    if not (core or weak):
        findings.append(_find("info", "Значимых различий между группами нет: поддержка ровная. Для выводов по сегментам нужно больше анкет."))
    findings += _neighbour_findings(order, counts, len(named), focus)
    findings += _quality_findings(scope, focus)
    if row:
        findings.append(_find("info", f"Прогноз по всем данным: {row['forecast']}% (± {row['margin']:g} п.п.), поправка к доле ответивших {row['delta']:+g} п.п."
                                      + (f"; оценка ещё дрейфует ({row['trend']:+g} п.п. за последние 30% данных)." if row["trend"] is not None and abs(row["trend"]) > 1 else ".")))

    reserve_tables = [_reserve(_segment_stats(scope, settings, dim, focus)[0], dim) for dim in ("age", "okrug")]
    actions = []
    if core:
        actions.append(f"Удерживать ядро: {', '.join(s['label'] for s in core[:3])}. Проверить, на каких УИК и сменах держится результат.")
    if reserve:
        actions.append(f"Работать с резервом: {', '.join(s['label'] for s in reserve[:2])} — высокая доля отказов при средней или выше средней поддержке.")
    if weak:
        actions.append(f"Слабые места ({', '.join(s['label'] for s in weak[:3])}): гипотезы — сообщение не доходит до группы либо структурный барьер; "
                       "проверить на дополнительной выборке, причину по опросу определить нельзя.")
    if not actions:
        actions.append("Пока нет значимых сегментов для действий: накопить данные и вернуться к анализу.")

    headline_parts = [f"«{focus}»: {share:.1f}% среди назвавших, {place}-е место из {len(order)}."]
    if core:
        headline_parts.append(f"Сильнее всего — {core[0]['label']} (индекс {core[0]['index']:.0f}).")
    if weak:
        headline_parts.append(f"Слабее всего — {weak[0]['label']} (индекс {weak[0]['index']:.0f}).")
    if reserve:
        headline_parts.append(f"Главный резерв — {reserve[0]['label']}.")
    return {"headline": {"level": "notable" if weak else "info", "text": " ".join(headline_parts)}, "metrics": metrics,
            "sections": [_section("Что видно", findings), _section("Сегменты поддержки", tables=tables[:4]),
                         _section("Возраст и пол вместе", tables=[tables[4]]),
                         _section("Расстановка сил", tables=[rank_table]),
                         _section("Скрытый резерв в отказах", tables=reserve_tables),
                         _section("Что проверить и делать", [_find("info", a) for a in actions])]}


def _versus_table(dim, named, settings, focus, rival):
    groups = defaultdict(list)
    for r in named:
        groups[_dim_value(dim, r, settings)].append(r)
    rows, ahead, behind = [], [], []
    for raw in _dim_labels(dim):
        segment = groups.get(raw)
        if not segment:
            continue
        n = len(segment)
        mine, theirs = sum(r["answer"] == focus for r in segment), sum(r["answer"] == rival for r in segment)
        gap, z = (mine - theirs) * 100 / n, _z_same_sample(mine, theirs, n)
        if n < DETAIL_MIN_N:
            text, cls = "мало данных", "muted"
        elif abs(z) < Z95:
            text, cls = "в пределах погрешности", ""
        else:
            text, cls = ("опережаем", "up") if gap > 0 else ("уступаем", "down")
            (ahead if gap > 0 else behind).append((_dim_label(dim, raw), gap))
        rows.append([_cell(_dim_label(dim, raw)), _cell(f"{mine * 100 / n:.1f}%", bar=mine * 100 / n),
                     _cell(f"{theirs * 100 / n:.1f}%", bar=theirs * 100 / n), _cell(_pp(gap), cls), _cell(text, cls)])
    return _table(f"«{focus}» и «{rival}» {DIM_TITLES[dim]}", ["Сегмент", focus, rival, "Разница", "Вывод"], rows), ahead, behind


def rival_detail(scope, label, focus, settings, forecast):
    """A rival party seen from the focus party's position: head-to-head by segment and the rival's own base."""
    named = [r for r in scope if r["answer"] not in SERVICE_ANSWERS]
    counts = Counter(r["answer"] for r in named)
    if not counts[label]:
        return {"headline": {"level": "info", "text": f"«{label}»: в выбранной области голосов нет."},
                "metrics": [{"label": "Голосов", "value": "0"}], "sections": []}
    total = len(named)
    order = [name for name, _ in counts.most_common()]
    share_rival, share_focus = counts[label] * 100 / total, counts[focus] * 100 / total
    z = _z_same_sample(counts[label], counts[focus], total)
    gap = share_rival - share_focus
    metrics = [{"label": f"«{label}»", "value": f"{share_rival:.1f}%"}, {"label": f"«{focus}»", "value": f"{share_focus:.1f}%"},
               {"label": "Разница", "value": _pp(gap)}, {"label": "Место", "value": f"{order.index(label) + 1} из {len(order)}"}]
    tables, ahead, behind = [], [], []
    for dim in ("age", "gender", "okrug"):
        table, won, lost = _versus_table(dim, named, settings, focus, label)
        tables.append(table)
        ahead += [(f"{name} ({DIM_TITLES[dim]})", g) for name, g in won]
        behind += [(f"{name} ({DIM_TITLES[dim]})", g) for name, g in lost]
    base_tables = []
    for dim in ("age", "gender"):
        stats, _ = _segment_stats(scope, settings, dim, label)
        base_tables.append(_answer_table(dim, stats, label, False))
    findings = [_find("notable" if abs(z) >= Z95 else "info",
                      f"Общий разрыв: «{label}» {'впереди' if gap > 0 else 'позади'} «{focus}» на {abs(gap):.1f} п.п. — "
                      + ("статистически значимо." if abs(z) >= Z95 else "в пределах погрешности, различие может быть случайным."))]
    if behind:
        findings.append(_find("notable", "Уступаем значимо: " + ", ".join(f"{n} ({g:+.1f})" for n, g in sorted(behind, key=lambda x: x[1])[:4]) + "."))
    if ahead:
        findings.append(_find("info", "Опережаем значимо: " + ", ".join(f"{n} ({g:+.1f})" for n, g in sorted(ahead, key=lambda x: -x[1])[:4]) + "."))
    if not (ahead or behind):
        findings.append(_find("info", "Ни в одном сегменте разница между партиями не выходит за пределы погрешности."))
    findings += _quality_findings(scope, label)
    actions = []
    if behind:
        actions.append(f"Конкурент «{label}» перехватывает аудиторию в: {', '.join(n for n, _ in sorted(behind, key=lambda x: x[1])[:3])}. Изучить, чем он там привлекает, и сравнить сообщения.")
    if ahead:
        actions.append(f"Защищать преимущество над «{label}»: {', '.join(n for n, _ in sorted(ahead, key=lambda x: -x[1])[:3])}.")
    if not actions:
        actions.append("Значимых сегментных различий нет: партии делят аудиторию похоже, выводов о конкуренции пока делать нельзя.")
    return {"headline": {"level": "notable" if behind else "info",
                         "text": f"«{label}» {'опережает' if gap > 0 else 'отстаёт от'} «{focus}» на {abs(gap):.1f} п.п. ({'значимо' if abs(z) >= Z95 else 'в пределах погрешности'})."
                                 + (f" Значимо сильнее в: {', '.join(n for n, _ in sorted(behind, key=lambda x: x[1])[:2])}." if behind else "")},
            "metrics": metrics,
            "sections": [_section("Что видно", findings), _section("Прямое сравнение по сегментам", tables=tables),
                         _section(f"Кто голосует за «{label}»", tables=base_tables),
                         _section("Что проверить и делать", [_find("info", a) for a in actions])]}


def answer_detail(scope, label, focus, settings, forecast):
    """Refusals and spoiled ballots: who they are, whether a few shifts hold them, and what they hide for the focus party."""
    refusal = label == "Отказался отвечать"
    subset = [r for r in scope if r["answer"] == label]
    total = len(scope)
    metrics = [{"label": "Анкет", "value": _fmt_int(len(subset))}, {"label": "От всех анкет", "value": _pct(_share(len(subset), total))}]
    if not subset:
        return {"headline": {"level": "info", "text": f"«{label}»: таких анкет в выбранной области нет."}, "metrics": metrics, "sections": []}
    tables, findings, notable = [], [], []
    for dim in ("age", "gender", "okrug", "time"):
        stats, _ = _segment_stats(scope, settings, dim, label)
        tables.append(_answer_table(dim, stats, label, True))
        notable += [s for s in stats if s["n"] >= DETAIL_MIN_N and abs(s["z"]) >= Z95]
    for s in sorted(notable, key=lambda s: -abs(s["z"]))[:4]:
        findings.append(_find("notable", f"{s['label']} ({DIM_TITLES[s['dim']]}): {s['rate']:.1f}% против {_pct(_share(len(subset), total))} в целом — "
                                          + ("значимо чаще." if s["z"] > 0 else "значимо реже.")))
    if not notable:
        findings.append(_find("info", "Значимых различий между группами нет: доля распределена равномерно."))
    concentration = _shift_concentration(scope, label, service=True)
    findings += _quality_findings(scope, label, service=True)
    sections = [_section("Что видно", findings), _section(f"Профиль «{label}»", tables=tables)]
    actions = []
    if refusal:
        people = {}
        for r in scope:
            entry = people.setdefault(_shift_key(r), [(r["surname"] + " " + r["name"]).strip(), 0, 0])
            entry[2] += 1
            entry[1] += r["answer"] == label
        rows = []
        for name, hit, n in sorted((e for e in people.values() if e[2] >= 15), key=lambda e: -e[1] / e[2])[:6]:
            z = _z_prop(hit, n, len(subset) - hit, total - n)
            rows.append([_cell(name), _cell(f"{hit} из {n}"), _cell(f"{hit * 100 / n:.0f}%", bar=hit * 100 / n),
                         _cell("значимо чаще" if z >= Z95 else "значимо реже" if z <= -Z95 else "в пределах погрешности", "down" if z >= Z95 else "up" if z <= -Z95 else "")])
        if rows:
            sections.append(_section("Интервьюеры", tables=[_table("Отказы по интервьюерам (смены от 15 анкет)", ["Интервьюер", "Отказов", "Доля", "Вывод"], rows,
                                                                     "Сильный разброс говорит о манере опроса, а не только о респондентах.")]))
        reserve_tables = [_reserve(_segment_stats(scope, settings, dim, focus)[0], dim) for dim in ("age", "okrug")]
        sections.append(_section(f"Что скрывают отказы для «{focus}»", tables=reserve_tables))
        row = forecast["rows"][0] if forecast else None
        if row:
            findings.append(_find("info", f"В прогнозе отказавшихся распределяют внутри групп «округ × пол × возраст»: у лидера ({row['label']}) это меняет долю на "
                                          f"{row['with_refusals'] - row['answered']:+.1f} п.п., подробности на вкладке «Прогноз»."))
        if any(s["dim"] == "okrug" and s["z"] >= Z95 for s in notable):
            actions.append("Проверить округа с повышенной долей отказов: скрипт обращения, время и место опроса, состав интервьюеров.")
        if concentration and concentration["flag"]:
            actions.append("Отказы сосредоточены в нескольких сменах: разобрать эти смены отдельно, возможно, различается манера опроса.")
        actions.append(f"Отказавшиеся в сегментах, где сильна «{focus}», — главный резерв: см. таблицы выше.")
    else:
        findings.append(_find("info", "Испорченные бюллетени обычно связаны с протестным голосованием или ошибками заполнения; причину по анкетам определить нельзя."))
        actions.append("Если доля растёт в одном сегменте, проверить понятность бюллетеня и инструкции этой группе, а также честность заполнения анкет.")
    sections.append(_section("Что проверить и делать", [_find("info", a) for a in actions]))
    return {"headline": {"level": "notable" if notable else "info",
                         "text": f"«{label}»: {_pct(_share(len(subset), total))} анкет ({_fmt_int(len(subset))})."
                                 + (f" Заметнее всего отличается: {max(notable, key=lambda s: abs(s['z']))['label']}." if notable else "")},
            "metrics": metrics, "sections": sections}


def group_analysis(scope, kind, key, focus, settings, forecast):
    """A demographic, territorial or hourly group compared with everyone else, seen from the focus party."""
    if kind == "gender":
        belongs, title = (lambda r: r["gender"] == key), {"Мужской": "Мужчины", "Женский": "Женщины"}[key]
        control = lambda r: (TIK_TO_OKRUG.get(r["tik"], ""), r["age"])
    elif kind == "age":
        belongs, title = (lambda r: r["age"] == key), f"Возраст {key}"
        control = lambda r: (TIK_TO_OKRUG.get(r["tik"], ""), r["gender"])
    elif kind == "okrug":
        belongs, title = (lambda r: TIK_TO_OKRUG.get(r["tik"]) == key), f"Округ {key}"
        control = lambda r: (r["age"], r["gender"])
    else:
        belongs = lambda r: (m := parse_moment(r["created_at"])) is not None and m.astimezone(settings.zone).hour == int(key)
        title, control = f"Час {key}:00–{key}:59", (lambda r: (r["age"], r["gender"]))
    subset = [r for r in scope if belongs(r)]
    rest = [r for r in scope if not belongs(r)]
    named_subset = [r for r in subset if r["answer"] not in SERVICE_ANSWERS]
    named_rest = [r for r in rest if r["answer"] not in SERVICE_ANSWERS]
    refusals = lambda rows_: sum(r["answer"] == "Отказался отвечать" for r in rows_)
    refusal_here, refusal_rest = _share(refusals(subset), len(subset)), _share(refusals(rest), len(rest))
    metrics = [{"label": "Анкет", "value": _fmt_int(len(subset))}, {"label": "От всех анкет", "value": _pct(_share(len(subset), len(scope)))},
               {"label": "Отказы", "value": f"{_pct(refusal_here)} (у остальных {_pct(refusal_rest)})"}]
    if not subset:
        return {"headline": {"level": "info", "text": f"{title}: анкет в выбранной области нет."}, "metrics": metrics, "sections": []}

    reliability = []
    shifts = Counter(_shift_key(r) for r in subset)
    reliability.append(_find("info", f"В группе {_fmt_int(len(subset))} анкет из {len({r['precinct_id'] for r in subset})} УИК, смен интервьюеров: {len(shifts)}."))
    if len(subset) < DETAIL_MIN_N:
        reliability.append(_find("warning", f"Анкет мало ({len(subset)}): выводы ниже ненадёжны."))
    elif len(shifts) <= 2 or shifts.most_common(1)[0][1] * 100 / len(subset) >= 60:
        reliability.append(_find("warning", "Группа держится на одной-двух сменах: различия могут отражать работу интервьюера или участка, а не саму группу."))
    z_ref = _z_prop(refusals(subset), len(subset), refusals(rest), len(rest))
    if len(subset) >= DETAIL_MIN_N and abs(z_ref) >= Z95:
        reliability.append(_find("notable", f"Отказы {_pct(refusal_here)} против {_pct(refusal_rest)} у остальных — значимо {'чаще' if z_ref > 0 else 'реже'}."))

    named_scope = [r for r in scope if r["answer"] not in SERVICE_ANSWERS]
    counts_scope = Counter(r["answer"] for r in named_scope)
    counts_here, counts_rest = Counter(r["answer"] for r in named_subset), Counter(r["answer"] for r in named_rest)
    top = [name for name, _ in counts_scope.most_common(7)]
    party_rows, party_findings = [], []
    for name in top:
        here, there = _share(counts_here[name], len(named_subset)), _share(counts_rest[name], len(named_rest))
        z = _z_prop(counts_here[name], len(named_subset), counts_rest[name], len(named_rest))
        text, cls = _verdict(z, len(named_subset))
        party_rows.append([_cell(name, "focus" if name == focus else ""), _cell(f"{here:.1f}%", bar=here), _cell(f"{there:.1f}%"),
                           _cell(_pp(here - there) if len(named_subset) >= DETAIL_MIN_N else "—", cls), _cell(text, cls)])
        if len(named_subset) >= DETAIL_MIN_N and abs(z) >= Z95:
            party_findings.append((abs(z), _find("notable", f"«{name}»: {here:.1f}% в группе против {there:.1f}% у остальных ({_pp(here - there)}) — значимо.")))
    party_findings = [f for _, f in sorted(party_findings, key=lambda x: -x[0])[:3]]

    position, actions = [], []
    order = [name for name, _ in counts_here.most_common()]
    votes_focus = counts_here[focus]
    if len(named_subset) >= DETAIL_MIN_N and focus in order:
        share = votes_focus * 100 / len(named_subset)
        rest_share = _share(counts_rest[focus], len(named_rest))
        position.append(_find("info", f"«{focus}» в группе: {share:.1f}%, {order.index(focus) + 1}-е место из {len(order)} (у остальных {rest_share:.1f}%)."))
        position += _neighbour_findings(order, counts_here, len(named_subset), focus)
        observed, expected, z = _standardize(named_subset, named_scope, focus, control)
        if len(named_subset) >= DETAIL_MIN_N:
            if abs(z) < Z95:
                position.append(_find("info", f"С учётом состава группы ожидаемая доля {expected:.1f}%, фактическая {observed:.1f}%: расхождение с областью объясняется составом, "
                                              "а не особенностями самой группы."))
            else:
                position.append(_find("notable", f"Даже с учётом состава группы ожидаемая доля {expected:.1f}%, фактическая {observed:.1f}% ({_pp(observed - expected)}, значимо): "
                                                 "это свойство самой группы, а не её состава."))
                actions.append(f"В группе «{title}» «{focus}» {'сильнее' if observed > expected else 'слабее'} ожидаемого при таком составе: искать причину в самой группе (тематика, каналы, местный фон).")
    elif focus in counts_here or counts_scope[focus]:
        position.append(_find("info", f"Партию назвали только {len(named_subset)} {_people(len(named_subset))}: для выводов о позиции «{focus}» мало данных."))

    rate_focus = votes_focus / len(named_subset) if len(named_subset) >= DETAIL_MIN_N else None
    reserve = []
    if rate_focus is not None and refusals(subset):
        expected_votes = refusals(subset) * rate_focus
        reserve.append(_find("info", f"Отказалось {_fmt_int(refusals(subset))} ({_pct(refusal_here)}). При ставке «{focus}» в группе ({rate_focus * 100:.1f}%) это ≈ {_fmt_int(round(expected_votes))} голосов."))
        if refusal_here >= refusal_rest + 5:
            actions.append(f"Высокая доля отказов в «{title}»: скрытый резерв для «{focus}» ≈ {_fmt_int(round(expected_votes))} голосов, стоит выяснить причины отказов.")

    mixes = []
    for dim in ("age", "gender", "okrug", "time"):
        if (dim == kind) or (kind == "hour" and dim == "time"):
            continue
        with_value = [r for r in subset if _dim_value(dim, r, settings)]
        scope_with = [r for r in scope if _dim_value(dim, r, settings)]
        if len(with_value) < DETAIL_MIN_N:
            continue
        here, there = Counter(_dim_value(dim, r, settings) for r in with_value), Counter(_dim_value(dim, r, settings) for r in scope_with)
        for raw in _dim_labels(dim):
            diff = _share(here[raw], len(with_value)) - _share(there[raw], len(scope_with))
            mixes.append((abs(diff), dim, raw, _share(here[raw], len(with_value)), _share(there[raw], len(scope_with)), diff))
    mix_rows = [[_cell(_dim_label(dim, raw)), _cell(DIM_TITLES[dim]), _cell(f"{here:.1f}%"), _cell(f"{there:.1f}%"), _cell(_pp(diff), "up" if diff > 0 else "down")]
                for _, dim, raw, here, there, diff in sorted(mixes, reverse=True)[:6] if abs(diff) >= 5]

    since_2021 = None
    if kind == "okrug" and BASELINE and key in BASELINE["okrugs"] and len(named_subset) >= DETAIL_MIN_N:
        reference = baseline_shares(key)
        since_rows = []
        for name in top:
            hits, now = counts_here[name], counts_here[name] * 100 / len(named_subset)
            ref = reference.get(name)
            if ref is None:
                since_rows.append([_cell(name, "focus" if name == focus else ""), _cell("нет"), _cell(f"{now:.1f}%", bar=now), _cell("—"), _cell("в 2021 не участвовала", "muted")])
                continue
            low, high = _wilson(hits, len(named_subset))
            outside = not (low * 100 <= ref <= high * 100)
            cls = ("up" if now > ref else "down") if outside else ""
            verdict = ("значимо выше" if now > ref else "значимо ниже") if outside else "в пределах погрешности"
            since_rows.append([_cell(name, "focus" if name == focus else ""), _cell(f"{ref:.1f}%"), _cell(f"{now:.1f}%", bar=now), _cell(_pp(now - ref), cls), _cell(verdict, cls)])
            if name == focus:
                position.append(_find("notable" if outside else "info",
                                      f"К 2021 году: «{focus}» было {ref:.1f}%, в опросе {now:.1f}% ({_pp(now - ref)}, "
                                      + ("значимо)." if outside else "в пределах погрешности).")))
        since_2021 = _table("Опрос против итогов 2021 в этом округе", ["Партия", "2021", "Опрос", "Разница", "Вывод"], since_rows,
                            "2021 — итоги партийных списков (ЦИК), пересчитанные на те же партии; список партий и способ голосования отличаются, сравнение ориентировочное.")

    sections = [_section("Надёжность", reliability),
                _section("Партии в группе и вне её", party_findings, [_table("Расклад среди назвавших партию", ["Партия", "В группе", "Вне группы", "Разница", "Вывод"], party_rows)]),
                _section(f"Позиция «{focus}»", position)]
    if since_2021:
        sections.append(_section("К итогам 2021", tables=[since_2021]))
    if mix_rows:
        sections.append(_section("Чем отличается состав группы", tables=[_table("Заметные отличия состава от области в целом", ["Категория", "Срез", "В группе", "В области", "Разница"], mix_rows)]))
    if reserve:
        sections.append(_section("Скрытый резерв", reserve))
    if not actions:
        actions.append("Существенных отличий, требующих действий, в группе нет: наблюдать за динамикой.")
    sections.append(_section("Что проверить и делать", [_find("info", a) for a in actions]))
    lead = order[0] if order and len(named_subset) >= DETAIL_MIN_N else None
    headline = f"{title}: {_fmt_int(len(subset))} анкет ({_pct(_share(len(subset), len(scope)))})."
    if lead:
        headline += f" Лидирует {lead} ({_pct(_share(counts_here[lead], len(named_subset)))})."
    return {"headline": {"level": "warning" if any(f["level"] == "warning" for f in reliability) else "info", "text": headline},
            "metrics": metrics, "sections": sections}


def dashboard_detail(values, settings, kind, key, focus=DEFAULT_FOCUS, requested_day=None, requested_okrug=None,
                     requested_tik=None, requested_precinct=None, base="prev"):
    if kind == "kpi":
        return kpi_detail(values, settings, key, requested_day, requested_okrug, requested_tik, requested_precinct, base)
    rows = parse_sheet_rows(values)
    _dates, _day, _day_rows, scope = scope_rows(rows, settings, requested_day, requested_okrug,
                                                 requested_tik, requested_precinct)
    forecast = forecast_shares(rows, territory_weights(settings))
    role = ""
    if kind == "party":
        if key == focus:
            body, role = focus_detail(scope, focus, settings, forecast), "Ваша партия"
        elif key in SERVICE_ANSWERS:
            body, role = answer_detail(scope, key, focus, settings, forecast), "Отказы и испорченные" if key == "Отказался отвечать" else "Испорченные бюллетени"
        else:
            body, role = rival_detail(scope, key, focus, settings, forecast), "Конкурент"
        title = key
    else:
        body = group_analysis(scope, kind, key, focus, settings, forecast)
        title = {"gender": {"Мужской": "Мужчины", "Женский": "Женщины"}.get(key, key), "age": f"Возраст {key}",
                 "okrug": f"Округ {key}", "hour": f"Час {key}:00–{key}:59"}[kind]
        role = "Группа"
    return {"kind": kind, "key": key, "focus": focus, "title": title, "role": role,
            "subtitle": f"В выбранной области: {_fmt_int(len(scope))} анкет", **body}


KPI_KEYS = ("total", "refusals", "spoiled", "interviewers", "uiks", "tiks")
KPI_TITLES = {"total": "Всего анкет", "refusals": "Отказались", "spoiled": "Испортили бюллетень", "interviewers": "Интервьюеры",
              "uiks": "УИК с данными", "tiks": "ТИК с данными"}
KPI_UNITS = {"total": "анкет", "refusals": "отказов", "spoiled": "испорченных бюллетеней", "interviewers": "интервьюеров",
             "uiks": "УИК с данными", "tiks": "ТИК с данными"}
KPI_FIRST_HOUR = 8


def _kpi_value(key, rows):
    if key == "total":
        return len(rows)
    if key == "refusals":
        return sum(r["answer"] == "Отказался отвечать" for r in rows)
    if key == "spoiled":
        return sum(r["answer"] == "Испортил бюллетень" for r in rows)
    if key == "interviewers":
        return len({person_id(r) for r in rows})
    if key == "uiks":
        return len({r["precinct_id"] for r in rows})
    return len({r["tik"] for r in rows if r["tik"]})


def _local_time(row, settings):
    moment = parse_moment(row["created_at"])
    return moment.astimezone(settings.zone) if moment else None


def _minutes(row, settings):
    moment = _local_time(row, settings)
    return moment.hour * 60 + moment.minute if moment else None


KPI_BASES = ("prev", "avg2")


def kpi_windows(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct, base="prev"):
    """The selected day against the day before it (base "prev") or the average of the two days before it (base "avg2").
    While the selected day is still running, earlier days are cut at the same clock time."""
    dates, selected, _day_rows, _scope = scope_rows(rows, settings, requested_day, None, None, None)
    day = selected if selected != ALL_DAYS else (dates[0] if dates else None)
    if not day or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None

    def visible(row):
        return ((not requested_okrug or TIK_TO_OKRUG.get(row["tik"]) == requested_okrug) and (not requested_tik or row["tik"] == requested_tik)
                and (not requested_precinct or row["precinct_id"] == requested_precinct))
    current = [r for r in rows if r["day"] == day and visible(r)]
    cutoff = None
    if dates and day == dates[0]:
        stamps = [m for m in (_minutes(r, settings) for r in rows if r["day"] == day) if m is not None]
        cutoff = max(stamps) if stamps else None
    baselines = []
    for back in range(1, (2 if base == "avg2" else 1) + 1):
        earlier = (date.fromisoformat(day) - timedelta(days=back)).isoformat()
        full = [r for r in rows if r["day"] == earlier and visible(r)]
        if full:  # a day without any data in this scope does not drag the average down
            same_time = [r for r in full if cutoff is None or (_minutes(r, settings) is not None and _minutes(r, settings) <= cutoff)]
            baselines.append({"day": earlier, "rows": full, "same_time": same_time})
    return {"day": day, "current": current, "baselines": baselines, "cutoff": cutoff, "has_previous": bool(baselines), "base": base}


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _mean(key, baselines, field, keep=None):
    if not baselines:
        return None
    values = [_kpi_value(key, [r for r in b[field] if keep is None or keep(r)]) for b in baselines]
    return round(sum(values) / len(values), 1)


def _change(today, before):
    if before in (None, 0):
        return None
    return round((today - before) * 100 / before, 1)


def _num(value):
    return _fmt_int(value) if float(value).is_integer() else f"{value:.1f}"


def _signed_num(value):
    text = _num(abs(value))
    return ("+" if value > 0 else "-" if value < 0 else "+") + text


def kpi_compare(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct, base="prev"):
    """Small per-card comparison for the summary tiles."""
    windows = kpi_windows(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct, base)
    if not windows:
        return None
    result = {"day": windows["day"], "base": base, "days": len(windows["baselines"]), "has_previous": windows["has_previous"],
              "cutoff": _clock(windows["cutoff"]) if windows["cutoff"] is not None else None, "values": {}}
    for key in KPI_KEYS:
        today = _kpi_value(key, windows["current"])
        before = _mean(key, windows["baselines"], "same_time")
        result["values"][key] = {"today": today, "before": before, "change": _change(today, before)}
    return result


def _short_date(iso):
    return f"{iso[8:10]}.{iso[5:7]}"


def kpi_detail(values, settings, key, requested_day=None, requested_okrug=None, requested_tik=None, requested_precinct=None, base="prev"):
    rows = parse_sheet_rows(values)
    title, unit = KPI_TITLES[key], KPI_UNITS[key]
    role = "Сравнение со средним за 2 дня" if base == "avg2" else "Сравнение с прошлым днём"
    windows = kpi_windows(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct, base)
    if not windows:
        return {"kind": "kpi", "key": key, "title": title, "role": role, "subtitle": "Данных пока нет",
                "headline": {"level": "info", "text": "Данных пока нет, сравнивать не с чем."}, "metrics": [], "sections": []}
    day, cutoff, baselines = windows["day"], windows["cutoff"], windows["baselines"]
    when = f"к {_clock(cutoff)}" if cutoff is not None else "за день"
    today = _kpi_value(key, windows["current"])
    if base == "avg2":
        wanted = [(date.fromisoformat(day) - timedelta(days=n)).isoformat() for n in (2, 1)]
        label = "Среднее за 2 дня" if len(baselines) == 2 else "Прошлый день (среднего нет)"
        span = "–".join(_short_date(d) for d in wanted)
    else:
        label = "Вчера"
        span = _short_date((date.fromisoformat(day) - timedelta(days=1)).isoformat())
    subtitle = f"{_short_date(day)} против {span}" + (f", {when}" if cutoff is not None else "")
    if not baselines:
        return {"kind": "kpi", "key": key, "title": title, "role": role, "subtitle": subtitle,
                "headline": {"level": "info", "text": f"За {span} данных в выбранной области нет: сейчас {_fmt_int(today)} {unit}, сравнивать не с чем."},
                "metrics": [{"label": f"Сегодня {when}", "value": _fmt_int(today)}], "sections": []}
    before = _mean(key, baselines, "same_time")
    full_before = _mean(key, baselines, "rows")
    change = _change(today, before)
    delta = round(today - before, 1)
    word = "больше" if delta > 0 else "меньше" if delta < 0 else "столько же"
    against = "в среднем за 2 дня" if len(baselines) == 2 else "за " + _short_date(baselines[0]["day"]) if base == "avg2" else "вчера"
    if base == "avg2" and len(baselines) == 1:
        against += " (второго дня нет)"
    text = (f"{when.capitalize()} {_fmt_int(today)} {unit} против {_num(before)} {against}" if cutoff is not None
            else f"За {_short_date(day)} {_fmt_int(today)} {unit} против {_num(before)} {against}")
    text += f": на {abs(change):g}% {word}." if change is not None and delta else "."
    level = "notable" if change is not None and abs(change) >= 25 else "info"
    metrics = [{"label": f"Сегодня {when}", "value": _fmt_int(today)}, {"label": f"{label} {when}" if cutoff is not None else label, "value": _num(before)},
               {"label": "Изменение", "value": _signed_num(delta) + (f" ({change:+g}%)" if change is not None else "")},
               {"label": f"{label} за весь день", "value": _num(full_before)}]
    if key in ("refusals", "spoiled"):
        now_total = len(windows["current"])
        was_total = _mean("total", baselines, "same_time")
        was_value = _mean(key, baselines, "same_time")
        metrics += [{"label": "Доля сегодня", "value": _pct(today * 100 / now_total) if now_total else "—"},
                    {"label": f"Доля: {label.lower()}", "value": _pct(was_value * 100 / was_total) if was_total else "—"}]

    def cumulative(field, hour, keep=None):
        return _mean(key, baselines, field, lambda r: (_minutes(r, settings) or 0) // 60 <= hour and (keep is None or keep(r)))
    last_hour = cutoff // 60 if cutoff is not None else 20
    hour_rows = []
    for hour in range(KPI_FIRST_HOUR, max(KPI_FIRST_HOUR, last_hour) + 1):
        now_v = _kpi_value(key, [r for r in windows["current"] if (_minutes(r, settings) or 0) // 60 <= hour])
        was_v = cumulative("rows", hour)
        diff = round(now_v - was_v, 1)
        hour_rows.append([_cell(f"до {hour + 1:02d}:00"), _cell(_num(now_v), bar=now_v), _cell(_num(was_v), bar=was_v),
                          _cell(_signed_num(diff), "up" if diff > 0 else "down" if diff < 0 else "")])
    okrug_rows = []
    for okrug in sorted(OKRUGS):
        in_okrug = lambda r, o=okrug: TIK_TO_OKRUG.get(r["tik"]) == o
        now_v = _kpi_value(key, [r for r in windows["current"] if in_okrug(r)])
        was_v = _mean(key, baselines, "same_time", in_okrug)
        okrug_rows.append((okrug, now_v, was_v))
    okrug_rows.sort(key=lambda item: (item[1] - item[2], item[0]))
    findings = []
    gains = [item for item in reversed(okrug_rows) if item[1] > item[2]][:3]
    drops = [item for item in okrug_rows if item[1] < item[2]][:3]
    if gains:
        findings.append(_find("info", "Больше, чем в сравнении: " + ", ".join(f"округ {o} ({_signed_num(round(n - w, 1))})" for o, n, w in gains) + "."))
    if drops:
        findings.append(_find("notable", "Меньше, чем в сравнении: " + ", ".join(f"округ {o} ({_signed_num(round(n - w, 1))})" for o, n, w in drops) + "."))
    days_rows = [[_cell(f"{_short_date(day)} (сегодня)"), _cell(_fmt_int(today)), _cell(_fmt_int(today))]]
    for b in baselines:
        days_rows.append([_cell(_short_date(b["day"])), _cell(_fmt_int(_kpi_value(key, b["same_time"]))), _cell(_fmt_int(_kpi_value(key, b["rows"])))])
    tables = [_table("По дням", ["День", f"Значение {when}", "За весь день"], days_rows,
                     "Среднее считается по этим дням; день без данных в выбранной области в среднее не входит." if base == "avg2" else ""),
              _table("Нарастающим итогом по часам", ["Час", "Сегодня", label, "Разница"], hour_rows,
                     "Значения на конец часа. Если день ещё идёт, показаны часы до последней анкеты."),
              _table("По округам", ["Округ", "Сегодня", label, "Разница"],
                     [[_cell(f"Округ {o}"), _cell(_fmt_int(n)), _cell(_num(w)), _cell(_signed_num(round(n - w, 1)), "up" if n > w else "down" if n < w else "")]
                      for o, n, w in sorted(okrug_rows, key=lambda item: item[0])],
                     "Значения сравнения — к тому же времени суток." if cutoff is not None else "")]
    return {"kind": "kpi", "key": key, "title": title, "role": role, "subtitle": subtitle,
            "headline": {"level": level, "text": text}, "metrics": metrics,
            "sections": [_section("Динамика", findings, tables[:2]), _section("Территории", [], tables[2:])]}


ROSTER_ACTIVE_MINUTES = 20  # last anketa no older than this: working
ROSTER_PAUSE_MINUTES = 60  # older than active but within this: on a pause; beyond: silent
ROSTER_SHIFT_END_HOUR = 20  # after voting closes nobody is "silent"
ROSTER_SALT = b"exit-poll-roster-v1"
# PBKDF2 hash of the roster password, so the password itself is not stored in Git; ROSTER_CODE in the environment overrides it.
ROSTER_HASH = "afa8cfd8ad0b9e85d4d0e2965b913345eac477cb103dc7e4457011675d0dd142"


def roster_code_ok(candidate, override=""):
    if override:
        return hmac.compare_digest(candidate.encode(), override.encode())
    digest = hashlib.pbkdf2_hmac("sha256", candidate.encode(), ROSTER_SALT, 200_000).hex()
    return hmac.compare_digest(digest, ROSTER_HASH)


def person_key(surname, name):
    """Order- and case-insensitive identity, so 'Шадура Матвей' and 'Матвей Шадура' are one person."""
    tokens = f"{surname} {name}".lower().replace("ё", "е").split()
    return " ".join(sorted(tokens))


def person_id(row):
    """One person, whichever shifts or days they appear on."""
    return person_key(row["surname"], row["name"]) or row["shift_id"]


def build_roster(values, settings, now=None):
    """Who worked yesterday and today, who has not appeared today yet, split by okrug."""
    moment = now or datetime.now(settings.zone)
    today = moment.date().isoformat()
    yesterday = (moment.date() - timedelta(days=1)).isoformat()
    people = {}
    for row in parse_sheet_rows(values):
        if row["day"] not in (today, yesterday) or not (row["surname"] or row["name"]):
            continue
        entry = people.setdefault(person_key(row["surname"], row["name"]), {"names": Counter(), "days": {today: Counter(), yesterday: Counter()},
                                                                            "tiks": {today: Counter(), yesterday: Counter()}, "last": {}, "first": {}})
        entry["names"][f'{row["surname"]} {row["name"]}'.strip()] += 1
        entry["days"][row["day"]][row["precinct"]] += 1
        entry["tiks"][row["day"]][row["tik"]] += 1
        stamp = parse_moment(row["created_at"])
        if stamp:
            stamp = stamp.astimezone(settings.zone)
            entry["last"][row["day"]] = max(stamp, entry["last"].get(row["day"], stamp))
            entry["first"][row["day"]] = min(stamp, entry["first"].get(row["day"], stamp))
    today_uik = defaultdict(list)
    for entry in people.values():
        for precinct in entry["days"][today]:
            today_uik[precinct].append(entry["names"].most_common(1)[0][0])
    okrugs = {okrug: {"okrug": okrug, "people": []} for okrug in sorted(OKRUGS)}
    for entry in people.values():
        was, is_now = sum(entry["days"][yesterday].values()), sum(entry["days"][today].values())
        tik = (entry["tiks"][yesterday] or entry["tiks"][today]).most_common(1)[0][0]
        if tik not in TIK_TO_OKRUG:
            continue
        name = entry["names"].most_common(1)[0][0]
        precinct = (entry["days"][yesterday] or entry["days"][today]).most_common(1)[0][0]
        status = "both" if was and is_now else "absent" if was else "new"
        minutes = activity = None
        if is_now and today in entry["last"]:
            minutes = max(0, int((moment - entry["last"][today]).total_seconds() // 60))
            activity = ("done" if moment.hour >= ROSTER_SHIFT_END_HOUR else "active" if minutes <= ROSTER_ACTIVE_MINUTES
                        else "pause" if minutes <= ROSTER_PAUSE_MINUTES else "silent")
        replaced_by = [other for other in today_uik.get(precinct, []) if other != name] if status == "absent" else []
        okrugs[TIK_TO_OKRUG[tik]]["people"].append({
            "name": name, "tik": tik, "precinct": precinct, "status": status, "yesterday": was, "today": is_now,
            "last_yesterday": entry["last"][yesterday].strftime("%H:%M") if yesterday in entry["last"] else None,
            "first_today": entry["first"][today].strftime("%H:%M") if today in entry["first"] else None,
            "last_today": entry["last"][today].strftime("%H:%M") if today in entry["last"] else None,
            "minutes_since": minutes, "activity": activity,
            "replaced_by": replaced_by})
    order = {"absent": 0, "new": 1, "both": 2}
    result = []
    for item in okrugs.values():
        item["people"].sort(key=lambda p: (order[p["status"]] if p["status"] == "absent" else 1,
                                            -p["yesterday"] if p["status"] == "absent" else -(p["minutes_since"] or 0), p["name"]))
        counts = Counter(p["status"] for p in item["people"])
        result.append({"okrug": item["okrug"], "yesterday": counts["absent"] + counts["both"], "today": counts["new"] + counts["both"],
                       "absent": counts["absent"], "new": counts["new"], "both": counts["both"], "people": item["people"]})
    totals = {key: sum(item[key] for item in result) for key in ("yesterday", "today", "absent", "new", "both")}
    for level in ("active", "pause", "silent"):
        totals[level] = sum(1 for item in result for p in item["people"] if p["activity"] == level)
    for item in result:
        item["silent"] = sum(1 for p in item["people"] if p["activity"] == "silent")
        item["pause"] = sum(1 for p in item["people"] if p["activity"] == "pause")
    return {"today": today, "yesterday": yesterday, "generated_at": moment.isoformat(), "totals": totals, "thresholds": {"active": ROSTER_ACTIVE_MINUTES, "pause": ROSTER_PAUSE_MINUTES}, "okrugs": result}


def build_map_data(day_rows):
    """Per-TIK answer counts for the choropleth (day filter only, like the other territory blocks)."""
    counts = defaultdict(Counter)
    totals = Counter()
    for row in day_rows:
        if row["tik"] in TIK_TO_OKRUG:
            totals[row["tik"]] += 1
            counts[row["tik"]][row["answer"]] += 1
    turnout = {}
    if BASELINE:
        for okrug, entry in BASELINE["okrugs"].items():
            turnout[okrug] = {"turnout": round(entry["issued"] * 100 / entry["voters"], 1), "voters": entry["voters"]}
    return {"tiks": [{"tik": tik, "okrug": TIK_TO_OKRUG[tik], "n": totals[tik],
                      "answers": {label: counts[tik][label] for label in (*FORECAST_PARTIES, *SERVICE_ANSWERS) if counts[tik][label]}}
                     for tik in sorted(TIK_TO_OKRUG, key=lambda t: (TIK_TO_OKRUG[t], t))],
            "turnout_2021": turnout}


def _swing_cell(hits, n, reference):
    cell = {"n": n, "y2021": reference, "poll": _share(hits, n) if n else None, "delta": None, "significant": False}
    if n >= DETAIL_MIN_N and reference is not None:
        low, high = _wilson(hits, n)
        cell["delta"] = round(hits * 100 / n - reference, 1)
        cell["significant"] = not (low * 100 <= reference <= high * 100)
    return cell


def build_swing(okrug_rows):
    """Poll share minus the 2021 result, for each party and okrug (both among the same parties)."""
    if not BASELINE:
        return None
    okrug_ids = sorted(OKRUGS)
    named = {o: Counter(r["answer"] for r in okrug_rows.get(o, []) if r["answer"] in FORECAST_PARTIES) for o in okrug_ids}
    region = sum(named.values(), Counter())
    region_ref = baseline_shares()
    rows = []
    for label in FORECAST_PARTIES:
        cells = [_swing_cell(named[o][label], sum(named[o].values()), baseline_shares(o).get(label)) for o in okrug_ids]
        rows.append({"label": label, "y2021": region_ref.get(label), "cells": cells,
                     "region": _swing_cell(region[label], sum(region.values()), region_ref.get(label))})
    rows.sort(key=lambda r: (r["y2021"] is None, -(r["y2021"] or 0)))
    return {"year": BASELINE["year"], "okrugs": [{"okrug": o, "n": sum(named[o].values())} for o in okrug_ids], "rows": rows,
            "region_n": sum(region.values())}


PARTY_COMPARE_MAX_DAYS = 5


def build_party_compare(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct):
    """Share of all surveys per answer on the other days, for bars next to the selected period's bars.
    A specific day is compared with every other day; "all days" is broken down into its days. Same okrug/TIK/UIK filters."""
    dates, selected, _day_rows, _scope = scope_rows(rows, settings, requested_day, None, None, None)
    others = [d for d in dates if d != selected]
    others = sorted(others, reverse=True)[:PARTY_COMPARE_MAX_DAYS]
    labels = [label for party_id, label in PARTIES if party_id != "2"]
    days = []
    for day in others:
        scoped = [r for r in rows if r["day"] == day
                  and (not requested_okrug or TIK_TO_OKRUG.get(r["tik"]) == requested_okrug)
                  and (not requested_tik or r["tik"] == requested_tik)
                  and (not requested_precinct or r["precinct_id"] == requested_precinct)]
        if not scoped:
            continue
        counts = Counter(r["answer"] for r in scoped)
        days.append({"day": day, "total": len(scoped),
                     "shares": {label: round(counts[label] * 100 / len(scoped), 1) for label in labels}})
    average = None
    if days:
        average = {"days": len(days), "shares": {label: round(sum(d["shares"][label] for d in days) / len(days), 1) for label in labels}}
    return {"selected": selected, "days": days, "average": average}


def forecast_for_day(rows, settings, requested_day):
    """The same forecast, built only from the selected day's surveys (the latest day when "all days" is chosen)."""
    dates, selected, _day_rows, _scope = scope_rows(rows, settings, requested_day, None, None, None)
    day = selected if selected != ALL_DAYS else (dates[0] if dates else None)
    if not day:
        return {"day": None, "forecast": None}
    return {"day": day, "forecast": forecast_shares([r for r in rows if r["day"] == day], territory_weights(settings))}


def dashboard_snapshot(values, settings, requested_day=None, requested_okrug=None,
                        requested_tik=None, requested_precinct=None, statuses=None):
    """Build a small, privacy-conscious aggregate from rows in Google Sheets."""
    rows = parse_sheet_rows(values)

    dates, selected_day, day_rows, filtered = scope_rows(rows, settings, requested_day, requested_okrug,
                                                         requested_tik, requested_precinct)

    answers = Counter(row["answer"] for row in filtered)
    genders = Counter(row["gender"] for row in filtered if row["gender"])
    ages = Counter(row["age"] for row in filtered if row["age"])
    people = {person_id(row) for row in filtered}
    total = len(filtered)

    catalog_tiks = sorted({p.get("tik", "") for p in settings.precincts if p.get("tik")})

    okrug_rows = defaultdict(list)
    for row in day_rows:
        okrug = TIK_TO_OKRUG.get(row["tik"])
        if okrug:
            okrug_rows[okrug].append(row)
    okrug_stats = []
    for okrug in sorted(OKRUGS):
        items = okrug_rows.get(okrug, [])
        okrug_stats.append({
            "okrug": okrug,
            "total": len(items),
            "refusals": sum(x["answer"] == "Отказался отвечать" for x in items),
            "spoiled": sum(x["answer"] == "Испортил бюллетень" for x in items),
            "tiks": len({x["tik"] for x in items}),
            "interviewers": len({person_id(x) for x in items}),
        })
    okrug_stats.sort(key=lambda item: (-item["total"], item["okrug"]))

    new_people_by_okrug = []
    for okrug in sorted(OKRUGS):
        items = okrug_rows.get(okrug, [])
        okrug_total = len(items)
        count = sum(x["answer"] == "Новые люди" for x in items)
        new_people_by_okrug.append({
            "label": f"Округ {okrug}", "count": count,
            "percent": round(count * 100 / okrug_total, 1) if okrug_total else 0,
        })

    new_people_by_age = []
    for age in AGES:
        group = [row for row in filtered if row["age"] == age]
        votes = sum(row["answer"] == "Новые люди" for row in group)
        new_people_by_age.append({
            "label": age, "count": votes, "total": len(group),
            "percent": round(votes * 100 / len(group), 1) if group else 0,
        })

    tik_stats = []
    if requested_okrug:
        tik_rows = defaultdict(list)
        for row in okrug_rows.get(requested_okrug, []):
            tik_rows[row["tik"]].append(row)
        for tik in sorted(OKRUGS.get(requested_okrug, [])):
            items = tik_rows.get(tik, [])
            tik_stats.append({
                "tik": tik,
                "total": len(items),
                "refusals": sum(x["answer"] == "Отказался отвечать" for x in items),
                "spoiled": sum(x["answer"] == "Испортил бюллетень" for x in items),
                "uiks": len({x["precinct_id"] for x in items}),
                "interviewers": len({person_id(x) for x in items}),
            })
        tik_stats.sort(key=lambda item: (-item["total"], item["tik"]))

    uik_stats = []
    if requested_tik:
        by_uik = defaultdict(list)
        for row in [r for r in day_rows if r["tik"] == requested_tik]:
            by_uik[row["precinct_id"]].append(row)
        for precinct in [p for p in settings.precincts if p.get("tik") == requested_tik]:
            items = by_uik.get(precinct["id"], [])
            uik_stats.append({
                "id": precinct["id"], "label": precinct["label"], "total": len(items),
                "refusals": sum(x["answer"] == "Отказался отвечать" for x in items),
                "spoiled": sum(x["answer"] == "Испортил бюллетень" for x in items),
                "interviewers": len({person_id(x) for x in items}),
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

    service_answers = ("Испортил бюллетень", "Отказался отвечать")
    okrug_answers = {okrug: Counter(row["answer"] for row in okrug_rows.get(okrug, [])) for okrug in OKRUGS}
    heat_rows = []
    for party_id, label in PARTIES:
        if party_id == "2":
            continue
        cells = []
        for okrug in sorted(OKRUGS):
            okrug_total = len(okrug_rows.get(okrug, []))
            count = okrug_answers[okrug][label]
            cells.append({"count": count, "percent": round(count * 100 / okrug_total, 1) if okrug_total else 0})
        heat_rows.append({"label": label, "total": sum(cell["count"] for cell in cells), "cells": cells})
    party_okrug = {
        "okrugs": [{"okrug": okrug, "total": len(okrug_rows.get(okrug, []))} for okrug in sorted(OKRUGS)],
        "rows": sorted((r for r in heat_rows if r["label"] not in service_answers), key=lambda r: -r["total"])
                + [r for r in heat_rows if r["label"] in service_answers],
    }

    age_answers = {age: Counter(row["answer"] for row in filtered if row["age"] == age) for age in AGES}
    age_rows = []
    for party_id, label in PARTIES:
        if party_id != "2":
            age_rows.append({"label": label, "total": answers[label],
                             "cells": [{"count": age_answers[age][label]} for age in AGES]})
    party_age = {
        "ages": [{"age": age, "total": sum(age_answers[age].values())} for age in AGES],
        "overall": total,
        "rows": sorted((r for r in age_rows if r["label"] not in service_answers), key=lambda r: -r["total"])
                + [r for r in age_rows if r["label"] in service_answers],
    }

    statuses = statuses or {}
    severity_rank = {"high": 0, "medium": 1}
    anomaly_items = []
    for item in detect_anomalies(rows, settings):
        if selected_day != ALL_DAYS and item["day"] != selected_day:
            continue
        if requested_okrug and item["okrug"] != requested_okrug:
            continue
        if requested_tik and item["tik"] != requested_tik:
            continue
        if requested_precinct and item["precinct_id"] != requested_precinct:
            continue
        saved = statuses.get(item["id"], {})
        item.update(status=saved.get("status", "open"), note=saved.get("note", ""),
                    updated_at=saved.get("updated_at", ""))
        anomaly_items.append(item)
    anomaly_items.sort(key=lambda a: a["interviewer"])
    anomaly_items.sort(key=lambda a: a["day"], reverse=True)
    anomaly_items.sort(key=lambda a: (a["status"] != "open", severity_rank[a["severity"]]))
    anomalies = {"items": anomaly_items[:300],
                 "open": sum(a["status"] == "open" for a in anomaly_items),
                 "closed": sum(a["status"] != "open" for a in anomaly_items)}

    party_order = [label for party_id, label in PARTIES if party_id != "2"]
    parties = [{"label": label, "count": answers[label],
                "percent": round(answers[label] * 100 / total, 1) if total else 0} for label in party_order]
    parties.sort(key=lambda item: -item["count"])
    result = {
        "generated_at": datetime.now(settings.zone).isoformat(),
        "selected_day": selected_day, "selected_okrug": requested_okrug or "",
        "selected_tik": requested_tik or "",
        "selected_precinct": requested_precinct or "", "available_dates": dates,
        "filters": {"okrugs": sorted(OKRUGS),
                    "tiks": (sorted(OKRUGS.get(requested_okrug, [])) if requested_okrug else []),
                    "precincts": ([{"id": p["id"], "label": p["label"]}
                                   for p in settings.precincts if p.get("tik") == requested_tik]
                                  if requested_tik else [])},
        "summary": {"total": total, "refusals": answers["Отказался отвечать"],
                    "spoiled": answers["Испортил бюллетень"], "interviewers": len(people),
                    "uiks": len({row["precinct_id"] for row in filtered}),
                    "tiks": len({row["tik"] for row in filtered if row["tik"]})},
        "parties": parties,
        "genders": [{"label": label, "count": genders[label],
                     "percent": round(genders[label] * 100 / total, 1) if total else 0} for label in ("Мужской", "Женский")],
        "ages": [{"label": label, "count": ages[label],
                  "percent": round(ages[label] * 100 / total, 1) if total else 0} for label in AGES],
        "hours": [{"hour": f"{hour:02d}:00", "count": hours[hour]} for hour in range(7, 24)],
        "new_people_by_okrug": new_people_by_okrug, "new_people_by_age": new_people_by_age, "party_okrug": party_okrug, "party_age": party_age,
        "swing": build_swing(okrug_rows), "map": build_map_data(day_rows),
        "compare": {base: kpi_compare(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct, base) for base in KPI_BASES},
        "forecast": forecast_shares(rows, territory_weights(settings)),
        "forecast_day": forecast_for_day(rows, settings, requested_day),
        "party_compare": build_party_compare(rows, settings, requested_day, requested_okrug, requested_tik, requested_precinct),
        "anomalies": anomalies,
        "okrug_stats": okrug_stats, "tik_stats": tik_stats, "uik_stats": uik_stats,
        "interviewers": interviewers[:100], "recent": recent,
    }
    return result


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

    async def refresher():
        """Keep one ready-made dashboard snapshot in memory; the slow Sheets read and the maths run off the event loop."""
        while True:
            try:
                await asyncio.to_thread(refresh_dashboard)
            except Exception as exc:
                LOG.warning("Dashboard refresh failed (%s); serving the previous snapshot", type(exc).__name__)
            await asyncio.sleep(settings.dashboard_refresh_seconds)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(exporter())]
        if settings.spreadsheet:
            tasks.append(asyncio.create_task(refresher()))
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Exit Poll", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    failures = {}
    # Dashboard state: raw sheet values and anomaly statuses refreshed in the background, plus finished views per data version.
    state = {"values": None, "statuses": {}, "statuses_ok": True, "loaded_at": 0.0, "version": 0, "views": {}}
    state_lock = threading.Lock()  # short critical sections only
    refresh_lock = threading.Lock()  # one Sheets read at a time
    compute_lock = threading.Lock()  # one heavy calculation at a time, so intake threads keep their CPU share

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

    def refresh_dashboard(warm=True):
        """Read the sheet and statuses, publish them, and pre-build the default views. Runs in a worker thread."""
        with refresh_lock:
            values = read_sheet(settings)
            try:
                statuses, ok = read_anomaly_statuses(settings), True
            except Exception as exc:
                LOG.warning("Anomaly statuses read failed (%s)", type(exc).__name__)
                statuses, ok = None, False
            with state_lock:
                state["values"] = values
                if statuses is not None:
                    state["statuses"] = statuses
                state.update(statuses_ok=ok, loaded_at=time.monotonic(), version=state["version"] + 1, views={})
        if not warm:  # called from inside a request that already holds compute_lock
            return
        for compute in (lambda: dashboard_view(None, None, None, None), roster_view):
            try:
                compute()
            except Exception as exc:
                LOG.warning("Dashboard warm-up failed (%s)", type(exc).__name__)

    def sheet_values():
        if state["values"] is None:  # cold start or the first refresh has not finished yet
            try:
                refresh_dashboard(warm=False)
            except Exception as exc:
                LOG.warning("Dashboard Sheets read failed (%s)", type(exc).__name__)
                raise HTTPException(502, "Не удалось прочитать Google Таблицу")
        return state["values"]

    def cached_view(key, compute):
        with compute_lock:
            with state_lock:
                version, hit = state["version"], state["views"].get(key)
            if hit is not None:
                return hit
            result = compute()
            with state_lock:
                if state["version"] == version:
                    if len(state["views"]) >= 64:
                        state["views"].clear()
                    state["views"][key] = result
            return result

    def dashboard_view(day, okrug, tik, precinct):
        def compute():
            snapshot = dashboard_snapshot(sheet_values(), settings, day, okrug, tik, precinct, dict(state["statuses"]))
            snapshot["anomalies"]["statuses_ok"] = state["statuses_ok"]
            return snapshot
        return cached_view(("data", day, okrug, tik, precinct), compute)

    def roster_view():
        return cached_view(("roster",), lambda: build_roster(sheet_values(), settings))

    def data_age():
        return round(time.monotonic() - state["loaded_at"]) if state["loaded_at"] else None

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return Response("Cross-origin request rejected", status_code=403)
            limit = MAX_BATCH_BYTES if request.url.path == "/api/surveys/batch" else 32768
            if int(request.headers.get("content-length", "0")) > limit:
                return Response("Request too large", status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/dashboard."):
            response.headers["Cache-Control"] = "no-cache"  # revalidate, so a cached old script never runs with the new page
        return response

    @app.get("/api/health")
    async def health():
        return {"ok": True}  # no database, no sheet, no calculations

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

    def check_scope(day, okrug, tik, precinct):
        if day and day != ALL_DAYS and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(422, "Неверная дата")
        if okrug and okrug not in OKRUGS:
            raise HTTPException(422, "Неизвестный округ")
        if tik and tik not in {p.get("tik") for p in settings.precincts}:
            raise HTTPException(422, "Неизвестный ТИК")
        if okrug and tik and TIK_TO_OKRUG.get(tik) != okrug:
            raise HTTPException(422, "ТИК не относится к выбранному округу")
        if precinct and precinct not in {p["id"] for p in settings.precincts}:
            raise HTTPException(422, "Неизвестный УИК")

    def roster_authorized(request: Request):
        token = request.cookies.get("exit_poll_roster_session", "")
        try:
            expiry, signature = token.split(".")
            if int(expiry) >= time.time() and hmac.compare_digest(signature, signed("roster:" + expiry)):
                return
        except (ValueError, TypeError):
            pass
        raise HTTPException(401, "Введите пароль сводки")

    @app.post("/api/dashboard/roster/login", dependencies=[Depends(dashboard_authorized)])
    def roster_login(body: DashboardLogin, request: Request, response: Response):
        now = time.time()
        key = "roster:" + (request.client.host if request.client else "unknown")
        count, since = failures.get(key, (0, now))
        if now - since >= 300:
            count, since = 0, now
        if count >= 10:
            raise HTTPException(429, "Повторите через 5 минут")
        if not roster_code_ok(body.code, settings.roster_code):
            failures[key] = (count + 1, since)
            raise HTTPException(401, "Неверный пароль")
        failures.pop(key, None)
        expiry = str(int(now + 12 * 3600))
        response.set_cookie("exit_poll_roster_session", expiry + "." + signed("roster:" + expiry),
                            httponly=True, secure=settings.production, samesite="strict", max_age=12 * 3600)
        return {"ok": True}

    @app.get("/api/dashboard/roster", dependencies=[Depends(dashboard_authorized), Depends(roster_authorized)])
    def roster_data():
        return {**roster_view(), "data_age": data_age()}

    @app.get("/api/dashboard/detail", dependencies=[Depends(dashboard_authorized)])
    def dashboard_detail_view(kind: str, key: str, focus: str = DEFAULT_FOCUS, day: str | None = None,
                              okrug: str | None = None, tik: str | None = None, precinct: str | None = None, base: str = "prev"):
        check_scope(day, okrug, tik, precinct)
        if base not in KPI_BASES:
            raise HTTPException(422, "Неизвестная база сравнения")
        valid = {"party": {label for pid, label in PARTIES if pid != "2"}, "gender": set(GENDER_LABELS),
                 "age": set(AGES), "okrug": set(OKRUGS), "hour": {f"{hour:02d}" for hour in range(24)}, "kpi": set(KPI_KEYS)}
        if kind not in DETAIL_KINDS or key not in valid[kind]:
            raise HTTPException(422, "Неизвестная графа")
        if focus not in valid["party"] - set(SERVICE_ANSWERS):
            raise HTTPException(422, "Неизвестная партия")
        base = base if kind == "kpi" else "prev"
        return cached_view(("detail", kind, key, focus, day, okrug, tik, precinct, base),
                           lambda: dashboard_detail(sheet_values(), settings, kind, key, focus, day, okrug, tik, precinct, base))

    @app.get("/api/dashboard/data", dependencies=[Depends(dashboard_authorized)])
    def dashboard_data(day: str | None = None, okrug: str | None = None,
                        tik: str | None = None, precinct: str | None = None):
        check_scope(day, okrug, tik, precinct)
        return {**dashboard_view(day, okrug, tik, precinct), "data_age": data_age()}

    @app.post("/api/dashboard/anomalies/status", dependencies=[Depends(dashboard_authorized)])
    def anomaly_status(body: AnomalyStatus):
        if not settings.spreadsheet:
            raise HTTPException(503, "Google Таблица не подключена")
        try:
            known = {item["id"]: item for item in detect_anomalies(parse_sheet_rows(sheet_values()), settings)}
        except HTTPException:
            raise
        except Exception as exc:
            LOG.warning("Dashboard Sheets read failed (%s)", type(exc).__name__)
            raise HTTPException(502, "Не удалось прочитать Google Таблицу")
        item = known.get(body.id)
        if item is None:
            raise HTTPException(404, "Аномалия не найдена: данные могли измениться")
        note = "".join(c for c in body.note if ord(c) >= 32).strip()
        record = {"id": body.id, "status": body.status, "note": note,
                  "updated_at": datetime.now(settings.zone).isoformat(timespec="seconds"),
                  "interviewer": item["interviewer"], "rule": item["rule"], "day": item["day"],
                  "tik": item["tik"], "precinct": item["precinct"]}
        try:
            write_anomaly_status(settings, record)
        except Exception as exc:
            LOG.warning("Anomaly status write failed (%s)", type(exc).__name__)
            raise HTTPException(502, "Не удалось сохранить статус в Google Таблице")
        with state_lock:
            state["statuses"] = {**state["statuses"], body.id: {"status": body.status, "note": note, "updated_at": record["updated_at"]}}
            state["views"] = {}  # the anomaly block embeds statuses
        return {"ok": True}

    def check_survey(body):
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

    def store_survey(db, body):
        payload = canonical(body)
        db.execute("INSERT OR IGNORE INTO surveys(id,payload,received_at) VALUES(?,?,?)",
                   (str(body.id), payload, datetime.now(timezone.utc).isoformat()))
        row = db.execute("SELECT payload,exported FROM surveys WHERE id=?", (str(body.id),)).fetchone()
        if row["payload"] != payload:
            raise HTTPException(409, "Анкета с этим ID уже содержит другие ответы")
        return {"id": str(body.id), "saved": True, "sheets_synced": bool(row["exported"])}

    @app.post("/api/surveys", dependencies=[Depends(authorized)])
    def submit(body: Survey):
        check_survey(body)
        with connect(settings) as db:
            return store_survey(db, body)

    @app.post("/api/surveys/batch", dependencies=[Depends(authorized)])
    def submit_batch(body: SurveyBatch):
        """Up to 50 surveys in one request and one database connection. Every item gets its own verdict, so the phone
        can mark the accepted ones as sent and keep or reject the rest exactly as with single submits."""
        results = []
        with connect(settings) as db:
            for raw in body.surveys:
                item_id = raw.get("id") if isinstance(raw.get("id"), str) else None
                try:
                    survey = Survey.model_validate(raw)
                    check_survey(survey)
                    results.append(store_survey(db, survey))
                except ValidationError:
                    results.append({"id": item_id, "saved": False, "status": 422, "error": "Анкета заполнена неверно"})
                except HTTPException as exc:
                    results.append({"id": item_id, "saved": False, "status": exc.status_code, "error": exc.detail})
        return {"results": results, "saved": sum(1 for r in results if r["saved"])}

    def register_sms_attempt(hashed):
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

    @app.post("/api/sms/request", dependencies=[Depends(authorized)])
    async def request_sms(body: SmsRequest):
        if not settings.sms_url or not settings.sms_token:
            raise HTTPException(503, "SMS-сервис ещё не подключён")
        hashed = signed("phone:" + body.phone)
        await asyncio.to_thread(register_sms_attempt, hashed)  # SQLite must not block the event loop
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
