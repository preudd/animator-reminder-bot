import asyncio
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import gspread
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


LOGGER = logging.getLogger("animator_reminder_bot")


def _lockfile_path(token: str) -> str:
    # Telegram token format: "<bot_id>:<secret>"
    bot_id = (token.split(":", 1)[0] or "unknown").strip()
    safe_bot_id = re.sub(r"[^0-9A-Za-z_-]+", "_", bot_id) or "unknown"
    return os.path.join(tempfile.gettempdir(), f"animator_reminder_bot_{safe_bot_id}.lock")


def env_get(name: str, default: str = "") -> str:
    """
    Read env var and normalize common .env mistakes:
    - surrounding quotes: TELEGRAM_TOKEN="..."
    - extra spaces: KEY= value
    """
    v = os.getenv(name, default)
    if v is None:
        return ""
    v = str(v).strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1].strip()
    return v


# --- Data model ---


@dataclass(frozen=True)
class ProgramRow:
    order_date_raw: str
    datetime_raw: str
    event_raw: str
    character_raw: str
    animator_raw: str
    comment_raw: str

    parsed_date: Optional[date]
    parsed_time: Optional[time]


# --- Google Sheets ---


def _normalize_header(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _get_credentials_path() -> str:
    """
    Returns path to Google credentials.
    If GOOGLE_CREDENTIALS_JSON env var is set, writes it to temp file (for hosting without files).
    """
    import json

    json_str = env_get("GOOGLE_CREDENTIALS_JSON")
    if json_str:
        try:
            data = json.loads(json_str)
            fd, path = tempfile.mkstemp(suffix=".json", prefix="gcreds_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                return path
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_CREDENTIALS_JSON: invalid JSON: {e}") from e

    path = env_get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    if path == "credentials.json" and not os.path.exists(path):
        if os.path.exists("credentials.json.json"):
            return "credentials.json.json"
    return path


def get_rows_from_sheet(sheet_name: str, credentials_path: str) -> List[Dict[str, Any]]:
    """
    Reads ALL rows as dictionaries (header -> value).
    Raises exceptions to let caller decide how to notify Telegram.
    """
    # Common Windows mistake: downloaded file ends up as credentials.json.json
    if credentials_path == "credentials.json" and not os.path.exists(credentials_path):
        alt = "credentials.json.json"
        if os.path.exists(alt):
            credentials_path = alt

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
    client = gspread.authorize(creds)

    # Accept either:
    # - spreadsheet title (client.open)
    # - spreadsheet URL
    # - spreadsheet key/id
    sheet_name = (sheet_name or "").strip()
    spreadsheet = None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_name)
    if m:
        spreadsheet = client.open_by_key(m.group(1))
    elif re.fullmatch(r"[a-zA-Z0-9-_]{20,}", sheet_name):
        spreadsheet = client.open_by_key(sheet_name)
    else:
        spreadsheet = client.open(sheet_name)

    worksheet = spreadsheet.sheet1
    return worksheet.get_all_records(default_blank="")


# --- Parsing / filtering ---


_DATE_PATTERNS: Sequence[str] = (
    "%d.%m.%Y",
    "%d.%m.%y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d/%m/%y",
)

_DATETIME_PATTERNS: Sequence[str] = (
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
)


def _try_parse_with_patterns(value: str, patterns: Iterable[str]) -> Optional[datetime]:
    v = (value or "").strip()
    if not v:
        return None
    for p in patterns:
        try:
            return datetime.strptime(v, p)
        except ValueError:
            continue
    return None


def _try_parse_short_date(value: str, tz: Any) -> Optional[date]:
    """
    Supports dates without year, e.g. '17/03' or '17.03'.
    Assumes current year in the configured timezone.
    """
    v = (value or "").strip()
    if not v:
        return None
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})", v)
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    year = datetime.now(tz).year if tz else datetime.now().year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _try_parse_time_loose(value: str) -> Optional[time]:
    """
    Supports '20:45' and also '20.45'.
    """
    v = (value or "").strip()
    if not v:
        return None
    m = re.search(r"(\d{1,2})[:.](\d{2})", v)
    if not m:
        return None
    try:
        return time(hour=int(m.group(1)), minute=int(m.group(2)))
    except ValueError:
        return None


