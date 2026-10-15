import hashlib
import json
import os
import re
import threading
import time
import traceback
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

    return {
        "mode": mode,
        "g": grades,
        "bg": bonus_res,
        "s": spheres + bonus_sph,
        "bs": bonus_sph,
        "n": notes,
        "z": signs,
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
        out[mode] = {"c": 0, "g": [0, 0, 0], "bg": 0, "s": 0, "bs": 0, "n": 0, "z": 0}
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
    total_res = sum(g)

    # Первая строка длинная и без эмодзи: VK тянет пузырь по самой длинной строке,
    # а строки с эмодзи считает уже, чем рисует, и переносит их.
    lines = [f"Стата · {scope_title} · {period_title}", ""]
    if c == 0:
        lines.append("Сборов пока нет.")
        return "\n".join(lines)

    lines += [f"🔢 Сборов: {c}", f"{PICK} Ресурсов: {total_res}", ""]
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
    """Топ игроков чата за период: [(имя, всего ресурсов, фиолетовых)].
    Сортировка по общему числу ресурсов, при равенстве по фиолетовым."""
    with stats_lock:
        recs = list(stats.get(str(peer_id), []))
    agg = {}
    for r in recs:
        if r["t"] < start or (end is not None and r["t"] >= end):
            continue
        a = agg.setdefault(r["u"], [0, 0])
        a[0] += sum(r["g"])
        a[1] += r["g"][2]
    rows = [(u, tot, vio) for u, (tot, vio) in agg.items() if tot > 0]
    rows.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return [(short_name(get_full_name(u)), tot, vio) for u, tot, vio in rows[:limit]]


def format_top(rows, head):
    """Две строки на игрока, имя на всю ширину пузыря, медаль во второй строке:
    Иван Иванов
    🥇 ⛏️ 200 · 🟣 20"""
    lines = [head, ""]
    if not rows:
        lines.append("Пока нет ресурсов.")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, tot, vio) in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(name)
        lines.append(f"{mark} {PICK} {tot} · 🟣 {vio}")
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
    "Просто пересылайте сюда сообщения от Атраксиса: бот сам запишет ресурсы, "
    "поставит таймер на босса и исследование и удалит ваше сообщение. "
    "Если приписать к пересланному подпись, сообщение останется.\n\n"
    "📊 /стата [общая] [сегодня | вчера | неделя | месяц | всё] — статистика сборов\n"
    "🏆 /топ — топ по ресурсам за неделю, /топ месяц, /топ всё (за всё время)\n"
    "⏳ /таймеры — ваши активные таймеры\n"
    "⏰ /таймер 45м кузница — свой таймер (45м, 1ч30м, 90)\n"
    "🗑 /отмена [босс | исследование | свои] — отменить таймеры\n"
    "📜 /статус — сколько рун собрано\n"
    "🏓 /пинг — проверить, что бот жив"
)

HELP_ADMIN = (
    "🛠 Админ-команды\n"
    "\n"
    "💾 /код — все собранные коды рун\n"
    "➕ /add <аспект> <код> — внести руну вручную\n"
    "➖ /удалить <аспект> — удалить одну руну\n"
    "🗑 /очистить — очистить базу рун\n"
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
    if "⚔️ Урон по боссу" in low:
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
            })
            continue

        kind = timer_kind(ftext)
        if kind:
            recognized = True
            timer_events.append((kind, fdate))
            continue

        if is_gather_start(ftext):
            recognized = True  # сообщение о начале сбора просто удаляем

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

    # Своё пересланное сообщение удаляем только если оно «голое»: без подписи и всё разобрано.
    # Если игрок что-то приписал («круто!», хвастаемся), сообщение остаётся, но сбор всё равно записан.
    has_caption = bool((msg.get("text") or "").strip())
    if unparsed == 0 and not has_caption:
        delete_msg(msg.get("id"))
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
