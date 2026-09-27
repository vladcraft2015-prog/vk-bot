import json
import os
import re
import threading
import time
import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll
from vk_api.utils import get_random_id

# --- НАСТРОЙКИ ЮЗЕРБОТА ---
# Токен забирается из переменных окружения Bothost (VK_TOKEN)
TOKEN = os.environ.get("VK_TOKEN", "ВАШ_ТОКЕН_ПОЛЬЗОВАТЕЛЯ")
ADMIN_ID = 550216110
DATA_FILE = "runes_data.json"

# ID бесед
RESET_PEER_ID = 2000000012  # Общий чат (отслеживание событий ИИ Аврора и сбросов)
RUNES_PEER_IDS = [2000000011, 2000000013]  # Рабочие чаты сбора рун и анонсов

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

START_TIME = time.time()
last_boss_info = None
data_lock = threading.Lock()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
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
            json.dump(
                {"runes": runes},
                f,
                ensure_ascii=False,
                indent=4,
            )
    except Exception as e:
        print(f"⚠️ Ошибка сохранения базы рун: {e}")


def send_msg(peer_id, text):
    try:
        params = {
            "peer_id": peer_id,
            "message": text,
            "random_id": get_random_id(),
        }
        vk.messages.send(**params)
    except Exception as e:
        print(f"⚠️ Ошибка отправки в {peer_id}: {e}")


def send_to_work_chats(text):
    for pid in RUNES_PEER_IDS:
        send_msg(pid, text)


def delete_msg(message_id):
    try:
        vk.messages.delete(message_ids=[message_id], delete_for_all=1)
    except Exception as e:
        print(f"⚠️ Не удалось удалить сообщение {message_id}: {e}")


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


# --- ИНИЦИАЛИЗАЦИЯ ПОЛЬЗОВАТЕЛЯ ---
vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()

try:
    group_info = vk.utils.resolveScreenName(screen_name="ai_aurora")
    AURORA_ID = -group_info["object_id"]
    print(f"✅ ID игрового бота Аврора (ai_aurora): {AURORA_ID}")
except Exception as e:
    print(f"⚠️ Ошибка определения ID Авроры: {e}")
    AURORA_ID = None

with data_lock:
    runes = load_data()

print(f"🤖 Юзербот запущен. Админ ID: {ADMIN_ID}")

# --- ОСНОВНОЙ ЦИКЛ (USER LONGPOLL) ---
longpoll = VkLongPoll(vk_session)