def parse_row(raw: Dict[str, Any]) -> ProgramRow:
    """
    Supports Russian headers (case/space-insensitive) and minor variations.
    Expected columns:
      Дата заказа | Дата/время | мер-ия | Персонаж | ФИО аниматора | комментарий
    """
    normalized = {_normalize_header(k): v for k, v in raw.items()}

    def _pick_value(*exact_keys: str, startswith: Sequence[str] = (), contains: Sequence[str] = ()) -> str:
        # 1) Exact matches
        for k in exact_keys:
            nk = _normalize_header(k)
            if nk in normalized:
                return str(normalized.get(nk, "")).strip()
        # 2) Startswith matches (for truncated headers like "дата/врем")
        for pref in startswith:
            pref_n = _normalize_header(pref)
            for key, val in normalized.items():
                if key.startswith(pref_n):
                    return str(val).strip()
        # 3) Contains matches
        for needle in contains:
            needle_n = _normalize_header(needle)
            for key, val in normalized.items():
                if needle_n in key:
                    return str(val).strip()
        return ""

    order_date_raw = _pick_value(
        "дата заказа",
        "дата",
        "order date",
        startswith=("дата зака",),
        contains=("дата заказа",),
    )
    datetime_raw = _pick_value(
        "дата/время",
        "дата время",
        "datetime",
        startswith=("дата/врем", "дата/вр"),
        contains=("дата/врем", "время"),
    )
    event_raw = _pick_value(
        "мер-ия",
        "мероприятие",
        "event",
        startswith=("мер",),
        contains=("мер", "мероп"),
    )
    character_raw = _pick_value(
        "персонаж",
        "character",
        startswith=("персон",),
        contains=("персонаж",),
    )
    animator_raw = _pick_value(
        "фио аниматора",
        "аниматор",
        "animator",
        startswith=("фио аним", "фио", "аним"),
        contains=("аним", "фио"),
    )
    comment_raw = _pick_value(
        "комментарий",
        "comment",
        startswith=("коммент",),
        contains=("коммент",),
    )

    parsed_date: Optional[date] = None
    parsed_time: Optional[time] = None

    dt = _try_parse_with_patterns(datetime_raw, _DATETIME_PATTERNS)
    if dt:
        parsed_date = dt.date()
        parsed_time = dt.time().replace(second=0, microsecond=0)
    else:
        d = _try_parse_with_patterns(order_date_raw, _DATE_PATTERNS)
        if d:
            parsed_date = d.date()
        else:
            # Support dd/mm (no year) in "Дата заказа"
            parsed_date = _try_parse_short_date(order_date_raw, _get_timezone())

        # Try to extract time from "Дата/время" even if date part is missing
        parsed_time = _try_parse_time_loose(datetime_raw)

    return ProgramRow(
        order_date_raw=order_date_raw,
        datetime_raw=datetime_raw,
        event_raw=event_raw,
        character_raw=character_raw,
        animator_raw=animator_raw,
        comment_raw=comment_raw,
        parsed_date=parsed_date,
        parsed_time=parsed_time,
    )


def filter_rows_for_date(rows: Sequence[ProgramRow], target_date: date) -> List[ProgramRow]:
    return [r for r in rows if r.parsed_date == target_date]


# --- Formatting ---


def _display_program_name(r: ProgramRow) -> str:
    # Prefer character (as in example), fallback to event.
    name = r.character_raw.strip() or r.event_raw.strip()
    return name or "Без названия"


def format_message(rows: Sequence[ProgramRow], target_date: date) -> str:
    if not rows:
        return "Завтра анимационных программ не запланировано"

    header = f"Завтрашняя анимационная программа ({target_date.strftime('%d.%m.%Y')}):"

    # Group by time to produce compact lines like:
    # 10:00 — Шелли, Лион (Аделя, Тимур)
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t_key = r.parsed_time.strftime("%H:%M") if r.parsed_time else "??:??"
        bucket = grouped.setdefault(t_key, {"names": [], "animators": []})
        bucket["names"].append(_display_program_name(r))
        if r.animator_raw:
            bucket["animators"].append(r.animator_raw)

    def _sort_key(k: str) -> Tuple[int, int]:
        if k == "??:??":
            return (99, 99)
        hh, mm = k.split(":")
        return (int(hh), int(mm))

    lines: List[str] = [header, ""]
    for t_key in sorted(grouped.keys(), key=_sort_key):
        names = ", ".join([n for n in grouped[t_key]["names"] if n])
        animators = ", ".join(sorted(set([a for a in grouped[t_key]["animators"] if a])))
        if animators:
            lines.append(f"- {t_key} — {names} ({animators})")
        else:
            lines.append(f"- {t_key} — {names}")

    return "\n".join(lines).strip()


