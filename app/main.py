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


def _forecast_core(prepared, uik_counts, detail=False):
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
    if uik_counts:
        vectors = {tik: [by_tik[tik][i] + alloc_tik[tik][i] for i in range(count_parties)]
                   for tik in set(by_tik) | set(alloc_tik)}
        okrug_totals = defaultdict(lambda: [0.0] * count_parties)
        for tik, vector in vectors.items():
            if tik in TIK_TO_OKRUG:
                for i in range(count_parties):
                    okrug_totals[TIK_TO_OKRUG[tik]][i] += vector[i]
        okrug_share = {okrug: _blend(vector, with_refusals, SHRINK_OKRUG) for okrug, vector in okrug_totals.items()}
        weight_total = sum(uik_counts.values())
        accumulated = [0.0] * count_parties
        for tik, weight in uik_counts.items():
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


def forecast_shares(rows, uik_counts=None):
    """Forecast of the final split among valid ballots, from all survey rows (independent of dashboard filters).

    Steps: respondents -> refusers spread over okrug x gender x age cells -> territories weighted by their
    number of UIKs (TIKs without data borrow their okrug's estimate) -> flow diagnostics (how the estimate
    moved as data arrived) -> uncertainty from a delete-a-group jackknife over UIKs (cluster design) combined
    with sensitivity to refusers voting differently.
    """
    prepared = _prepare_forecast_rows(rows)
    fingerprint = (hash(tuple((t[0], t[2], t[3], t[4], t[5], t[6]) for t in prepared)),
                   hash(tuple(sorted((uik_counts or {}).items()))))
    if fingerprint in _FORECAST_CACHE:
        return _FORECAST_CACHE[fingerprint]
    full = _forecast_core(prepared, uik_counts, detail=True)
    if full is None:
        return None
    parties, count_parties = FORECAST_PARTIES, len(FORECAST_PARTIES)

    groups = {}
    for item in prepared:
        groups.setdefault(item[5], zlib.crc32(item[5].encode()) % JACKKNIFE_GROUPS)
    replicates = []
    for group in sorted(set(groups.values())):
        part = _forecast_core([item for item in prepared if groups[item[5]] != group], uik_counts)
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
        part = full if fraction == 1.0 else _forecast_core(ordered[:size], uik_counts)
        if part:
            moment = ordered[size - 1][6]
            history.append({"fraction": fraction, "n": size, "at": moment.isoformat() if moment else "",
                            "final": [round(value * 100, 1) for value in part["final"]]})

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
                  "tiks_with_data": full["tiks_with_data"], "tiks_total": len(uik_counts or {}),
                  "covered_share": round(full["covered"] * 100), "days": sorted({r["day"] for r in rows if r["day"]}),
                  "last_at": max(moments).isoformat() if moments else ""},
        "history": {"points": [{k: v for k, v in point.items() if k != "final"} for point in history],
                    "series": [{"label": parties[i], "values": [point["final"][i] for point in history]}
                               for i in order[:4]]},
        "ages": ages,
        "deg": {**DEG_CONTEXT, "scenarios": scenarios},
    }
    if len(_FORECAST_CACHE) >= 4:
        _FORECAST_CACHE.clear()
    _FORECAST_CACHE[fingerprint] = result
    return result


SERVICE_ANSWERS = ("Испортил бюллетень", "Отказался отвечать")
INSIGHT_MIN_N = 30


def _fmt_int(value):
    return f"{int(value):,}".replace(",", "\u00a0")


def _pct(value):
    return f"{round(value, 1):g}%"