while True:
    try:
        for event in longpoll.listen():
            if event.type == VkEventType.MESSAGE_NEW:
                # Игнорируем собственные сообщения аккаунта
                if event.from_me:
                    continue

                try:
                    msg_data = vk.messages.getById(
                        message_ids=event.message_id
                    )
                    if not msg_data.get("items"):
                        continue
                    msg = msg_data["items"][0]
                except Exception as e:
                    print(f"Ошибка получения сообщения: {e}")
                    continue

                text = msg.get("text", "").strip()
                user_id = msg.get("from_id")
                peer_id = msg.get("peer_id")

                if not user_id:
                    continue

                fwd_messages = msg.get("fwd_messages", [])
                if "reply_message" in msg:
                    fwd_messages.append(msg["reply_message"])

                clean_text = text.lower()

                # ----------------------------------------------------
                # 1. ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫВ БЕСЕДАХ
                # ----------------------------------------------------
                if clean_text in ["/id", "/чат", "/chat"]:
                    send_msg(peer_id, f"🆔 **ID этого чата:** `{peer_id}`")
                    continue

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
                    continue

                if clean_text in ["/пинг", "/ping"]:
                    uptime_sec = int(time.time() - START_TIME)
                    hours, remainder = divmod(uptime_sec, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    uptime_str = f"{hours}ч {minutes}м {seconds}с"

                    send_msg(
                        peer_id,
                        f"🏓 **ПОНГ!** Юзербот работает стабильно.\n⏱ **Время онлайн:** {uptime_str}",
                    )
                    continue

                if clean_text in ["/босс", "/boss", "/координаты"]:
                    if last_boss_info:
                        msg_text = (
                            f"👹 **Последний колосс:** {last_boss_info['name']}\n"
                            f"📍 **Координаты:** `{last_boss_info['coords']}`\n"
                            f"🕒 **Обнаружен в:** {last_boss_info['time']}"
                        )
                    else:
                        msg_text = "ℹ️ Информация о боссах с момента запуска юзербота ещё не поступала."
                    send_msg(peer_id, msg_text)
                    continue

                # ----------------------------------------------------
                # 2. АДМИН-КОМАНДЫ (/код, /все, /add, /очистить)
                # ----------------------------------------------------
                if clean_text.startswith(("/код", "/все", "/add", "/очистить")):
                    if user_id != ADMIN_ID:
                        send_msg(peer_id, "⛔ У вас нет доступа к этой команде.")
                        continue

                    if clean_text == "/код":
                        lines = []
                        with data_lock:
                            for a in ASPECTS:
                                if a in runes:
                                    t_str = time.strftime(
                                        "%H:%M",
                                        time.localtime(runes[a]["time"]),
                                    )
                                    lines.append(
                                        f"{a}: {runes[a]['code']} (в {t_str})"
                                    )
                                else:
                                    lines.append(f"{a}: Нет")
                        msg_text = "💾 Все собранные коды:\n\n" + "\n".join(
                            lines
                        )
                        send_msg(peer_id, msg_text)
                        continue

                    if clean_text.startswith("/все"):
                        broadcast_text = text[4:].strip()
                        if broadcast_text:
                            send_to_work_chats(
                                f"📢 **Общее сообщение:**\n\n{broadcast_text}"
                            )
                            send_msg(
                                peer_id,
                                "✅ Сообщение отправлено во все рабочие чаты.",
                            )
                        else:
                            send_msg(
                                peer_id,
                                "⚠️ Формат команды: /все <текст рассылки>",
                            )
                        continue

                    if clean_text.startswith("/add"):
                        parts = text.split()
                        if len(parts) >= 3:
                            aspect = parts[1].capitalize()
                            code = parts[2]
                            if aspect in ASPECTS:
                                trigger_broadcast = False
                                with data_lock:
                                    was_complete = len(runes) == len(ASPECTS)
                                    runes[aspect] = {
                                        "code": code,
                                        "time": time.time(),
                                    }
                                    save_data(runes)
                                    status_str = get_status_text(runes)

                                    if (
                                        len(runes) == len(ASPECTS)
                                        and not was_complete
                                    ):
                                        trigger_broadcast = True

                                response_text = (
                                    f"✅ Аспект «{aspect}» установлен вручную!\n\n"
                                    f"{status_str}"
                                )
                                send_msg(peer_id, response_text)

                                if trigger_broadcast:
                                    broadcast_notification()
                        continue

                    if clean_text == "/очистить":
                        with data_lock:
                            runes.clear()
                            save_data(runes)
                        send_msg(peer_id, "🗑 База рун полностью очищена.")
                        continue

                # ----------------------------------------------------
                # 3. ОБРАБОТКА ТРИГГЕРА АВРОРЫ (ВЫДАЧА КОМБО ИГРОКУ)
                # ----------------------------------------------------
                current_time = time.time()
                trigger_status = check_aurora_trigger(
                    fwd_messages, current_time, user_id
                )

                if trigger_status == "EXPIRED":
                    send_msg(
                        peer_id,
                        "⚠️ Это сообщение от Авроры слишком старое!\n"
                        "Принимаются только свежие сообщения (не старше 15 минут).",
                    )
                    continue

                if trigger_status == "VALID":
                    with data_lock:
                        is_complete = len(runes) == len(ASPECTS)
                        combo_str = " ".join(
                            [
                                runes[a]["code"]
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
                    continue

                # ----------------------------------------------------
                # 4. ОБРАБОТКА ИВЕНТОВ, СБРОСА И СБОРА РУН
                # ----------------------------------------------------

                # --- А) АВТО-ОПОВЕЩЕНИЯ ОБ ИВЕНТАХ И СБРОСЕ (Из Общего чата RESET_PEER_ID) ---
                if peer_id == RESET_PEER_ID and user_id == AURORA_ID:

                    # Пробуждение колосса (@online)
                    if "пробудился" in clean_text and "колосс" in clean_text:
                        boss_match = re.search(
                            r"Пробудился\s+([^.\n!]+)", text, re.IGNORECASE
                        )
                        coords_match = re.search(
                            r"координаты:\s*([-\d]+:[-\d]+)", text, re.IGNORECASE
                        )

                        if boss_match and coords_match:
                            boss_name = boss_match.group(1).strip()
                            coords = coords_match.group(1).strip()

                            last_boss_info = {
                                "name": boss_name,
                                "coords": coords,
                                "time": time.strftime(
                                    "%H:%M", time.localtime()
                                ),
                            }

                            colossus_msg = (
                                f"@online ☄️ **ОБНАРУЖЕНА УГРОЗА!**\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"👹 **Существо:** {boss_name}\n"
                                f"📍 **Координаты:** `{coords}`"
                            )
                            send_to_work_chats(colossus_msg)

                    # Колоссальная сущность / Босс на стене (@all)
                    elif (
                        "колоссальная сущность" in clean_text
                        or "цель операции:" in clean_text
                    ):
                        event_msg = (
                            f"@all 🛑 **БОСС НА СТЕНЕ ГРУППЫ!**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"👾 В группе пробудился мировой босс!\n"
                            f"⚔️ Переходите к посту на стене и вступайте в бой."
                        )
                        send_to_work_chats(event_msg)

                    # Аномальные возмущения (@all)
                    elif "аномальные возмущения" in clean_text:
                        event_msg = (
                            f"@all 🧪 **НОВОЕ СОБЫТИЕ НА СТЕНЕ!**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"🔬 На стене группы появилось аномальное событие.\n"
                            f"🚀 Пора отправляться на исследование!"
                        )
                        send_to_work_chats(event_msg)

                    # Турнир на Арене (@all) — срабатывает только на анонс старта
                    elif (
                        ("турнир на арене" in clean_text or "распорядителю арены" in clean_text)
                        and "окончен" not in clean_text
                        and "завершен" not in clean_text
                    ):
                        event_msg = (
                            f"@all 🏟 **ТУРНИР НА АРЕНЕ!**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"⚔️ Через 30 минут начинается турнир на арене Хром-Гарна!"
                        )
                        send_to_work_chats(event_msg)


                    # Сброс алтаря
                    elif any(
                        trigger in clean_text for trigger in RESET_TRIGGERS
                    ):
                        reset_time_str = time.strftime(
                            "%H:%M:%S", time.localtime()
                        )

                        with data_lock:
                            old_count = len(runes)
                            runes.clear()
                            save_data(runes)
                            status_str = get_status_text(runes)

                        response = (
                            f"🚨 **Алтарь вскрыт! ({reset_time_str})**\n\n"
                            f"• Сброс зафиксирован от ИИ Аврора в общем чате.\n"
                            f"• База рун полностью очищена (удалено: {old_count}).\n\n"
                            f"{status_str}"
                        )
                        send_to_work_chats(response)
                        continue

                # --- Б) АВТО-СОХРАНЕНИЕ РУН (Принимаем в рабочих чатах RUNES_PEER_IDS) ---
                if peer_id in RUNES_PEER_IDS:
                    all_to_check = [msg] + fwd_messages

                    for fwd in all_to_check:
                        fwd_text = fwd.get("text", "")
                        fwd_date = fwd.get("date", int(current_time))
                        fwd_text_lower = fwd_text.lower()

                        if (
                            current_time - fwd_date
                        ) <= 1800 or user_id == ADMIN_ID:
                            if (
                                "реликвия активна" in fwd_text_lower
                                and "аспект:" in fwd_text_lower
                            ):
                                match = re.search(
                                    r"Аспект:\s*([А-Яа-яЁё]+)",
                                    fwd_text,
                                    re.IGNORECASE,
                                )
                                if match:
                                    aspect = match.group(1).capitalize()
                                    code = None

                                    code_match = re.search(
                                        r"\((.*?)\)", fwd_text
                                    )
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
                                            was_complete = len(runes) == len(
                                                ASPECTS
                                            )
                                            runes[aspect] = {
                                                "code": code,
                                                "time": fwd_date,
                                            }
                                            save_data(runes)
                                            status_str = get_status_text(runes)

                                            if (
                                                len(runes) == len(ASPECTS)
                                                and not was_complete
                                            ):
                                                trigger_broadcast = True

                                        # Подтверждение сохранения в чат
                                        response_msg = (
                                            f"✅ Аспект «{aspect}» сохранен!\n\n"
                                            f"{status_str}"
                                        )
                                        send_msg(peer_id, response_msg)

                                        # Подчищаем сообщение игрока (если есть права админа в чате)
                                        delete_msg(event.message_id)

                                        # При сборе 5/5 созываем беседы
                                        if trigger_broadcast:
                                            broadcast_notification()
                                        break

    except Exception as e:
        print(f"⚠️ Ошибка VK: {e}")
        time.sleep(5)