# --- Telegram sending ---


async def send_message(bot: Bot, chat_id: str, text: str) -> None:
    await bot.send_message(chat_id=chat_id, text=text)


# --- Job orchestration ---


def _get_timezone() -> Any:
    tz_name = env_get("TIMEZONE", "Europe/Moscow") or "Europe/Moscow"
    if ZoneInfo is None:
        return tz_name
    try:
        return ZoneInfo(tz_name)
    except Exception:
        LOGGER.warning("Unknown TIMEZONE=%r, fallback to Europe/Moscow", tz_name)
        return ZoneInfo("Europe/Moscow")


def _tomorrow_in_tz(tz: Any) -> date:
    now = datetime.now(tz) if tz else datetime.now()
    return (now + timedelta(days=1)).date()


async def daily_job(app: Application) -> None:
    LOGGER.info("Daily job started")
    chat_id = env_get("CHAT_ID")
    sheet_name = env_get("GOOGLE_SHEET_NAME")
    credentials_path = _get_credentials_path()

    if not chat_id or not sheet_name:
        LOGGER.error("Missing CHAT_ID or GOOGLE_SHEET_NAME in .env")
        return

    tz = _get_timezone()
    target = _tomorrow_in_tz(tz)

    try:
        raw_rows = get_rows_from_sheet(sheet_name=sheet_name, credentials_path=credentials_path)
    except Exception as e:
        LOGGER.exception("Google Sheet is unavailable or cannot be read")
        try:
            await send_message(
                bot=app.bot,
                chat_id=chat_id,
                text=f"Ошибка: не удалось прочитать Google Sheets (таблица недоступна).\nПричина: {type(e).__name__}: {e}",
            )
        except TelegramError:
            LOGGER.exception("Failed to send sheet-unavailable error to Telegram")
        return

    parsed = [parse_row(r) for r in raw_rows]
    filtered = filter_rows_for_date(parsed, target)
    text = format_message(filtered, target)

    try:
        await send_message(bot=app.bot, chat_id=chat_id, text=text)
    except TelegramError:
        LOGGER.exception("Failed to send daily message to Telegram")


async def test_sheet_send_now() -> None:
    """
    One-off run: read sheet -> build "tomorrow" message -> send to CHAT_ID, then exit.
    Useful for manual testing without waiting for 12:00.
    """
    token = env_get("TELEGRAM_TOKEN") or env_get("BOT_TOKEN") or env_get("TELEGRAM_BOT_TOKEN")
    chat_id = env_get("CHAT_ID")
    sheet_name = env_get("GOOGLE_SHEET_NAME")
    credentials_path = _get_credentials_path()

    if not token:
        raise RuntimeError("TELEGRAM_TOKEN (или BOT_TOKEN) не задан. Добавьте в .env или переменные окружения.")
    if not chat_id or not sheet_name:
        raise RuntimeError("CHAT_ID or GOOGLE_SHEET_NAME is missing in .env")

    bot = Bot(token=token)
    tz = _get_timezone()
    target = _tomorrow_in_tz(tz)

    try:
        raw_rows = get_rows_from_sheet(sheet_name=sheet_name, credentials_path=credentials_path)
    except Exception as e:
        LOGGER.exception("Google Sheet is unavailable or cannot be read")
        try:
            await send_message(
                bot=bot,
                chat_id=chat_id,
                text=f"ТЕСТ: Ошибка: не удалось прочитать Google Sheets (таблица недоступна).\nПричина: {type(e).__name__}: {e}",
            )
        except TelegramError:
            LOGGER.exception("Failed to send test sheet-unavailable error to Telegram")
        return

    parsed = [parse_row(r) for r in raw_rows]
    filtered = filter_rows_for_date(parsed, target)
    text = "ТЕСТ:\n" + format_message(filtered, target)

    try:
        await send_message(bot=bot, chat_id=chat_id, text=text)
        LOGGER.info("Test message sent to chat_id=%s", chat_id)
    except TelegramError:
        LOGGER.exception("Failed to send test message to Telegram")


# --- Scheduler wiring ---


