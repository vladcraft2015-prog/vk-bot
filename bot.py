import hashlib
import json
import math
import os
import random
import re
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone

import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll
from vk_api.utils import get_random_id

# ==========================================
# ⚙️ НАСТРОЙКИ И ДАННЫЕ
# ==========================================
# Токен VK (берётся из переменной окружения VK_TOKEN или подставьте свой вместо строки ниже)
TOKEN = os.environ.get("VK_TOKEN", "ВАШ_ТОКЕН_ПОЛЬЗОВАТЕЛЯ")

# ID администратора
ADMIN_ID = 550216110

# ID сообщества ИИ Авроры (события в общем чате и сброс алтаря)
AURORA_ID = -235592858

# ID сообщества Атраксис (игра): от него принимаются руны, сборы и таймеры
ATRAXIS_ID = -207204359

# Путь для сохранения данных
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "runes_data.json")
STATS_FILE = os.path.join(DATA_DIR, "stats_data.json")
TIMERS_FILE = os.path.join(DATA_DIR, "timers_data.json")

# ID бесед
RESET_PEER_ID = 2000000012  # Общий чат (события и сбросы)
RUNES_PEER_IDS = [2000000011, 2000000013]  # Рабочие чаты сбора рун
# Куда бот сам присылает итоги недели и месяца (в чат, где за период не было сборов, ничего не придёт).
# Чтобы слать только игрокам, оставьте тут один ID, например [2000000013]
TOP_PEER_IDS = list(RUNES_PEER_IDS)

# 5 аспектов рун Атраксиса
ASPECTS = ["Мутация", "Стабильность", "Память", "Забвение", "Очищение"]

RESET_TRIGGERS = [
    "рунический свет погас",
    "разгадал символы",
    "символы алтаря",
    "алтаря",
    "алтарь сброшен",
    "ритуал соверш",
    "ритуал заверш",
]

AURORA_TRIGGER = "✍ Введите все 5 руноблоков через пробел. Порядок не важен. Есть лишь один шанс..."

# --- Таймеры и автоудаление ---
TIMER_SECONDS = 3600      # до следующего удара по боссу / исследования всегда час
REMINDER_TTL = 30 * 60    # напоминание с тегом удаляется через 30 минут
NOTICE_TTL = 10           # короткие пометки бота удаляются через 10 секунд
WARNING_TTL = 30          # предупреждения об ошибках разбора
TIMER_STALE = 2 * 3600    # если бот был выключен дольше, напоминание уже не нужно
CUSTOM_MIN = 10           # свой таймер: минимум 10 секунд
CUSTOM_MAX = 24 * 3600    # свой таймер: максимум 24 часа
CUSTOM_LIMIT = 5          # не больше 5 своих таймеров на игрока в чате

# --- Шифр Цезаря (терминал ИИ Авроры приходит в личку с Атраксисом) ---
CIPHER_FORWARD = True     # игроки пересылают терминал в рабочий чат, бот отвечает расшифровкой
CIPHER_TTL = 15           # через сколько секунд удалять и ответ бота, и пересланный терминал
CIPHER_AUTO = False       # решать шифр сам, когда терминал приходит в личку аккаунту бота
CIPHER_GUESS = True       # если слова нет в словаре, отвечать «по звучанию», когда бот очень уверен
CIPHER_MARGIN = 8.0       # порог уверенности для CIPHER_GUESS (больше: отвечает реже, но точнее)
CIPHER_DELAY = (2, 4)     # пауза перед ответом в игру, секунд (от и до)
CIPHER_NOTIFY_ID = ADMIN_ID   # кому писать, когда бот не уверен или игра сказала «неверно»
CIPHER_WORDS_FILE = os.path.join(DATA_DIR, "cipher_words.json")  # слова, которые бот выучил сам

# Московское время (без перехода на летнее/зимнее)
MSK = timezone(timedelta(hours=3))

START_TIME = time.time()
last_reset_time = 0  # Время последнего сброса алтаря
data_lock = threading.Lock()
api_lock = threading.Lock()
stats_lock = threading.Lock()
timers_lock = threading.RLock()

stats = {}        # {"peer_id": [запись, ...]}
stats_seen = {}   # {"peer_id": {ключи уже учтённых сборов}}
timers = []       # активные таймеры
deletions = []    # очередь сообщений бота на удаление
posted = {}       # за какие периоды уже отправлены итоги: {"week": "2026-10-05", "month": "2026-09"}
_post_state = {}  # попытки отправки итогов

# Виды таймеров
KINDS = {
    "boss": {
        "icon": "⚔️",
        "logged": "удар по боссу записан",
        "late": "можно бить босса",
        "remind": "пора бить босса! ⚔️",
        "title": "Босс",
    },
    "research": {
        "icon": "🔬",
        "logged": "исследование записано",
        "late": "можно исследовать",
        "remind": "пора на исследование! 🔬",
        "title": "Исследование",
    },
}

GRADE_LABELS = ["⚪ Серых (I)", "🔵 Синих (II)", "🟣 Фиолетовых (III)"]
ROMAN = {"I": 0, "II": 1, "III": 2}


# ==========================================
# 🛠 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_runes = data.get("runes", {})
                runes = {}
                for k, v in raw_runes.items():
                    if isinstance(v, str):
                        runes[k] = {"code": v, "time": 0}
                    else:
                        runes[k] = v
                return runes
        except Exception as e:
            print(f"⚠️ Ошибка загрузки базы рун: {e}")
            return {}
    return {}


def save_data(runes):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"runes": runes}, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения базы рун: {e}")


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Ошибка загрузки {os.path.basename(path)}: {e}")
    return default


def _save_json(path, payload):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения {os.path.basename(path)}: {e}")


def load_stats():
    global stats, stats_seen
    data = _load_json(STATS_FILE, {})
    raw = data.get("chats", {})
    stats = {str(k): list(v) for k, v in raw.items()}
    stats_seen = {k: {r.get("k") for r in v} for k, v in stats.items()}
    posted.clear()
    posted.update(data.get("posted", {}))


def save_stats():
    _save_json(STATS_FILE, {"chats": stats, "posted": posted})


def load_timers():
    global timers, deletions
    raw = _load_json(TIMERS_FILE, {})
    timers = list(raw.get("timers", []))
    deletions = list(raw.get("deletions", []))


def save_timers():
    _save_json(TIMERS_FILE, {"timers": timers, "deletions": deletions})


def send_msg(peer_id, text):
    """Отправляет сообщение. Возвращает id отправленного сообщения или None."""
    try:
        params = {
            "peer_id": peer_id,
            "message": text,
            "random_id": get_random_id(),
        }
        with api_lock:
            res = vk.messages.send(**params)
        return res if isinstance(res, int) else None
    except Exception as e:
        print(f"⚠️ Ошибка отправки в {peer_id}: {e}")
        return None


def send_to_work_chats(text):
    for pid in RUNES_PEER_IDS:
        send_msg(pid, text)


def delete_msg(message_id):
    if not message_id:
        return False
    try:
        with api_lock:
            vk.messages.delete(message_ids=[message_id], delete_for_all=1)
        return True
    except Exception as e:
        print(f"⚠️ Не удалось удалить сообщение {message_id}: {e}")
        return False


def delete_by_cmid(peer_id, cmid):
    """Удаление по номеру сообщения внутри беседы (если у reply_message нет обычного id)."""
    if not cmid:
        return False
    try:
        with api_lock:
            vk.messages.delete(peer_id=peer_id, cmids=[cmid], delete_for_all=1)
        return True
    except Exception as e:
        print(f"⚠️ Не удалось удалить сообщение cmid={cmid} в {peer_id}: {e}")
        return False


DEL_WORDS = {"дел", "делл", "dell", "del", "/дел", "/del", "/dell"}


def handle_delete_command(msg, peer_id, user_id, clean_text):
    """Админ отвечает «дел» на любое сообщение: бот удаляет его и саму команду."""
    if user_id != ADMIN_ID or clean_text.strip(" .!") not in DEL_WORDS:
        return False
    reply = msg.get("reply_message")
    if not reply:
        return False  # без ответа это просто слово, ничего не делаем
    if reply.get("id"):
        ok = delete_msg(reply["id"])
    else:
        ok = delete_by_cmid(peer_id, reply.get("conversation_message_id"))
    delete_msg(msg.get("id"))
    if not ok:
        schedule_delete(
            send_msg(peer_id, "⚠️ Не получилось удалить сообщение: нужны права администратора беседы."),
            WARNING_TTL,
        )
    return True


def schedule_delete(message_id, delay, now=None):
    """Ставит собственное сообщение бота в очередь на удаление через delay секунд."""
    if not message_id:
        return
    now = time.time() if now is None else now
    with timers_lock:
        deletions.append({"mid": message_id, "at": now + delay})
        save_timers()


_name_cache = {}


def get_name(user_id):
    if user_id in _name_cache:
        return _name_cache[user_id]
    name = "игрок"
    try:
        with api_lock:
            res = vk.users.get(user_ids=user_id)
        if res:
            name = res[0].get("first_name") or name
    except Exception as e:
        print(f"⚠️ Не удалось получить имя {user_id}: {e}")
        return name
    _name_cache[user_id] = name
    return name


def get_status_text(runes):
    status_lines = [f"• {a}: {'+' if a in runes else '-'}" for a in ASPECTS]
    return f"📊 Статус рун ({len(runes)}/5):\n\n" + "\n".join(status_lines)


def broadcast_notification():
    message = (
        "@all 🏛 **Алтарь готов к ритуалу!** (Собрано 5/5)\n\n"
        "Найди алтарь в игре и перешли сюда системное сообщение:\n"
        "«✍ Введите все 5 руноблоков через пробел. Порядок не важен. Есть лишь один шанс...»\n\n"
        "Бот мгновенно выдаст тебе все руноблоки 📜"
    )
    send_to_work_chats(message)