def build_insights(snap):
    """Plain-language observations from fixed rules over the current dashboard payload (no model involved)."""
    items = []

    def add(section, level, text):
        items.append({"section": section, "level": level, "text": text})

    summary = snap["summary"]
    total = summary["total"]
    if not total:
        return [{"section": "Итоги", "level": "info",
                 "text": "В выбранной области пока нет анкет. Анализ появится, когда придут данные."}]
    named = [p for p in snap["parties"] if p["label"] not in SERVICE_ANSWERS]
    named_total = sum(p["count"] for p in named)
    refusal_share = summary["refusals"] * 100 / total
    add("Итоги", "warning" if refusal_share >= 40 else "info",
        f"Собрано {_fmt_int(total)} анкет: партию назвали {_fmt_int(named_total)} ({_pct(named_total * 100 / total)}), "
        f"отказались {_fmt_int(summary['refusals'])} ({_pct(refusal_share)}), "
        f"испортили бюллетень {_fmt_int(summary['spoiled'])}.")
    if refusal_share >= 40:
        add("Итоги", "warning", "Доля отказов высокая: доли партий среди ответивших могут быть смещены, "
                                "смотрите вкладку «Прогноз».")
    add("Итоги", "info", f"Данные по {summary['tiks']} ТИК и {summary['uiks']} УИК, смен интервьюеров: "
                         f"{summary['interviewers']}.")

    if named_total >= INSIGHT_MIN_N:
        ranked = sorted(named, key=lambda p: -p["count"])
        lead, second = ranked[0], ranked[1]
        lead_share, second_share = lead["count"] * 100 / named_total, second["count"] * 100 / named_total
        gap = round(lead_share - second_share, 1)
        add("Партии", "notable" if gap < 3 else "info",
            f"Среди назвавших партию лидирует {lead['label']}: {_pct(lead_share)} ({_fmt_int(lead['count'])}), "
            f"затем {second['label']} — {_pct(second_share)}. Отрыв {gap:g} п.п."
            + (" Гонка близка." if gap < 3 else ""))
        add("Партии", "info", f"Три первые партии набирают {_pct(sum(p['count'] for p in ranked[:3]) * 100 / named_total)} ответивших.")
    else:
        add("Партии", "info", f"Партию назвали только {named_total} человек: для выводов по партиям мало данных.")

    forecast = snap.get("forecast")
    if forecast and forecast["scope"]["respondents"] >= INSIGHT_MIN_N:
        first, second = forecast["rows"][0], forecast["rows"][1]
        add("Прогноз", "info",
            f"Прогноз по всем данным: {first['label']} {_pct(first['forecast'])} (± {first['margin']:g} п.п.), "
            f"{second['label']} {_pct(second['forecast'])}. Поправка к доле ответивших у лидера: {first['delta']:+g} п.п.")
        if first["trend"] is not None:
            drifting = abs(first["trend"]) > 1
            add("Прогноз", "notable" if drifting else "info",
                (f"Оценка ещё дрейфует: за последние 30% данных лидер сдвинулся на {first['trend']:+g} п.п."
                 if drifting else f"Оценка стабильна: за последние 30% данных лидер сдвинулся на {first['trend']:+g} п.п."))
        scope = forecast["scope"]
        add("Прогноз", "notable" if scope["covered_share"] < 60 else "info",
            f"Прогноз опирается на данные по {scope['tiks_with_data']} из {scope['tiks_total']} ТИК "
            f"({scope['covered_share']}% участков области). Электронное голосование опросом не охвачено.")

    genders = {g["label"]: g["count"] for g in snap["genders"]}
    men, women = genders.get("Мужской", 0), genders.get("Женский", 0)
    if men + women >= INSIGHT_MIN_N:
        more, less, word = (women, men, "женщин") if women >= men else (men, women, "мужчин")
        add("Демография", "info", f"Среди опрошенных больше {word}: {_pct(more * 100 / (men + women))} против {_pct(less * 100 / (men + women))}.")
    biggest_age = max(snap["ages"], key=lambda a: a["count"])
    if biggest_age["count"]:
        add("Демография", "info", f"Самая многочисленная возрастная группа — {biggest_age['label']}: {_pct(biggest_age['percent'])} анкет.")
    by_age = snap["party_age"]
    age_names = [a["age"] for a in by_age["ages"]]
    refused_row = next((r for r in by_age["rows"] if r["label"] == "Отказался отвечать"), None)
    if refused_row:
        rates = [(age_names[i], refused_row["cells"][i]["count"] * 100 / by_age["ages"][i]["total"])
                 for i in range(len(age_names)) if by_age["ages"][i]["total"] >= INSIGHT_MIN_N]
        if len(rates) >= 2:
            high, low = max(rates, key=lambda r: r[1]), min(rates, key=lambda r: r[1])
            if high[1] - low[1] >= 5:
                add("Демография", "notable", f"Чаще всего отказываются в группе {high[0]} ({_pct(high[1])}), реже всего — в группе {low[0]} ({_pct(low[1])}).")
    age_party_rows = [r for r in by_age["rows"] if r["label"] not in SERVICE_ANSWERS]
    named_by_age = [sum(r["cells"][i]["count"] for r in age_party_rows) for i in range(len(age_names))]
    for row in age_party_rows[:2]:
        shares = [(age_names[i], row["cells"][i]["count"] * 100 / named_by_age[i])
                  for i in range(len(age_names)) if named_by_age[i] >= INSIGHT_MIN_N]
        if len(shares) >= 2:
            high, low = max(shares, key=lambda r: r[1]), min(shares, key=lambda r: r[1])
            if high[1] - low[1] >= 8:
                add("Демография", "notable", f"{row['label']}: сильнее всего в группе {high[0]} ({_pct(high[1])} назвавших), слабее всего — в группе {low[0]} ({_pct(low[1])}).")

    okrug_stats = snap["okrug_stats"]
    active = [o for o in okrug_stats if o["total"]]
    if active:
        biggest = max(active, key=lambda o: o["total"])
        add("Территории", "info",
            f"Данные есть по {len(active)} из {len(okrug_stats)} округов; больше всего анкет в округе {biggest['okrug']} "
            f"({_fmt_int(biggest['total'])}, {_pct(biggest['total'] * 100 / sum(o['total'] for o in okrug_stats))} всех).")
    heat = snap["party_okrug"]
    okrug_ids = [o["okrug"] for o in heat["okrugs"]]
    heat_parties = [r for r in heat["rows"] if r["label"] not in SERVICE_ANSWERS]
    named_by_okrug = [sum(r["cells"][i]["count"] for r in heat_parties) for i in range(len(okrug_ids))]
    if heat_parties:
        row = heat_parties[0]
        shares = [(okrug_ids[i], row["cells"][i]["count"] * 100 / named_by_okrug[i])
                  for i in range(len(okrug_ids)) if named_by_okrug[i] >= INSIGHT_MIN_N]
        if len(shares) >= 2:
            high, low = max(shares, key=lambda r: r[1]), min(shares, key=lambda r: r[1])
            if high[1] - low[1] >= 10:
                add("Территории", "notable", f"{row['label']}: сильнее всего в округе {high[0]} ({_pct(high[1])} назвавших), слабее всего — в округе {low[0]} ({_pct(low[1])}).")
    refused_row = next((r for r in heat["rows"] if r["label"] == "Отказался отвечать"), None)
    if refused_row:
        rates = [(okrug_ids[i], refused_row["cells"][i]["count"] * 100 / heat["okrugs"][i]["total"])
                 for i in range(len(okrug_ids)) if heat["okrugs"][i]["total"] >= INSIGHT_MIN_N]
        if len(rates) >= 2:
            high, low = max(rates, key=lambda r: r[1]), min(rates, key=lambda r: r[1])
            if high[1] - low[1] >= 10:
                add("Территории", "notable", f"Больше всего отказов в округе {high[0]} ({_pct(high[1])}), меньше всего — в округе {low[0]} ({_pct(low[1])}).")

    new_by_okrug = snap["new_people_by_okrug"]
    if sum(x["count"] for x in new_by_okrug):
        top = max(new_by_okrug, key=lambda x: x["count"])
        totals = {o["okrug"]: o["total"] for o in heat["okrugs"]}
        eligible = [x for x in new_by_okrug if totals.get(x["label"].split()[-1], 0) >= INSIGHT_MIN_N]
        text = f"За «Новых людей» больше всего голосов в округе {top['label'].split()[-1]} ({_fmt_int(top['count'])})"
        if eligible:
            best = max(eligible, key=lambda x: x["percent"])
            text += f"; выше всего доля в округе {best['label'].split()[-1]} ({_pct(best['percent'])} анкет округа)"
        add("Новые люди", "info", text + ".")
    if named_total >= INSIGHT_MIN_N:
        order = [p["label"] for p in sorted(named, key=lambda p: -p["count"])]
        if "Новые люди" in order:
            place = order.index("Новые люди") + 1
            share = next(p["count"] for p in named if p["label"] == "Новые люди") * 100 / named_total
            add("Новые люди", "info", f"«Новые люди» — {place}-е место среди назвавших партию ({_pct(share)}).")
    age_groups = [g for g in snap["new_people_by_age"] if g["total"] >= 20]
    if age_groups and any(g["count"] for g in age_groups):
        best, worst = max(age_groups, key=lambda g: g["percent"]), min(age_groups, key=lambda g: g["percent"])
        add("Новые люди", "notable" if best["percent"] - worst["percent"] >= 8 else "info",
            f"Чаще всего за «Новых людей» голосует группа {best['label']}: {_pct(best['percent'])} её анкет "
            f"({best['count']} из {best['total']}); реже всего — {worst['label']} ({_pct(worst['percent'])}).")

    counts = [h["count"] for h in snap["hours"]]
    if sum(counts):
        peak = max(range(len(counts)), key=lambda i: counts[i])
        nonzero = [i for i, c in enumerate(counts) if c]
        first_hour, last_hour = snap["hours"][nonzero[0]]["hour"], snap["hours"][nonzero[-1]]["hour"][:2]
        add("Поток", "info", f"Анкеты поступали с {first_hour} до {last_hour}:59, пик — {snap['hours'][peak]['hour']} ({_fmt_int(counts[peak])} анкет).")
        strong = [i for i, c in enumerate(counts) if c >= counts[peak] * 0.5]
        after = counts[strong[-1] + 1:nonzero[-1] + 1]
        if after and all(c <= counts[peak] * 0.25 for c in after):
            add("Поток", "notable", f"После {snap['hours'][strong[-1]]['hour'][:2]}:59 поток заметно снизился: не выше четверти от пика.")

    people = snap["interviewers"]
    if people:
        lead = people[0]
        add("Интервьюеры и качество", "info", f"Больше всех анкет у {lead['name']}: {_fmt_int(lead['total'])} ({_pct(lead['total'] * 100 / total)} от всех).")
        rated = [(p, p["refusals"] * 100 / p["total"]) for p in people if p["total"] >= 15]
        if len(rated) >= 3:
            high, low = max(rated, key=lambda r: r[1]), min(rated, key=lambda r: r[1])
            if high[1] - low[1] >= 30:
                add("Интервьюеры и качество", "notable", f"Доля отказов сильно различается по интервьюерам: от {_pct(low[1])} до {_pct(high[1])} ({high[0]['name']}).")
    anomalies = snap["anomalies"]
    if anomalies["open"]:
        open_items = [a for a in anomalies["items"] if a["status"] == "open"]
        high_count = sum(1 for a in open_items if a["severity"] == "high")
        common, occurrences = Counter(a["title"] for a in open_items).most_common(1)[0]
        add("Интервьюеры и качество", "warning" if high_count else "notable",
            f"Открыто аномалий: {anomalies['open']}" + (f", из них высоких: {high_count}" if high_count else "")
            + f". Чаще всего: «{common}» ({occurrences}).")
    else:
        add("Интервьюеры и качество", "info", "Открытых аномалий нет.")
    return items