def setup_scheduler(app: Application) -> AsyncIOScheduler:
    tz = _get_timezone()
    scheduler = AsyncIOScheduler(timezone=tz)
    trigger = CronTrigger(hour=14, minute=00, timezone=tz)

    scheduler.add_job(
        daily_job,
        args=[app],
        trigger=trigger,
        id="daily_animator_reminder",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60 * 30,  # 30 minutes
        coalesce=True,
    )
    scheduler.start()
    try:
        job = scheduler.get_job("daily_animator_reminder")
        if job and job.next_run_time:
            LOGGER.info("Next scheduled run at: %s", job.next_run_time)
    except Exception:
        LOGGER.exception("Failed to read next_run_time from scheduler")
    return scheduler


async def post_init(app: Application) -> None:
    app.bot_data["scheduler"] = setup_scheduler(app)
    # Log the effective timezone used for scheduling
    LOGGER.info("Scheduler started. Timezone: %s", env_get("TIMEZONE", "Europe/Moscow") or "Europe/Moscow")


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def acquire_single_instance_lock(lockfile_path: str) -> None:
    """
    Prevent running multiple bot instances simultaneously.
    Otherwise Telegram will return 409 Conflict for getUpdates (long polling).
    """
    try:
        fd = os.open(lockfile_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing_pid: Optional[int] = None
        try:
            with open(lockfile_path, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r"pid\s*=\s*(\d+)", content)
            if m:
                existing_pid = int(m.group(1))
        except OSError:
            pass

        # If lockfile exists but the process is gone, treat lock as stale and remove it.
        if existing_pid is not None:
            try:
                os.kill(existing_pid, 0)  # does not kill; just checks existence/permissions
                is_running = True
            except OSError:
                is_running = False

            if not is_running:
                try:
                    os.remove(lockfile_path)
                except OSError as e:
                    raise RuntimeError(
                        f"Найден устаревший lock-файл {lockfile_path} (pid={existing_pid} не существует), "
                        f"но не удалось удалить его: {e}"
                    ) from e
                return acquire_single_instance_lock(lockfile_path)

            raise RuntimeError(
                f"Бот уже запущен (PID={existing_pid}, lock-файл {lockfile_path}). "
                f"Остановите процесс PID={existing_pid} или дождитесь его завершения."
            )

        raise RuntimeError(
            f"Бот уже запущен (найден lock-файл {lockfile_path}). "
            f"Остановите предыдущий процесс или удалите lock-файл."
        )
    except OSError as e:
        raise RuntimeError(f"Не удалось создать lock-файл {lockfile_path}: {e}") from e

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()}\n")

    def _cleanup() -> None:
        try:
            os.remove(lockfile_path)
        except OSError:
            pass

    import atexit

    atexit.register(_cleanup)


async def on_error(update: object, context: object) -> None:
    err = getattr(context, "error", None)
    if isinstance(err, TelegramError) and err.__class__.__name__ == "Conflict":
        LOGGER.error(
            "Telegram 409 Conflict: запущен другой экземпляр бота (или другой процесс делает getUpdates). "
            "Остановите второй экземпляр."
        )
        # Best effort: exit immediately to avoid endless retries/spam and half-initialized HTTP state.
        os._exit(2)
    LOGGER.exception("Unhandled error in telegram application", exc_info=err)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Simple health-check command."""
    try:
        await update.message.reply_text("Бот в рабочем состоянии ✅")
    except Exception:
        LOGGER.exception("Failed to respond to /start")


def main() -> None:
    load_dotenv()
    setup_logging()

    # CLI flag: one-off test to send tomorrow's schedule and exit.
    if len(sys.argv) > 1 and sys.argv[1] == "--test-sheet":
        asyncio.run(test_sheet_send_now())
        return

    token = env_get("TELEGRAM_TOKEN") or env_get("BOT_TOKEN") or env_get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_TOKEN (или BOT_TOKEN) не задан. Добавьте токен в переменные окружения на хостинге."
        )

    try:
        acquire_single_instance_lock(_lockfile_path(token))
    except RuntimeError as e:
        LOGGER.error(str(e))
        sys.exit(1)

    # Python 3.14+ не создаёт event loop автоматически в главном потоке,
    # а python-telegram-bot ожидает его наличия при run_polling.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start_command))

    # Standard PTB polling; scheduler runs in the same event loop.
    app.run_polling()


if __name__ == "__main__":
    main()