def check_aurora_trigger(fwd_messages, current_time, user_id):
    for fwd in fwd_messages:
        # Только настоящее сообщение игры, пересланное от Атраксиса. Ответ на сообщение самого
        # бота (в рассылке «Алтарь готов» эта фраза процитирована) или чужой текст не считаются.
        if fwd.get("from_id") != ATRAXIS_ID:
            continue
        if AURORA_TRIGGER in fwd.get("text", ""):
            fwd_date = fwd.get("date", 0)
            if (current_time - fwd_date) <= 900 or user_id == ADMIN_ID:
                return "VALID"
            else:
                return "EXPIRED"
    return None


# ==========================================
# 🕒 ВРЕМЯ (МОСКВА)
# ==========================================
def hhmm(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")


def fmt_left(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м"
    return "меньше минуты"


def period_range(period, now_ts):
    """Возвращает (начало, конец или None, название) периода по Москве.
    Неделя начинается с понедельника 00:00, то есть сбрасывается с воскресенья на понедельник."""
    now = datetime.fromtimestamp(now_ts, MSK)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today.timestamp(), None, "Сегодня"
    if period == "yesterday":
        return (today - timedelta(days=1)).timestamp(), today.timestamp(), "Вчера"
    if period == "week":
        monday = today - timedelta(days=today.weekday())
        return monday.timestamp(), None, "Неделя"
    if period == "month":
        return today.replace(day=1).timestamp(), None, "Месяц"
    return 0, None, "Всё время"


# ==========================================
# ⛏ РАЗБОР СООБЩЕНИЙ ИГРЫ (СБОРЫ)
# ==========================================
RE_BRACKET = re.compile(r"\[\s*(III|II|I)\s*\]")
RE_TRAIL = re.compile(r"\s(III|II|I)\s*[.!]?\s*$")
RE_TIER = re.compile(r"\(\s*[TТtт]\s*(\d+)\s*\)")  # (T5), латиница или кириллица


def tier_of(line):
    """Тир ресурса из строки вида «… (T5)». Нет тира — 0."""
    m = RE_TIER.search(line)
    return int(m.group(1)) if m else 0


def is_gather_start(text):
    return "сбор начат" in text.lower()


def parse_gather(text):
    """Разбирает результат сбора. Возвращает None, если это не результат сбора.
    Грейд (серый/синий/фиолетовый) берётся по цифре I/II/III, а не по цвету кружка."""
    low = text.lower()
    if "собрано:" in low:
        mode = "v"  # Жилы (сбор с локации)
    elif "вы добыли ресурсы" in low:
        mode = "r"  # Исследование (жила, найденная в исследовании)
    else:
        return None

    res = []
    tiers = []
    spheres = notes = signs = 0
    dup_line = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        l = line.lower()

        if "бонусы дублируют" in l:
            dup_line = l
            continue

        m = RE_BRACKET.search(line)
        if m and "найден" not in l:
            res.append(ROMAN[m.group(1)])
            tiers.append(tier_of(line))
            continue

        if "{" in line:
            if "сфер" in l:
                spheres += 1
            elif "заметк" in l:
                notes += 1
            elif "знак" in l:
                signs += 1
            else:
                t = RE_TRAIL.search(line)
                if t:
                    res.append(ROMAN[t.group(1)])
                    tiers.append(tier_of(line))

    bonus_res = bonus_sph = 0
    if dup_line is not None:
        if "сфер" in dup_line:
            target = "s"
        elif "ресурс" in dup_line:
            target = "r"
        else:
            target = "s" if spheres else "r"
        if target == "s":
            bonus_sph = spheres
        else:
            bonus_res = len(res)

    grades = [0, 0, 0]
    for g in res:
        grades[g] += 1
    if bonus_res:
        for g in res:
            grades[g] += 1

    # Материя: только серые (I), равна тиру; если бонус задвоил сам ресурс, то вдвое больше
    materia = sum(tr for g, tr in zip(res, tiers) if g == 0)
    if bonus_res:
        materia *= 2

    return {
        "mode": mode,
        "g": grades,
        "bg": bonus_res,
        "s": spheres + bonus_sph,
        "bs": bonus_sph,
        "n": notes,
        "z": signs,
        "mt": materia,
        "ok": bool(res or spheres or notes or signs),
    }


def make_key(user_id, fdate, ftext):
    raw = f"{user_id}|{fdate}|{ftext}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:16]


def add_stat_records(peer_id, recs):
    """Добавляет записи в статистику чата. Возвращает (список добавленных записей, число дубликатов)."""
    added = []
    dupes = 0
    key = str(peer_id)
    with stats_lock:
        bucket = stats.setdefault(key, [])
        seen = stats_seen.setdefault(key, set())
        for rec in recs:
            if rec["k"] in seen:
                dupes += 1
                continue
            seen.add(rec["k"])
            bucket.append(rec)
            added.append(rec)
        if added:
            save_stats()
    return added, dupes


def fmt_recorded(name, recs):
    """Короткая пометка о записанных сборах (без подробностей)."""
    return "✅ Ресурсы записаны"


def aggregate(recs):
    out = {}
    for mode in ("v", "r"):
        out[mode] = {"c": 0, "g": [0, 0, 0], "bg": 0, "s": 0, "bs": 0, "n": 0, "z": 0, "mt": 0}
    for r in recs:
        a = out.get(r.get("m"))
        if a is None:
            continue
        a["c"] += 1
        for i in range(3):
            a["g"][i] += r["g"][i]
        a["bg"] += r.get("bg", 0)
        a["s"] += r.get("s", 0)
        a["bs"] += r.get("bs", 0)
        a["n"] += r.get("n", 0)
        a["z"] += r.get("z", 0)
        a["mt"] += r.get("mt", 0)
    return out


def fmt_pct(part, total):
    pct = (part / total * 100) if total else 0
    return f"{pct:.1f}".replace(".", ",") + "%"


def format_stats(recs, scope_title, period_title):
    """Одна общая сводка. Строки короткие, чтобы не переносились в узком пузыре сообщения."""
    agg = aggregate(recs)
    v, r = agg["v"], agg["r"]
    c = v["c"] + r["c"]
    g = [v["g"][i] + r["g"][i] for i in range(3)]
    bonus = v["bg"] + r["bg"] + v["bs"] + r["bs"]
    sph = v["s"] + r["s"]
    notes = v["n"] + r["n"]
    signs = v["z"] + r["z"]
    materia = v["mt"] + r["mt"]
    total_res = sum(g)

    # Первая строка длинная и без эмодзи: VK тянет пузырь по самой длинной строке,
    # а строки с эмодзи считает уже, чем рисует, и переносит их.
    lines = [f"Стата · {scope_title} · {period_title}", ""]
    if c == 0:
        lines.append("Сборов пока нет.")
        return "\n".join(lines)

    lines += [f"🔢 Сборов: {c}", f"{PICK} Ресурсов: {total_res}", f"🔮 Материя: {materia}", ""]
    for emoji, i in (("⚪", 0), ("🔵", 1), ("🟣", 2)):
        lines.append(f"{emoji} {g[i]} — {fmt_pct(g[i], total_res)}")
    lines += ["", f"📦 Сферы: {sph}", f"📜 Заметки: {notes}", f"🎖 Знаки: {signs}"]
    if bonus:
        lines.append(f"🎁 Бонусных: {bonus}")
    return "\n".join(lines)


# ==========================================
# 📈 КОМАНДА /стата
# ==========================================
STATS_HEADS = ("/стата", "/стат", "/stats")
PERIOD_WORDS = {
    "сегодня": "today", "сег": "today", "today": "today",
    "вчера": "yesterday",
    "неделя": "week", "неделю": "week", "нед": "week", "week": "week",
    "месяц": "month", "мес": "month", "month": "month",
    "всё": "all", "все": "all", "всегда": "all", "all": "all",
}
SCOPE_WORDS = {"общая", "общую", "общий", "общее", "всех"}
EXTRA_WORDS = {"время", "сброс"}
STATS_USAGE = (
    "ℹ️ Формат: /стата [общая] [сегодня | вчера | неделя | месяц | всё]\n"
    "Примеры: /стата, /стата неделя, /стата общая месяц"
)


def parse_stats_command(clean_text):
    tokens = clean_text.split()
    if not tokens or tokens[0] not in STATS_HEADS:
        return None
    args = tokens[1:]

    cmd = {"scope": "me", "period": "today", "reset": False, "error": False}
    for a in args:
        if a in SCOPE_WORDS:
            cmd["scope"] = "all"
        elif a in PERIOD_WORDS:
            cmd["period"] = PERIOD_WORDS[a]
        elif a == "сброс":
            cmd["reset"] = True
        elif a == "время":
            continue
        else:
            cmd["error"] = True
    return cmd


def handle_stats_command(peer_id, user_id, clean_text):
    cmd = parse_stats_command(clean_text)
    if cmd is None:
        return False

    if cmd["error"]:
        send_msg(peer_id, STATS_USAGE)
        return True

    if cmd["reset"]:
        if user_id != ADMIN_ID:
            send_msg(peer_id, "⛔ У вас нет доступа к этой команде.")
            return True
        with stats_lock:
            stats[str(peer_id)] = []
            stats_seen[str(peer_id)] = set()
            save_stats()
        send_msg(peer_id, "🗑 Статистика этого чата очищена.")
        return True

    start, end, period_title = period_range(cmd["period"], time.time())
    with stats_lock:
        recs = list(stats.get(str(peer_id), []))
    recs = [
        r for r in recs
        if r["t"] >= start and (end is None or r["t"] < end)
        and (cmd["scope"] == "all" or r["u"] == user_id)
    ]
    scope_title = "Общая" if cmd["scope"] == "all" else "Моя"
    send_msg(peer_id, format_stats(recs, scope_title, period_title))
    return True


# ==========================================
# 🏆 КОМАНДА /топ
# ==========================================
_full_cache = {}


def get_full_name(user_id):
    if user_id in _full_cache:
        return _full_cache[user_id]
    name = f"id{user_id}"
    try:
        with api_lock:
            res = vk.users.get(user_ids=user_id)
        if res:
            first = (res[0].get("first_name") or "").strip()
            last = (res[0].get("last_name") or "").strip()
            name = (first + " " + last).strip() or name
    except Exception as e:
        print(f"⚠️ Не удалось получить имя {user_id}: {e}")
        return name
    _full_cache[user_id] = name
    return name


PICK = "\u26cf\ufe0f"  # ⛏️ значок ресурсов
TOP_USAGE = (
    "ℹ️ Топ игроков по ресурсам:\n"
    "/топ — за неделю\n"
    "/топ месяц — за месяц\n"
    "/топ всё — за всё время\n"
    "Ещё: сегодня, вчера"
)
TOP_FILLER = {"за", "время"}
TOP_PERIOD_TITLES = {
    "today": "Сегодня",
    "yesterday": "Вчера",
    "week": "Неделя",
    "month": "Месяц",
    "all": "Всё время",
}
RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
RU_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


NAME_FIT = 18  # столько символов помещается в строку узкого пузыря сообщения


def short_name(full):
    """Имя и фамилия целиком, если помещаются в строку. Длинные сокращаем: 'Анастасия Вербицкая' -> 'Анастасия В.'."""
    full = " ".join(full.split())
    if len(full) <= NAME_FIT:
        return full
    parts = full.split()
    if len(parts) > 1 and not full.startswith("id"):
        return f"{parts[0][:12]} {parts[1][0]}."
    return full[:NAME_FIT]


def top_rows(peer_id, start, end, limit=10):
    """Топ игроков чата за период: [(имя, всего ресурсов, фиолетовых, материя)].
    Сортировка по общему числу ресурсов, при равенстве по фиолетовым, затем по материи."""
    with stats_lock:
        recs = list(stats.get(str(peer_id), []))
    agg = {}
    for r in recs:
        if r["t"] < start or (end is not None and r["t"] >= end):
            continue
        a = agg.setdefault(r["u"], [0, 0, 0])
        a[0] += sum(r["g"])
        a[1] += r["g"][2]
        a[2] += r.get("mt", 0)
    rows = [(u, tot, vio, mat) for u, (tot, vio, mat) in agg.items() if tot > 0]
    rows.sort(key=lambda x: (-x[1], -x[2], -x[3], x[0]))
    return [(short_name(get_full_name(u)), tot, vio, mat) for u, tot, vio, mat in rows[:limit]]


def format_top(rows, head):
    """Три строки на игрока, между игроками пустая строка:
    Иван Иванов
    🥇 ⛏️ 200 · 🟣 20
    🔮 Материя: 45"""
    lines = [head, ""]
    if not rows:
        lines.append("Пока нет ресурсов.")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, tot, vio, mat) in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        if i:
            lines.append("")
        lines.append(name)
        lines.append(f"{mark} {PICK} {tot} · 🟣 {vio}")
        lines.append(f"🔮 Материя: {mat}")
    return "\n".join(lines)


def handle_top_command(peer_id, user_id, clean_text):
    tokens = clean_text.split()
    if not tokens or tokens[0] not in ("/топ", "/top"):
        return False
    args = [a for a in tokens[1:] if a not in TOP_FILLER]

    period = "week"
    for a in args:
        if a in PERIOD_WORDS:
            period = PERIOD_WORDS[a]
        else:
            send_msg(peer_id, TOP_USAGE)
            return True

    start, end, _ = period_range(period, time.time())
    rows = top_rows(peer_id, start, end)
    send_msg(peer_id, format_top(rows, f"Топ игроков · {TOP_PERIOD_TITLES[period]}"))
    return True


def week_range_title(monday):
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {RU_MONTHS_GEN[sunday.month - 1]}"
    return (
        f"{monday.day} {RU_MONTHS_GEN[monday.month - 1]} – "
        f"{sunday.day} {RU_MONTHS_GEN[sunday.month - 1]}"
    )


def send_period_top(start, end, head, footer):
    """Шлёт итоги периода в чаты, где за него были сборы. True, если отправлено или слать некуда."""
    sent_any = failed = False
    for pid in TOP_PEER_IDS:
        rows = top_rows(pid, start, end)
        if not rows:
            continue
        if send_msg(pid, format_top(rows, head) + "\n\n" + footer):
            sent_any = True
        else:
            failed = True
    return sent_any or not failed


def post_period_tops(now=None):
    """С воскресенья на понедельник присылает итоги недели, 1-го числа итоги месяца (по Москве).
    Какой период уже отправлен, помнит в файле, поэтому перезапуск не даёт дублей.
    При самом первом запуске ничего задним числом не шлёт."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    this_month = today.replace(day=1)
    prev_month = (this_month - timedelta(days=1)).replace(day=1)

    jobs = [
        (
            "week",
            prev_monday.date().isoformat(),
            prev_monday.timestamp(),
            this_monday.timestamp(),
            f"Топ недели · {week_range_title(prev_monday)}",
            "🔄 Новая неделя — счётчик с нуля.",
        ),
        (
            "month",
            prev_month.strftime("%Y-%m"),
            prev_month.timestamp(),
            this_month.timestamp(),
            f"Топ месяца · {RU_MONTHS[prev_month.month - 1]}",
            "🔄 Новый месяц — счётчик с нуля.",
        ),
    ]
    for kind, key, start, end, head, footer in jobs:
        with stats_lock:
            last = posted.get(kind)
            if last is None:
                posted[kind] = key
                save_stats()
                continue
            if last == key:
                continue
        st = _post_state.get(kind)
        if st and now < st["next"]:
            continue

        if send_period_top(start, end, head, footer):
            with stats_lock:
                posted[kind] = key
                save_stats()
            _post_state.pop(kind, None)
        else:
            tries = (st["tries"] if st else 0) + 1
            if tries >= 5:
                with stats_lock:
                    posted[kind] = key
                    save_stats()
                _post_state.pop(kind, None)
            else:
                _post_state[kind] = {"tries": tries, "next": now + 60}


# ==========================================
# 📖 КОМАНДА /помощь
# ==========================================
HELP_USER = (
    "📖 Команды\n"
    "\n"
    "Пересылайте сюда сообщения от Атраксиса: бот сам запишет ресурсы, "
    "поставит таймер на босса и исследование и удалит ваше сообщение. "
    "Если приписать подпись или переслать вместе с логом начала сбора, сообщение останется.\n\n"
    "📊 /стата [общая] [сегодня | вчера | неделя | месяц | всё] — статистика сборов\n"
    "🏆 /топ — топ по ресурсам за неделю, /топ месяц, /топ всё (за всё время)\n"
    "⏳ /таймеры — ваши активные таймеры\n"
    "⏰ /таймер 45м кузница — свой таймер (45м, 1ч30м, 90)\n"
    "🗑 /отмена [босс | исследование | свои] — отменить таймеры\n"
    "🔐 Терминал с шифром просто перешлите сюда: бот пришлёт ответ и через минуту всё удалит\n"
    "📜 /статус — сколько рун собрано\n"
    "🏓 /пинг — проверить, что бот жив"
)

HELP_ADMIN = (
    "🛠 Админ-команды\n"
    "\n"
    "🧽 дел — ответом на чужое сообщение: бот удалит его и вашу команду (нужны права админа беседы)\n"
    "💾 /код — все собранные коды рун\n"
    "➕ /add <аспект> <код> — внести руну вручную\n"
    "➖ /удалить <аспект> — удалить одну руну\n"
    "🗑 /очистить — очистить базу рун\n"
    "🔐 /шифр+ <слово> — добавить слово в словарь шифра, /шифр- <слово> — убрать выученное\n"
    "📢 /все <текст> — рассылка во все рабочие чаты\n"
    "🧹 /стата сброс — очистить статистику этого чата\n"
    "🔎 /кто — перешлите сообщение с этой командой, бот покажет его from_id\n"
    "🆔 /id — ID этого чата"
)


def handle_help_command(peer_id, user_id, clean_text):
    tokens = clean_text.split()
    if not tokens:
        return False
    head = tokens[0]
    if head in ("/админ", "/адм"):
        admin = True
    elif head in ("/помощь", "/help", "/команды", "/хелп"):
        admin = len(tokens) > 1 and tokens[1].startswith("адм")
    else:
        return False

    if admin and user_id != ADMIN_ID:
        send_msg(peer_id, "⛔ У вас нет доступа к этой команде.")
        return True
    send_msg(peer_id, HELP_ADMIN if admin else HELP_USER)
    return True


# ==========================================
# ⏳ ТАЙМЕРЫ (БОСС / ИССЛЕДОВАНИЕ)
# ==========================================
def timer_kind(text):
    low = text.lower()
    # Удар по колоссу («Урон по колоссу… Следующая атака через 1м») таймера не получает:
    # часовой таймер только на удар по мировому боссу.
    if "следующая атака" in low and "по колосс" not in low:
        return "boss"
    if "следующее исследование" in low:
        return "research"
    return None


def set_timer(peer_id, user_id, kind, start, name):
    """Ставит таймер. Возвращает (статус, время срабатывания).
    Статусы: new, dupe (такое сообщение уже учтено), expired (час уже прошёл)."""
    now = time.time()
    due = start + TIMER_SECONDS
    with timers_lock:
        existing = None
        for t in timers:
            if t["peer"] == peer_id and t["user"] == user_id and t["kind"] == kind:
                existing = t
                break
        if existing and start <= existing["start"]:
            return "dupe", existing["due"]
        if existing:
            timers.remove(existing)
        if due <= now:
            save_timers()
            return "expired", due
        timers.append({
            "peer": peer_id,
            "user": user_id,
            "kind": kind,
            "start": start,
            "due": due,
            "name": name,
        })
        save_timers()
    return "new", due


RE_DUR = re.compile(r"(\d+)\s*([чмсhms])[а-яёa-z]*")
UNIT_WORD = re.compile(r"^(ч|час\w*|м|мин\w*|сек\w*|h|hr|hour\w*|m|min\w*|sec\w*)$")
TIMER_USAGE = (
    "ℹ️ Формат: /таймер <время> [подпись]\n"
    "Время: 45м, 1ч30м, 2ч, 90 (минуты), 1:30\n"
    "Пример: /таймер 45м кузница"
)


def fmt_dur(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s and not h:
        parts.append(f"{s}с")
    return " ".join(parts) or "0с"


def parse_duration_token(tok):
    """'45м', '1ч30м', '90' (минуты), '1:30' -> секунды, иначе None."""
    tok = tok.strip().lower()
    if not tok:
        return None
    if tok.isdigit():
        return int(tok) * 60
    m = re.fullmatch(r"(\d+):(\d{2})", tok)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    pos = total = 0
    found = False
    for m in RE_DUR.finditer(tok):
        if m.start() != pos:
            return None
        n, u = int(m.group(1)), m.group(2)
        total += n * (3600 if u in "чh" else 60 if u in "мm" else 1)
        pos = m.end()
        found = True
    return total if found and pos == len(tok) else None


def merge_duration_args(args):
    """'5 минут' -> '5минут', '1ч 30 мин' -> '1ч', '30мин'. Одиночное «с» секундой не считается."""
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.isdigit() and i + 1 < len(args) and UNIT_WORD.match(args[i + 1].lower()):
            out.append(a + args[i + 1])
            i += 2
        else:
            out.append(a)
            i += 1
    return out


def timer_title(t):
    if t["kind"] == "custom":
        return f"⏰ {t.get('label') or 'Таймер'}"
    info = KINDS[t["kind"]]
    return f"{info['icon']} {info['title']}"


def handle_custom_timer(peer_id, user_id, text):
    args = merge_duration_args(text.split()[1:])
    total = None
    i = 0
    while i < len(args):
        tok = args[i].lower()
        if i > 0 and (tok.isdigit() or ":" in tok):
            break
        sec = parse_duration_token(tok)
        if sec is None:
            break
        total = (total or 0) + sec
        i += 1

    if total is None:
        send_msg(peer_id, TIMER_USAGE)
        return True
    if total < CUSTOM_MIN:
        send_msg(peer_id, f"⚠️ Минимальное время таймера — {CUSTOM_MIN} секунд.")
        return True
    if total > CUSTOM_MAX:
        send_msg(peer_id, "⚠️ Максимальное время таймера — 24 часа.")
        return True

    label = " ".join(args[i:]).strip()[:60]
    now = time.time()
    name = get_name(user_id)
    with timers_lock:
        mine = [
            t for t in timers
            if t["peer"] == peer_id and t["user"] == user_id and t["kind"] == "custom"
        ]
        if len(mine) >= CUSTOM_LIMIT:
            full = True
        else:
            full = False
            timers.append({
                "peer": peer_id,
                "user": user_id,
                "kind": "custom",
                "start": now,
                "due": now + total,
                "name": name,
                "label": label,
            })
            save_timers()
    if full:
        mid = send_msg(
            peer_id,
            f"⚠️ {name}, у вас уже {CUSTOM_LIMIT} своих таймеров. Освободите место: /отмена свои",
        )
        schedule_delete(mid, WARNING_TTL)
        return True

    what = f" «{label}»" if label else ""
    mid = send_msg(
        peer_id,
        f"⏰ {name}, таймер{what} на {fmt_dur(total)}. Напомню в {hhmm(now + total)}.",
    )
    schedule_delete(mid, NOTICE_TTL)
    return True


def handle_timer_commands(peer_id, user_id, clean_text, text=None):
    tokens = clean_text.split()
    if not tokens:
        return False
    head = tokens[0]
    text = text if text is not None else clean_text

    if head in ("/таймеры", "/timers"):
        now = time.time()
        with timers_lock:
            mine = [t for t in timers if t["peer"] == peer_id and t["user"] == user_id]
        if not mine:
            send_msg(peer_id, "⏳ У вас нет активных таймеров.")
            return True
        lines = ["Ваши активные таймеры:"]
        for t in sorted(mine, key=lambda x: x["due"]):
            lines.append(
                f"{timer_title(t)} · {hhmm(t['due'])} "
                f"({fmt_left(t['due'] - now)})"
            )
        send_msg(peer_id, "\n".join(lines))
        return True

    if head in ("/таймер", "/timer"):
        return handle_custom_timer(peer_id, user_id, text)

    if head in ("/отмена", "/cancel"):
        want = None
        if len(tokens) > 1:
            if tokens[1].startswith("бос"):
                want = "boss"
            elif tokens[1].startswith("исслед"):
                want = "research"
            elif tokens[1].startswith(("сво", "мои", "тайм")):
                want = "custom"
        with timers_lock:
            before = len(timers)
            timers[:] = [
                t for t in timers
                if not (
                    t["peer"] == peer_id and t["user"] == user_id
                    and (want is None or t["kind"] == want)
                )
            ]
            removed = before - len(timers)
            if removed:
                save_timers()
        send_msg(peer_id, f"🗑 Отменено таймеров: {removed}")
        return True

    return False


def scheduler_tick(now=None):
    """Один проход планировщика: срабатывают таймеры и удаляются старые сообщения бота."""
    now = time.time() if now is None else now

    due = []
    with timers_lock:
        keep = []
        for t in timers:
            (due if t["due"] <= now else keep).append(t)
        if due:
            timers[:] = keep
            save_timers()

    for t in due:
        if now - t["due"] > TIMER_STALE:
            continue
        who = f"[id{t['user']}|{t.get('name') or 'игрок'}]"
        if t["kind"] == "custom":
            label = t.get("label") or ""
            text = f"⏰ {who}, время вышло" + (f": {label}!" if label else "!")
        else:
            info = KINDS.get(t["kind"])
            if info is None:
                continue
            text = f"{who}, {info['remind']}"
        mid = send_msg(t["peer"], text)
        if mid:
            schedule_delete(mid, REMINDER_TTL, now)
        elif t.get("tries", 0) < 3:
            t["tries"] = t.get("tries", 0) + 1
            t["due"] = now + 15
            with timers_lock:
                timers.append(t)
                save_timers()

    ready = []
    with timers_lock:
        keep = []
        for d in deletions:
            (ready if d["at"] <= now else keep).append(d)
        if ready:
            deletions[:] = keep
            save_timers()
    for d in ready:
        delete_msg(d["mid"])

    try:
        post_period_tops(now)
    except Exception:
        print("⚠️ Ошибка публикации итогов:")
        traceback.print_exc()


def scheduler_loop():
    while True:
        try:
            scheduler_tick()
        except Exception:
            print("⚠️ Ошибка планировщика:")
            traceback.print_exc()
        time.sleep(2)


# ==========================================
# 📦 ОБРАБОТКА ПЕРЕСЛАННЫХ СООБЩЕНИЙ ИГРЫ (СБОРЫ И ТАЙМЕРЫ)
# ==========================================
def process_game_forwards(msg, peer_id, user_id, fwd_messages):
    """Обрабатывает пачку пересланных сообщений Атраксиса: сборы и таймеры.
    Возвращает True, если в сообщении было что-то распознано."""
    now = time.time()
    new_recs = []
    timer_events = []
    unparsed = 0
    recognized = False
    has_start = False

    for fwd in fwd_messages:
        if fwd.get("from_id") != ATRAXIS_ID:
            continue
        ftext = fwd.get("text") or ""
        fdate = int(fwd.get("date") or now)

        g = parse_gather(ftext)
        if g is not None:
            recognized = True
            if not g["ok"]:
                unparsed += 1
                continue
            new_recs.append({
                "k": make_key(user_id, fdate, ftext),
                "u": user_id,
                "t": fdate,
                "m": g["mode"],
                "g": g["g"],
                "bg": g["bg"],
                "s": g["s"],
                "bs": g["bs"],
                "n": g["n"],
                "z": g["z"],
                "mt": g["mt"],
            })
            continue

        kind = timer_kind(ftext)
        if kind:
            recognized = True
            timer_events.append((kind, fdate))
            continue

        if is_gather_start(ftext):
            has_start = True  # лог начала сбора: сам по себе ничего не значит, но и удалять его не нужно

    if not recognized:
        return False

    added, dupes = add_stat_records(peer_id, new_recs)
    name = get_name(user_id)

    notices = []   # короткие пометки без тега, исчезают через несколько секунд
    tagged = []    # сообщения с тегом (должны прийти уведомлением и повисеть)

    if added:
        text_ok = fmt_recorded(name, added)
        if dupes:
            text_ok += f"\n(уже было: {dupes})"
        notices.append(text_ok)
    elif dupes:
        notices.append(f"ℹ️ {name}, это уже учтено ({dupes})")
    if unparsed:
        notices.append(f"⚠️ Не смог разобрать сообщений: {unparsed}. Они остались в чате.")

    for kind, fdate in timer_events:
        info = KINDS[kind]
        status, due = set_timer(peer_id, user_id, kind, fdate, name)
        if status == "new":
            notices.append(f"{info['icon']} {name}, {info['logged']}. Напомню в {hhmm(due)}.")
        elif status == "dupe":
            notices.append(f"ℹ️ {name}, этот таймер уже стоит (в {hhmm(due)}).")
        else:
            tagged.append(f"{info['icon']} [id{user_id}|{name}], час уже прошёл — {info['late']}!")

    if notices:
        mid = send_msg(peer_id, "\n\n".join(notices))
        schedule_delete(mid, WARNING_TTL if unparsed else NOTICE_TTL)

    for line in tagged:
        mid = send_msg(peer_id, line)
        schedule_delete(mid, REMINDER_TTL)

    # Своё пересланное сообщение удаляем только если оно «голое»: без подписи, без лога начала сбора
    # и всё разобрано. Иначе сообщение остаётся (хвастаемся), но сбор всё равно записан.
    has_caption = bool((msg.get("text") or "").strip())
    if unparsed == 0 and not has_caption and not has_start:
        delete_msg(msg.get("id"))
    return True


# ==========================================
# 🔐 ШИФР ЦЕЗАРЯ (ТЕРМИНАЛ ИИ АВРОРЫ)
# ==========================================
# Как работает. Аврора в личке с Атраксисом присылает «Зашифрована фраза: «пёпэк»». Алфавит из 33 букв
# (с ё), сдвиг от 1 до 32 в любую сторону (сдвиг вперёд на k = сдвиг назад на 33-k, поэтому перебираем
# все 33 варианта). Дальше бот ищет настоящее слово по очереди так:
#   1) среди вариантов ответа (кнопки клавиатуры или строки под текстом), если они есть;
#   2) в словаре (встроенный список + слова, которые бот выучил после «Расшифровка верна»), с учётом
#      окончаний: «архив» и «архивы», «звезда» и «звёзды» считаются одним словом;
#   3) по «звучанию» (буквенные пары, обученные на словаре). Так бот отвечает, только если очень уверен.
# Если не уверен, ответ в игру НЕ отправляется, а CIPHER_NOTIFY_ID получает лучшие варианты.
CIPHER_ALPHA = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
CIPHER_N = len(CIPHER_ALPHA)
_C_IDX = {c: i for i, c in enumerate(CIPHER_ALPHA)}
_C_VOWELS = set("аеёиоуыэюя")
_C_ENDINGS = sorted(
    ["ами", "ями", "ов", "ев", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ою", "ею", "ые", "ие",
     "ых", "их", "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ую", "юю", "ы", "и", "а", "я", "у",
     "ю", "е", "о", "ь", "й"],
    key=len, reverse=True,
)
_C_FREQ = {
    "о": 10.97, "е": 8.45, "а": 8.01, "и": 7.35, "н": 6.70, "т": 6.26, "с": 5.47, "р": 4.73,
    "в": 4.54, "л": 4.34, "к": 3.49, "м": 3.21, "д": 2.98, "п": 2.81, "у": 2.62, "я": 2.01,
    "ы": 1.90, "ь": 1.74, "г": 1.69, "з": 1.65, "б": 1.59, "ч": 1.45, "й": 1.21, "х": 0.97,
    "ж": 0.94, "ш": 0.72, "ю": 0.64, "ц": 0.49, "щ": 0.36, "э": 0.33, "ф": 0.26, "ъ": 0.04,
    "ё": 0.25,
}
_C_UNI = {c: math.log(f / 3.03) for c, f in _C_FREQ.items()}
_C_BIGRAMS = set(
    "ст но то на ен ов ни ра во ко не по ор ро ть ли ал ре ер ан ат от ел ти ка ий ри ва ин ес ол "
    "ис ие ль ла ик ем да ча ны ых ам ог ом ту ск ак ус ур ци он ия ир ит ап ар ос ей ки ед ож те "
    "му ве ше нн тр пр пе ое ая ый ой ма ме ми мо ну".split()
)

# Встроенный словарь (имена существительные и прилагательные в начальной форме).
# Можно спокойно дописывать свои слова: через пробел или с новой строки.
CIPHER_SEED_WORDS = """
авангард аврора агент агрегат адаптер азот акула алгоритм аллея алмаз алмазы алтарь альянс амбар
ангар андроид антенна антиматерия аппарат арена армия артефакт архив архивы аспект атака атом аура
аэродром база балка баррикада бастион батарея башни башня бесконечность бетон биомасса бита блок
болото бомба бронеплита броня бумага бункер буря бусина бутылка вагон вакуум вакцина вектор вентиль
вестник ветер вечность вилка вирус вихрь вишня вода воздух воин волк волна вопрос ворон ворота
восточный время вспышка высота выстрел вышка вьюга газета галактика гараж гарнизон гвоздь гелий
генератор гильза глина гнездо голем голограмма голос гора горн город гранат граница гром груша гряда
гусеница датчик дверь двигатель действие демон дерево детектор диапазон диск добыча дождь доклад
долина дорога доспех дракон дрейф дрон дроны дуга дым железо жетон жидкий жидкость жизнь жила жрец
жук журнал забвение заброшка завод загадка заглушка задание задача залив замки замок западный запас
заряд засада защита защёлка заяц звезда звено зверь звёзды здоровье земля зенит зеркало зерно змея
знак знание золото зона иглу излучение изумруд икона иллюзия имплант импульс инженер инструмент
интерфейс инфекция искра испытание источник кабели кабель кабина калибр камень камни канал канистра
каньон капитан капсула капуста карантин каркас карта картофель каталог катушка квадрат кварц киборг
кинжал клетка климат клинок клуб ключ ключи книга кнопка кнут код колба колесо колонист колонна
колосс командир комета компас конвой контейнер контроль контур копьё корабли корабль корень корпус
косяк кошка кран кратер крепость кристалл кристаллы кровать крыло кубок кузница купол курс лабиринт
лаборатория лавина лагерь лампа ландшафт лента лес линза линия лиса литий лифт ловкость ловушка
ложка локатор лось лук люк лёгкий лёд маг магистраль магнит мантия маршрут маска масло мастерская
материал маяк маяки маятник медведь медик медь мембрана металл метеорит механизм механик меч мина
минерал миссия мишень многоножка модификатор модуль молекула молния молоко молот монета монстр
монстры морковь мост мотор муравей мусор мутант мутация муфта мышь набор навигатор навык наноробот
наследие насос недра нефть нить новичок нож нуль обелиск обломок оболочка образец обрыв объект объём
огонь огурец окно олень оптика опыт орбита оружейная оружие орёл осколок остров острый ответ отряд
охотник охотники охрана очаг очищение палуба память панели панель паника пароль парус пассажир паук
пепел перевал передатчик периметр перо перчатки песок пещера пещеры пилон пилот письмо плазма пламя
планета планеты плащ плита пломба плоскость плотина площадка поверхность погрузчик подвал подземелье
подъезд поле полигон помидор порох порт портал порядок посох пост поток потолок предмет привод
призрак призраки приманка прицел пробирка провод провода проводник прожектор пропасть пространство
протез протокол процессор прыжок пулемёт пульт пункт пустошь пустыня пыль работа равнина радар
радары радиация разведчик раздел разлом разум ракета рана расселина реагент реактор реакторы ребро
регулятор резерв река реликвия рельеф ремень ресурс решение риф робот роботы рой рубеж рубин рубины
руда руды руна ручей рыцарь рычаг сад самолёт сапоги сапфир сварка свекла свет светлый свиток связь
северный секрет сектор семья семя сенсор сенсоры серверная серебро сетка сигнал сигналы сила символ
синтез система скала скафандр склад склон скорость след слива слиток смерть смесь снаряд снег собака
собор сова солдат сосуд спираль спора спутник средство стабильность сталь станции станция статья
ствол стелла стена стержень стол страж стрела стрелок струна стул ступень субстрат сундук сфера
схема схрон счётчик съёмка таблетка таймер тайна тайник танк тарелка твёрдый тень терминал терраса
тигель титан титаны токсин тоннель топливник топливо топор торговец торпеда трава тревога труба
туман туманность туннель турбина турель тьма тяжёлый тёмный угол уголь удача узел улей улика
ультиматум упаковка уровень урон усилитель установка учёный ущелье фабрика фараон фермент фильтр
флаг флот фонарь формула форпост фрагмент фронт хаос хижина хищник хлеб храм хранилище хребет
хрупкий цвет цель центр цепь цех цикл цирк цистерна часы чаша чашка чаща червь чертёж чип чудак
чудовище шаблон шахта шахты шестерня шип шифратор шкала шкаф шлем шлюз шнур шпиль штаб шторм штурм
щель щит щука щётка эвакуация экзамен экран экраны эксперимент электрон эмблема эмиттер энергия
энциклопедия эпицентр эпоха эскадра этаж этап эфир эхо юбка юг южный юла юнит яблоко яд ядро якорь
яма янтари янтарь ящик ёж ёлка
"""

_C_WORD_RE = re.compile(r"[а-яё]+")
_cipher_lock = threading.RLock()
_cipher_state = {"known": set(), "stems": set(), "scorer": None, "learned": []}
_cipher_pending = {}   # {peer_id: {"answer": ..., "words": [...], "at": ...}} ответ ждёт вердикта игры
_cipher_seen = []      # id уже обработанных сообщений терминала


def _c_shift(text, k):
    """Сдвиг каждой буквы вперёд на k по 33-буквенному алфавиту. Остальные символы не трогаем."""
    return "".join(
        CIPHER_ALPHA[(_C_IDX[c] + k) % CIPHER_N] if c in _C_IDX else c for c in text
    )


def _c_shift_label(k):
    if k == 0:
        return "без сдвига"
    return f"+{k}" if k <= CIPHER_N // 2 else f"−{CIPHER_N - k}"


def _c_fold(w):
    return w.replace("ё", "е")


def cipher_stem(w):
    """Слово без окончания: «архивы» и «архив» дают одну основу, «звёзды» и «звезда» тоже."""
    w = _c_fold(w)
    for e in _C_ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[: -len(e)]
    return w


def _c_rules(w):
    """Очки «русскости» слова по правилам и число грубых нарушений (ыы, ьь, жы, 4 согласных подряд…)."""
    n = len(w)
    if not n:
        return 0.0, 0
    bad = 0
    sc = 0.0
    vow = sum(1 for c in w if c in _C_VOWELS)
    for c in w:
        sc += _C_UNI.get(c, -3.0)
    if w[0] in "ъыь":
        bad += 1
    if n >= 3 and vow == 0:
        bad += 1
    run_c = run_v = 0
    for i, c in enumerate(w):
        prev = w[i - 1] if i else ""
        nxt = w[i + 1] if i + 1 < n else ""
        if c in _C_VOWELS:
            run_v += 1
            run_c = 0
        elif c in "ьъ":
            run_c = run_v = 0
        else:
            run_c += 1
            run_v = 0
        if run_c >= 4 or run_v >= 4:
            bad += 1
        if c == "ь" and (prev in _C_VOWELS or prev in ("й", "ь", "ъ")):
            bad += 1
        if c == "ъ" and (nxt not in ("е", "ё", "ю", "я") or prev in _C_VOWELS):
            bad += 1
        if c in "ыяю" and prev in "жшчщ":
            bad += 1
        if c == "ы" and prev in "гкх":
            bad += 1
        if prev == c and c in "ыьъэюяё":
            bad += 1
        if c == "й" and prev and prev not in _C_VOWELS:
            bad += 1
        if prev and (prev + c) in _C_BIGRAMS:
            sc += 0.9
    ratio = vow / n
    if n >= 4 and (ratio < 0.2 or ratio > 0.75):
        bad += 1
    return sc - 6 * bad, bad


def _c_make_scorer(words):
    """Обучает модель буквенных пар и троек на словаре. Возвращает функцию слово -> (очки, нарушения)."""
    big, ctx, tri, tctx = Counter(), Counter(), Counter(), Counter()
    for w in words:
        s = "^" + w + "$"
        for a, b in zip(s, s[1:]):
            big[(a, b)] += 1
            ctx[a] += 1
        for a, b, c in zip(s, s[1:], s[2:]):
            tri[(a, b, c)] += 1
            tctx[(a, b)] += 1
    size = CIPHER_N + 1

    def score(w):
        s = "^" + w + "$"
        lp = 0.0
        for a, b, c in zip(s, s[1:], s[2:]):
            p2 = (big[(b, c)] + 0.3) / (ctx[b] + 0.3 * size)
            p3 = (tri[(a, b, c)] + 1.5 * p2) / (tctx[(a, b)] + 1.5)
            lp += math.log(p3)
        rs, bad = _c_rules(w)
        return lp + 0.25 * rs, bad

    return score


def _c_rebuild():
    """Пересобирает словарь и модель (после запуска и после каждого выученного слова)."""
    with _cipher_lock:
        words = set(_C_WORD_RE.findall(CIPHER_SEED_WORDS.lower())) | set(_cipher_state["learned"])
        _cipher_state["known"] = {_c_fold(w) for w in words}
        _cipher_state["stems"] = {cipher_stem(w) for w in words if len(cipher_stem(w)) >= 4}
        _cipher_state["scorer"] = _c_make_scorer([w for w in words if len(w) >= 3])


def load_cipher_words():
    raw = _load_json(CIPHER_WORDS_FILE, {})
    with _cipher_lock:
        _cipher_state["learned"] = [w for w in raw.get("words", []) if isinstance(w, str)]
    _c_rebuild()


def learn_cipher_words(words):
    """Добавляет слова в выученные. Возвращает, сколько действительно новых."""
    added = 0
    with _cipher_lock:
        for w in words:
            w = w.lower().strip()
            if len(w) < 3 or not _C_WORD_RE.fullmatch(w):
                continue
            if _c_fold(w) in _cipher_state["known"]:
                continue
            _cipher_state["learned"].append(w)
            _cipher_state["known"].add(_c_fold(w))
            added += 1
        if added:
            _save_json(CIPHER_WORDS_FILE, {"words": _cipher_state["learned"]})
    if added:
        _c_rebuild()
    return added


def forget_cipher_words(words):
    removed = 0
    with _cipher_lock:
        for w in words:
            w = w.lower().strip()
            if w in _cipher_state["learned"]:
                _cipher_state["learned"].remove(w)
                removed += 1
        if removed:
            _save_json(CIPHER_WORDS_FILE, {"words": _cipher_state["learned"]})
    if removed:
        _c_rebuild()
    return removed


def _c_known_level(w):
    """2: слово есть в словаре, 1: есть с другим окончанием (мн. число, падеж), 0: неизвестно."""
    wf = _c_fold(w)
    with _cipher_lock:
        if wf in _cipher_state["known"]:
            return 2
        s = cipher_stem(wf)
        if len(s) >= 4 and s in _cipher_state["stems"]:
            return 1
    return 0


def cipher_candidates(phrase, scorer=None):
    """Все 33 варианта расшифровки, лучшие по «русскости» первыми."""
    phrase = phrase.lower().strip()
    with _cipher_lock:
        scorer = scorer or _cipher_state["scorer"]
    cands = []
    for k in range(CIPHER_N):
        dec = _c_shift(phrase, k)
        words = _C_WORD_RE.findall(dec)
        if not words:
            continue
        sc = 0.0
        bad = 0
        levels = []
        for w in words:
            s, b = scorer(w)
            sc += s
            bad += b
            levels.append(_c_known_level(w))
        cands.append({
            "k": k, "text": dec, "score": sc, "bad": bad,
            "exact": min(levels) == 2, "known": min(levels) >= 1,
        })
    cands.sort(key=lambda c: -c["score"])
    return cands


def solve_cipher(phrase, options=None, scorer=None):
    """Ищет расшифровку. Возвращает {"answer", "how", "sure", "cands", "note"}.
    sure=True означает, что бот готов отправить answer в игру без подтверждения."""
    cands = cipher_candidates(phrase, scorer)
    res = {"answer": None, "how": None, "sure": False, "cands": cands, "note": ""}
    if not cands:
        res["note"] = "в фразе нет русских букв"
        return res

    # 1) варианты ответа из самой игры
    if options:
        opt_map = {}
        for o in options:
            opt_map.setdefault(_c_fold(o.lower().strip()), o.strip())
        hits = [c for c in cands if _c_fold(c["text"]) in opt_map]
        if not hits:
            stems = {}
            for key, label in opt_map.items():
                st = cipher_stem(key)
                if len(st) >= 4:
                    stems.setdefault(st, label)
            hits = [c for c in cands if cipher_stem(_c_fold(c["text"])) in stems]
            if hits:
                key = cipher_stem(_c_fold(hits[0]["text"]))
                res.update(answer=stems[key], how="options", sure=len(hits) == 1, best=hits[0])
                return res
        else:
            best = hits[0]
            res.update(
                answer=opt_map[_c_fold(best["text"])], how="options", best=best,
                sure=len(hits) == 1 or best["score"] - hits[1]["score"] >= 3,
            )
            return res
        res["note"] = "ни один вариант из игры не совпал с расшифровкой"

    # 2) словарь
    pool = [c for c in cands if c["exact"]] or [c for c in cands if c["known"]]
    if pool:
        best = pool[0]
        sure = len(pool) == 1 or best["score"] - pool[1]["score"] >= 3
        res.update(answer=best["text"], how="dict", sure=sure, best=best)
        return res

    # 3) «на слух»
    best = cands[0]
    margin = best["score"] - cands[1]["score"] if len(cands) > 1 else 99.0
    res.update(
        answer=best["text"], how="model", best=best,
        sure=CIPHER_GUESS and best["bad"] == 0 and margin >= CIPHER_MARGIN,
    )
    res["margin"] = margin
    return res


HOW_LABELS = {
    "options": "совпало с вариантом из игры",
    "dict": "слово есть в словаре",
    "model": "угадано по звучанию",
}


def format_cipher_candidates(cands, limit=5):
    lines = []
    for i, c in enumerate(cands[:limit], 1):
        mark = "📗" if c["exact"] else ("📙" if c["known"] else "▫️")
        lines.append(f"{i}) {mark} {c['text']} (сдвиг {_c_shift_label(c['k'])})")
    return "\n".join(lines)


def cipher_notify(text):
    """Пишет админу. Если ему написать нельзя (VK не пускает), дублирует в рабочий чат."""
    if send_msg(CIPHER_NOTIFY_ID, text) is None:
        send_msg(RUNES_PEER_IDS[-1], text)


def extract_cipher_options(msg, text):
    """Варианты ответа: кнопки клавиатуры сообщения, а если их нет, строки под «Выбери расшифровку»."""
    opts = []
    kb = msg.get("keyboard") or {}
    for row in kb.get("buttons") or []:
        for btn in row:
            act = btn.get("action", {}) if isinstance(btn, dict) else {}
            label = (act.get("label") or "").strip()
            if label:
                opts.append(label)
    if not opts:
        i = text.lower().find("выбери расшифровку")
        if i >= 0:
            for line in text[i:].split("\n")[1:]:
                line = re.sub(r"^[\s\-–•*\d.)]+", "", line).strip()
                if line:
                    opts.append(line)
    return opts


def _cipher_worker(peer_id, phrase, options):
    try:
        res = solve_cipher(phrase, options)
        cands = res["cands"]
        if res["answer"] and res["sure"]:
            time.sleep(random.uniform(*CIPHER_DELAY))
            if send_msg(peer_id, res["answer"]) is None:
                cipher_notify(f"⚠️ Шифр «{phrase}»: расшифровал как «{res['answer']}», но отправить в игру не вышло.")
                return
            words = _C_WORD_RE.findall(res["answer"].lower())
            _cipher_pending[peer_id] = {"answer": res["answer"], "words": words, "at": time.time()}
            print(f"🔐 Шифр «{phrase}» → «{res['answer']}» ({HOW_LABELS.get(res['how'], res['how'])})")
            return
        head = f"🔐 Шифр «{phrase}»: не уверен, в игру ничего не отправил."
        if res["note"]:
            head += f"\n({res['note']})"
        if options:
            head += "\nВарианты из игры: " + ", ".join(options)
        cipher_notify(head + "\n\nЛучшие расшифровки:\n" + format_cipher_candidates(cands)
                      + "\n\n📗 слово из словаря, 📙 то же слово с другим окончанием."
                      + "\nПравильное слово можно добавить: /шифр+ слово")
        print(f"🔐 Шифр «{phrase}»: не уверен, отправил варианты в {CIPHER_NOTIFY_ID}")
    except Exception:
        print("⚠️ Ошибка решателя шифра:")
        traceback.print_exc()


def handle_cipher_message(msg, peer_id, text):
    """Сообщения Атраксиса: терминал с шифром и вердикт игры. True, если сообщение наше."""
    low = text.lower()
    if "зашифрована фраза" in low:
        if not CIPHER_AUTO:
            return False
        mid = msg.get("id")
        with _cipher_lock:
            if mid in _cipher_seen:
                return True
            _cipher_seen.append(mid)
            del _cipher_seen[:-50]
        m = re.search(r"[«“\"]([^»”\"]+)[»”\"]", text)
        if not m:
            print("⚠️ Шифр: не нашёл фразу в «кавычках».")
            return True
        options = extract_cipher_options(msg, text)
        threading.Thread(
            target=_cipher_worker, args=(peer_id, m.group(1).strip(), options), daemon=True
        ).start()
        return True

    if "расшифровка" in low:
        pend = _cipher_pending.pop(peer_id, None)
        if not pend or time.time() - pend["at"] > 900:
            return True
        if "неверн" in low or "не верн" in low or "❌" in text:
            cipher_notify(
                f"❌ Шифр: я ответил «{pend['answer']}», но игра сказала, что это неверно. "
                f"Слово в словарь не добавлял."
            )
        elif "верна" in low or "✅" in text:
            if learn_cipher_words(pend["words"]):
                print(f"🔐 Выучил слово: {', '.join(pend['words'])}")
        return True
    return False


def handle_cipher_forward(msg, peer_id, user_id, fwd_messages):
    """Рабочий чат: игрок переслал терминал Авроры. Отвечаем расшифровкой и через минуту
    удаляем и ответ, и пересланное сообщение. True, если в пересылке был терминал с шифром."""
    if not CIPHER_FORWARD:
        return False
    found = False
    for fwd in fwd_messages:
        if fwd.get("from_id") != ATRAXIS_ID:
            continue
        ftext = fwd.get("text") or ""
        if "зашифрована фраза" not in ftext.lower():
            continue
        m = re.search(r"[«“\"]([^»”\"]+)[»”\"]", ftext)
        if not m:
            continue
        found = True
        phrase = m.group(1).strip()
        res = solve_cipher(phrase)
        if not res["cands"]:
            text = f"🔐 «{phrase}»: в фразе нет русских букв."
        elif res["sure"]:
            text = f"🔐 «{phrase}» → {res['answer']}"
            if res["how"] == "model":
                text += "\n(угадано по звучанию, слова нет в словаре)"
        else:
            text = (
                f"🔐 «{phrase}»: не уверен. Похожие варианты:\n"
                + format_cipher_candidates(res["cands"], 3)
            )
        schedule_delete(send_msg(peer_id, text), CIPHER_TTL)
        print(f"🔐 Шифр «{phrase}» → «{res['answer']}» ({res['how']}, sure={res['sure']}) для {user_id}")
    if found:
        schedule_delete(msg.get("id"), CIPHER_TTL)
    return found


def handle_cipher_command(peer_id, user_id, text):
    """/шифр <фраза> — показать расшифровки; /шифр+ и /шифр- <слова> — словарь (админ)."""
    tokens = text.split()
    if not tokens or tokens[0].lower() not in ("/шифр", "/шифр+", "/шифр-"):
        return False
    head, args = tokens[0].lower(), tokens[1:]

    if head in ("/шифр+", "/шифр-"):
        if user_id != ADMIN_ID:
            send_msg(peer_id, "⛔ У вас нет доступа к этой команде.")
            return True
        if not args:
            send_msg(peer_id, f"⚠️ Формат команды: {head} <слово> [слово ...]")
            return True
        if head == "/шифр+":
            n = learn_cipher_words(args)
            send_msg(peer_id, f"✅ Добавлено новых слов: {n} (остальные уже были в словаре).")
        else:
            n = forget_cipher_words(args)
            send_msg(peer_id, f"🗑 Удалено выученных слов: {n} (встроенные слова не удаляются).")
        return True

    if not args:
        send_msg(peer_id, "⚠️ Формат команды: /шифр <зашифрованное слово>")
        return True
    res = solve_cipher(" ".join(args))
    if not res["cands"]:
        send_msg(peer_id, "⚠️ В тексте нет русских букв.")
        return True
    verdict = "✅ Уверен" if res["sure"] else "🤔 Не уверен"
    send_msg(
        peer_id,
        f"🔐 {verdict}: {res['answer']} ({HOW_LABELS.get(res['how'], '')})\n\n"
        + format_cipher_candidates(res["cands"]),
    )
    return True


# ==========================================
# 📨 ОБРАБОТКА ОДНОГО СООБЩЕНИЯ
# ==========================================
def handle_message(msg):
    global last_reset_time

    text = msg.get("text", "").strip()
    user_id = msg.get("from_id")
    peer_id = msg.get("peer_id")

    if not user_id:
        return

    fwd_messages = list(msg.get("fwd_messages", []))
    if "reply_message" in msg:
        fwd_messages.append(msg["reply_message"])

    clean_text = text.lower()

    # «дел» ответом на сообщение (только админ): удалить его и саму команду
    if handle_delete_command(msg, peer_id, user_id, clean_text):
        return

    # Терминал шифра от ИИ Авроры и вердикт игры приходят от Атраксиса (обычно в личке с ним)
    if user_id == ATRAXIS_ID and handle_cipher_message(msg, peer_id, text):
        return

    # ----------------------------------------------------
    # 1. ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ В БЕСЕДАХ
    # ----------------------------------------------------
    if clean_text in ["/id", "/чат", "/chat"]:
        send_msg(peer_id, f"🆔 **ID этого чата:** `{peer_id}`")
        return

    if clean_text in [
        "/статус",
        "/прогресс",
        "/руны",
        "📊 статус рун",
        "статус рун",
    ]:
        with data_lock:
            status_msg = get_status_text(runes)
        send_msg(peer_id, status_msg)
        return

    if clean_text in ["/пинг", "/ping"]:
        uptime_sec = int(time.time() - START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}ч {minutes}м {seconds}с"

        send_msg(
            peer_id,
            f"🏓 **ПОНГ!** Юзербот работает стабильно.\n⏱ **Время онлайн:** {uptime_str}",
        )
        return

    # Помощь, статистика, топ и таймеры работают только в рабочих чатах
    if peer_id in RUNES_PEER_IDS:
        if handle_help_command(peer_id, user_id, clean_text):
            return
        if handle_stats_command(peer_id, user_id, clean_text):
            return
        if handle_top_command(peer_id, user_id, clean_text):
            return
        if handle_timer_commands(peer_id, user_id, clean_text, text):
            return
        if handle_cipher_command(peer_id, user_id, text):
            return
        if handle_cipher_forward(msg, peer_id, user_id, fwd_messages):
            return

    # ----------------------------------------------------
    # 2. АДМИН-КОМАНДЫ (/код, /все, /add, /удалить, /очистить, /кто)
    # ----------------------------------------------------
    if clean_text.startswith(("/код", "/все", "/add", "/удалить", "/очистить", "/кто")):
        if user_id != ADMIN_ID:
            send_msg(peer_id, "⛔ У вас нет доступа к этой команде.")
            return

        if clean_text == "/кто":
            lines = [f"👤 Отправитель: {user_id}"]
            for i, fwd in enumerate(fwd_messages, 1):
                lines.append(f"📨 Пересланное {i}: from_id = {fwd.get('from_id')}")
            if len(lines) == 1:
                lines.append("Перешлите сообщение вместе с командой /кто, и я покажу, чьё оно.")
            send_msg(peer_id, "\n".join(lines))
            return

        if clean_text == "/код":
            lines = []
            with data_lock:
                for a in ASPECTS:
                    if a in runes:
                        val = runes[a]
                        code_val = val.get("code", "???") if isinstance(val, dict) else str(val)
                        t_val = val.get("time", 0) if isinstance(val, dict) else 0
                        t_str = time.strftime("%H:%M", time.localtime(t_val)) if t_val else "время не указано"
                        lines.append(f"• {a}: `{code_val}` (в {t_str})")
                    else:
                        lines.append(f"• {a}: *нет*")
            msg_text = "💾 **Все собранные коды (Админ):**\n\n" + "\n".join(lines)
            send_msg(peer_id, msg_text)
            return

        if clean_text.startswith("/все"):
            broadcast_text = text[4:].strip()
            if broadcast_text:
                send_to_work_chats(f"📢 **Общее сообщение:**\n\n{broadcast_text}")
                send_msg(peer_id, "✅ Сообщение отправлено во все рабочие чаты.")
            else:
                send_msg(peer_id, "⚠️ Формат команды: /все <текст рассылки>")
            return

        if clean_text.startswith("/add"):
            parts = text.split()
            if len(parts) < 3:
                send_msg(
                    peer_id,
                    "⚠️ Формат команды: /add <аспект> <код>\n"
                    f"Аспекты: {', '.join(ASPECTS)}",
                )
                return

            aspect = parts[1].capitalize()
            code = parts[2]
            if aspect not in ASPECTS:
                send_msg(
                    peer_id,
                    f"⚠️ Неизвестный аспект «{parts[1]}».\n"
                    f"Доступные: {', '.join(ASPECTS)}",
                )
                return

            trigger_broadcast = False
            with data_lock:
                was_complete = len(runes) == len(ASPECTS)
                runes[aspect] = {"code": code, "time": time.time()}
                save_data(runes)
                status_str = get_status_text(runes)

                if len(runes) == len(ASPECTS) and not was_complete:
                    trigger_broadcast = True

            send_msg(
                peer_id,
                f"✅ Аспект «{aspect}» установлен вручную!\n\n{status_str}",
            )

            if trigger_broadcast:
                broadcast_notification()
            return

        if clean_text.startswith("/удалить"):
            parts = text.split()
            if len(parts) < 2:
                send_msg(
                    peer_id,
                    "⚠️ Формат команды: /удалить <аспект>\n"
                    f"Аспекты: {', '.join(ASPECTS)}",
                )
                return

            aspect = parts[1].capitalize()
            if aspect not in ASPECTS:
                send_msg(
                    peer_id,
                    f"⚠️ Неизвестный аспект «{parts[1]}».\n"
                    f"Доступные: {', '.join(ASPECTS)}",
                )
                return

            with data_lock:
                if aspect in runes:
                    del runes[aspect]
                    save_data(runes)
                    removed = True
                else:
                    removed = False
                status_str = get_status_text(runes)

            if removed:
                send_msg(peer_id, f"🗑 Аспект «{aspect}» удалён.\n\n{status_str}")
            else:
                send_msg(
                    peer_id,
                    f"ℹ️ Аспекта «{aspect}» и так нет в базе.\n\n{status_str}",
                )
            return

        if clean_text == "/очистить":
            with data_lock:
                runes.clear()
                last_reset_time = time.time()
                save_data(runes)
            send_msg(peer_id, "🗑 База рун полностью очищена.")
            return

    # ----------------------------------------------------
    # 3. ОБРАБОТКА ТРИГГЕРА АВРОРЫ (ВЫДАЧА КОМБО ИГРОКУ)
    # ----------------------------------------------------
    current_time = time.time()
    trigger_status = check_aurora_trigger(fwd_messages, current_time, user_id)

    if trigger_status == "EXPIRED":
        send_msg(
            peer_id,
            "⚠️ Это сообщение от Авроры слишком старое!\n"
            "Принимаются только свежие сообщения (не старше 15 минут).",
        )
        return

    if trigger_status == "VALID":
        with data_lock:
            is_complete = len(runes) == len(ASPECTS)
            combo_str = " ".join(
                [
                    (runes[a]["code"] if isinstance(runes[a], dict) else str(runes[a]))
                    for a in ASPECTS
                    if a in runes
                ]
            )
            runes_len = len(runes)

        if is_complete:
            send_msg(
                peer_id,
                f"✅ Запрос принят! Вот все 5 руноблоков:\n\n{combo_str}",
            )
        else:
            send_msg(
                peer_id,
                f"⚠️ Собраны еще не все руны ({runes_len}/5). Ожидайте рассылки!",
            )
        return

    # ----------------------------------------------------
    # 4. ОБРАБОТКА ИВЕНТОВ, СБРОСА И СБОРА РУН
    # ----------------------------------------------------

    # --- А) ИВЕНТЫ И СБРОСЫ (Общий чат RESET_PEER_ID, ТОЛЬКО от Авроры) ---
    if peer_id == RESET_PEER_ID:
        if user_id != AURORA_ID:
            # Чужие сообщения игнорируем. Если пишет какое-то сообщество, оставляем след в логе
            if user_id < 0 and any(trigger in clean_text for trigger in RESET_TRIGGERS):
                print(
                    f"ℹ️ Триггер сброса найден, но отправитель не Аврора "
                    f"(from_id={user_id}) — игнорирую."
                )
            return

        # Сброс алтаря
        if any(trigger in clean_text for trigger in RESET_TRIGGERS):
            reset_time_str = time.strftime("%H:%M:%S", time.localtime())

            with data_lock:
                old_count = len(runes)
                runes.clear()
                last_reset_time = time.time()
                save_data(runes)

            response = (
                f"🚨 **Алтарь вскрыт! ({reset_time_str})**\n\n"
                f"• База рун полностью очищена (удалено: {old_count})."
            )
            send_to_work_chats(response)
            return

        # Пробуждение колосса (@online)
        if (
            "пробудился" in clean_text
            and "колосс" in clean_text
            and "повержен" not in clean_text
            and "уничтожен" not in clean_text
            and "заверш" not in clean_text
        ):
            boss_match = re.search(r"Пробудился\s+([^.\n!]+)", text, re.IGNORECASE)
            coords_match = re.search(
                r"координаты:\s*([-\d]+:[-\d]+)", text, re.IGNORECASE
            )

            if boss_match and coords_match:
                boss_name = boss_match.group(1).strip()
                coords = coords_match.group(1).strip()

                colossus_msg = (
                    f"@online ☄️ **ОБНАРУЖЕНА УГРОЗА!**\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👹 **Существо:** {boss_name}\n"
                    f"📍 **Координаты:** `{coords}`"
                )
                send_to_work_chats(colossus_msg)

        # Босс на стене (@all)
        elif (
            (
                "колоссальная сущность" in clean_text
                or "цель операции:" in clean_text
            )
            and "уничтожена" not in clean_text
            and "повержен" not in clean_text
            and "заверш" not in clean_text
        ):
            event_msg = (
                f"@all 🛑 **БОЛЬШОЙ БОСС НА СТЕНЕ!**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👾 В группе пробудился мировой босс!\n"
                f"⚔️ Переходите к посту на стене и вступайте в бой."
            )
            send_to_work_chats(event_msg)

        # Аномальные возмущения (@all)
        elif (
            "аномальные возмущения" in clean_text
            and "заверш" not in clean_text
            and "изучены" not in clean_text
        ):
            event_msg = (
                f"@all 🧪 **НОВОЕ СОБЫТИЕ НА СТЕНЕ!**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔬 На стене группы появилось аномальное событие.\n"
                f"🚀 Пора отправляться на исследование!"
            )
            send_to_work_chats(event_msg)

        # Турнир на Арене (@all)
        elif (
            (
                "турнир на арене" in clean_text
                or "распорядителю арены" in clean_text
            )
            and "окончен" not in clean_text
            and "завершен" not in clean_text
        ):
            event_msg = (
                f"@all 🏟 **ТУРНИР НА АРЕНЕ!**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚔️ Через 30 минут начинается турнир на арене Хром-Гарна!"
            )
            send_to_work_chats(event_msg)

    # --- Б) СБОРЫ И ТАЙМЕРЫ (Рабочие чаты, ТОЛЬКО пересланное от Атраксиса) ---
    if peer_id in RUNES_PEER_IDS:
        if process_game_forwards(msg, peer_id, user_id, fwd_messages):
            return

        # --- В) СБОР РУН ---
        for fwd in fwd_messages:
            # Принимаем только сообщения, пересланные от сообщества Атраксис
            if fwd.get("from_id") != ATRAXIS_ID:
                continue

            fwd_date = fwd.get("date", int(current_time))

            # Игнорируем сообщения, отправленные до последнего сброса
            if fwd_date <= last_reset_time:
                continue

            fwd_text = fwd.get("text", "")
            fwd_text_lower = fwd_text.lower()

            if (current_time - fwd_date) <= 1800 or user_id == ADMIN_ID:
                if (
                    "реликвия активна" in fwd_text_lower
                    and "аспект:" in fwd_text_lower
                ):
                    match = re.search(
                        r"Аспект:\s*([А-Яа-яЁё]+)", fwd_text, re.IGNORECASE
                    )
                    if not match:
                        continue

                    aspect = match.group(1).capitalize()
                    code = None

                    code_match = re.search(r"\((.*?)\)", fwd_text)
                    if code_match:
                        code = code_match.group(1).strip()
                    else:
                        lines = [
                            line.strip()
                            for line in fwd_text.splitlines()
                            if line.strip()
                        ]
                        for idx, line in enumerate(lines):
                            if "аспект:" in line.lower():
                                if idx + 1 < len(lines):
                                    code = lines[idx + 1]
                                break

                    if aspect in ASPECTS and code:
                        trigger_broadcast = False
                        with data_lock:
                            was_complete = len(runes) == len(ASPECTS)
                            runes[aspect] = {"code": code, "time": fwd_date}
                            save_data(runes)
                            status_str = get_status_text(runes)

                            if len(runes) == len(ASPECTS) and not was_complete:
                                trigger_broadcast = True

                        response_msg = f"✅ Аспект «{aspect}» сохранен!\n\n{status_str}"
                        send_msg(peer_id, response_msg)
                        delete_msg(msg.get("id"))

                        if trigger_broadcast:
                            broadcast_notification()
                        break


# ==========================================
# 🚀 ИНИЦИАЛИЗАЦИЯ И ОСНОВНОЙ ЦИКЛ
# ==========================================
vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()

with data_lock:
    runes = load_data()
load_stats()
load_timers()
load_cipher_words()


def main():
    print(
        f"🤖 Юзербот запущен. Admin ID: {ADMIN_ID} | "
        f"Aurora ID: {AURORA_ID} | Atraxis ID: {ATRAXIS_ID}"
    )
    threading.Thread(target=scheduler_loop, daemon=True).start()

    while True:
        try:
            longpoll = VkLongPoll(vk_session)

            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW or event.from_me:
                    continue

                # Ошибка в одном сообщении не должна перезапускать весь longpoll
                try:
                    with api_lock:
                        msg_data = vk.messages.getById(message_ids=event.message_id)
                    if not msg_data.get("items"):
                        continue
                    handle_message(msg_data["items"][0])
                except Exception:
                    print(f"⚠️ Ошибка обработки сообщения {event.message_id}:")
                    traceback.print_exc()

        except Exception as e:
            print(f"⚠️ Ошибка VK / LongPoll: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