def dashboard_snapshot(values, settings, requested_day=None, requested_okrug=None,
                        requested_tik=None, requested_precinct=None, statuses=None):
    """Build a small, privacy-conscious aggregate from rows in Google Sheets."""
    rows = parse_sheet_rows(values)

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

    answers = Counter(row["answer"] for row in filtered)
    genders = Counter(row["gender"] for row in filtered if row["gender"])
    ages = Counter(row["age"] for row in filtered if row["age"])
    shifts = {row["shift_id"] or f'{row["surname"]}|{row["name"]}' for row in filtered}
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
            "interviewers": len({x["shift_id"] or f'{x["surname"]}|{x["name"]}' for x in items}),
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
                "interviewers": len({x["shift_id"] or f'{x["surname"]}|{x["name"]}' for x in items}),
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
                    "spoiled": answers["Испортил бюллетень"], "interviewers": len(shifts),
                    "uiks": len({row["precinct_id"] for row in filtered}),
                    "tiks": len({row["tik"] for row in filtered if row["tik"]})},
        "parties": parties,
        "genders": [{"label": label, "count": genders[label],
                     "percent": round(genders[label] * 100 / total, 1) if total else 0} for label in ("Мужской", "Женский")],
        "ages": [{"label": label, "count": ages[label],
                  "percent": round(ages[label] * 100 / total, 1) if total else 0} for label in AGES],
        "hours": [{"hour": f"{hour:02d}:00", "count": hours[hour]} for hour in range(7, 24)],
        "new_people_by_okrug": new_people_by_okrug, "new_people_by_age": new_people_by_age, "party_okrug": party_okrug, "party_age": party_age,
        "forecast": forecast_shares(rows, Counter(p["tik"] for p in settings.precincts if p.get("tik"))),
        "anomalies": anomalies,
        "okrug_stats": okrug_stats, "tik_stats": tik_stats, "uik_stats": uik_stats,
        "interviewers": interviewers[:100], "recent": recent,
    }
    result["insights"] = build_insights(result)
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
    anomaly_cache = {"at": 0.0, "values": {}, "ok": True}
    anomaly_lock = threading.Lock()

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

    def cached_anomaly_statuses():
        with anomaly_lock:
            if time.monotonic() - anomaly_cache["at"] >= 25:
                try:
                    anomaly_cache.update(values=read_anomaly_statuses(settings), ok=True)
                except Exception as exc:
                    LOG.warning("Anomaly statuses read failed (%s)", type(exc).__name__)
                    anomaly_cache.update(ok=False)
                anomaly_cache["at"] = time.monotonic()
            return dict(anomaly_cache["values"]), anomaly_cache["ok"]

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
    def dashboard_data(day: str | None = None, okrug: str | None = None,
                        tik: str | None = None, precinct: str | None = None):
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
        try:
            values = cached_sheet_values()
        except Exception as exc:
            LOG.warning("Dashboard Sheets read failed (%s)", type(exc).__name__)
            raise HTTPException(502, "Не удалось прочитать Google Таблицу")
        statuses, statuses_ok = cached_anomaly_statuses()
        snapshot = dashboard_snapshot(values, settings, day, okrug, tik, precinct, statuses)
        snapshot["anomalies"]["statuses_ok"] = statuses_ok
        return snapshot

    @app.post("/api/dashboard/anomalies/status", dependencies=[Depends(dashboard_authorized)])
    def anomaly_status(body: AnomalyStatus):
        if not settings.spreadsheet:
            raise HTTPException(503, "Google Таблица не подключена")
        try:
            known = {item["id"]: item for item in detect_anomalies(parse_sheet_rows(cached_sheet_values()), settings)}
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
        with anomaly_lock:
            try:
                write_anomaly_status(settings, record)
            except Exception as exc:
                LOG.warning("Anomaly status write failed (%s)", type(exc).__name__)
                raise HTTPException(502, "Не удалось сохранить статус в Google Таблице")
            anomaly_cache["values"][body.id] = {"status": body.status, "note": note,
                                                "updated_at": record["updated_at"]}
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

    @app.get("/dashboard")
    def dashboard():
        return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def worker():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
