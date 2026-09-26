import json
import os
import re
import threading
import time
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.utils import get_random_id

# --- НАСТРОЙКИ ---
TOKEN = "vk1.a.LRvr8yJ8JNiySD0kkLOQGkHYbMBKag1sOQ3LNLLnXv4701gdDZMVA-kfucgclW8eJzzgaKOJNNRqQiWUx2Pxw0vMslyhwxG8R4ew8fTZ-vfrvXFPrCwQx0rJpOyU1NMskR8EYwpemfUlz24qxTClbThxpVlm1VbUMEZEGPJifwLhgamUSC25-_eb4AIX2EKYZ1AmEic6hjknpZvoGn3DAQ"
GROUP_ID = 240378679
ADMIN_ID = 550216110
DATA_FILE = "bot_data.json"

ASPECTS = ["Мутация", "Стабильность", "Память", "Забвение", "Очищение"]

RESET_TRIGGERS = [
    "символ",
    "глиф",
    "ритуал соверш",
    "ритуал заверш",
    "алтаря",
    "алтарь сброшен",
]

ATRAXIS_TRIGGER = "✍ Введите все 5 руноблоков через пробел. Порядок не важен. Есть лишь один шанс..."


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
                users = set(data.get("users", []))
                timers = data.get("timers", {})
                return runes, users, timers
        except Exception as e:
            print(f"Ошибка загрузки базы: {e}")
            return {}, set(), {}
    return {}, set(), {}


def save_data(runes, users, timers):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"runes": runes, "users": list(users), "timers": timers},
                f,
                ensure_ascii=False,
                indent=4,
            )
    except Exception as e:
        print(f"Ошибка сохранения базы: {e}")


def get_main_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button("📊 Статус рун", color=VkKeyboardColor.PRIMARY)
    return keyboard.get_keyboard()


def send_msg(user_id, text, keyboard=None):
    try:
        params = {
            "user_id": user_id,
            "message": text,
            "random_id": get_random_id(),
        }
        if keyboard:
            params["keyboard"] = keyboard
        vk.messages.send(**params)
    except Exception as e:
        print(f"Ошибка отправки пользователю {user_id}: {e}")


def get_status_text(runes):
    status_lines = [f"{a} {'+' if a in runes else '-'}" for a in ASPECTS]
    return f"📊 Статус рун ({len(runes)}/5):\n\n" + "\n".join(status_lines)


def broadcast_notification(users):
    message = (
        "🏛 **Алтарь готов к ритуалу!** (Собрано 5/5)\n\n"
        "Найди алтарь в игре и перешли сюда системное сообщение:\n"
        "«✍ Введите все 5 руноблоков через пробел. Порядок не важен. Есть лишь один шанс...»\n\n"
        "Бот мгновенно выдаст тебе все руноблоки 📜"
    )
    kb = get_main_keyboard()
    for u_id in list(users):
        send_msg(u_id, message, keyboard=kb)
        time.sleep(0.05)


def check_atraxis_trigger(fwd_messages, current_time, user_id):
    for fwd in fwd_messages:
        if ATRAXIS_TRIGGER in fwd.get("text", ""):
            fwd_date = fwd.get("date", 0)
            if (current_time - fwd_date) <= 900 or user_id == ADMIN_ID:
                return "VALID"
            else:
                return "EXPIRED"
    return None


# --- ФОНОВЫЙ ПОТОК ДЛЯ ТАЙМЕРОВ ---
def timer_worker():
    while True:
        try:
            now = time.time()
            data_changed = False

            for u_id_str, user_timers in list(timers.items()):
                u_id = int(u_id_str)

                # ⚔️ Напоминание Босса
                if "boss" in user_timers and now >= user_timers["boss"]:
                    msg_boss = (
                        "⚔️ ГОТОВ К БИТВЕ?\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "⏳ Прошёл ровно 1 час!\n"
                        "💥 Пора нанести новый урон по боссу!"
                    )
                    send_msg(u_id, msg_boss, keyboard=get_main_keyboard())
                    del user_timers["boss"]
                    data_changed = True

                # 🔬 Напоминание Исследований
                if (
                    "research" in user_timers
                    and now >= user_timers["research"]
                ):
                    msg_res = (
                        "🔬 ИССЛЕДОВАНИЕ ГОТОВО!\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "⏳ Прошёл ровно 1 час!\n"
                        "🧪 Пора отправляться на новые исследования!"
                    )
                    send_msg(u_id, msg_res, keyboard=get_main_keyboard())
                    del user_timers["research"]
                    data_changed = True

            if data_changed:
                save_data(runes, users, timers)

        except Exception as e:
            print(f"Ошибка в фоновом таймере: {e}")

        time.sleep(5)


# --- ИНИЦИАЛИЗАЦИЯ ---
vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()
runes, users, timers = load_data()

threading.Thread(target=timer_worker, daemon=True).start()
print(f"🤖 Бот запущен. Админ ID: {ADMIN_ID}")

# --- ОСНОВНОЙ ЦИКЛ ---
while True:
    try:
        longpoll = VkBotLongPoll(vk_session, GROUP_ID)
        for event in longpoll.listen():
            if event.type == VkBotEventType.MESSAGE_NEW:
                msg = event.obj.message
                text = msg.get("text", "").strip()
                user_id = msg.get("from_id")

                if user_id and user_id not in users:
                    users.add(user_id)
                    save_data(runes, users, timers)

                fwd_messages = msg.get("fwd_messages", [])
                if "reply_message" in msg:
                    fwd_messages.append(msg["reply_message"])

                clean_text = text.lower()

                # ----------------------------------------------------
                # 1. ТАЙМЕРЫ (БОСС / ИССЛЕДОВАНИЯ) С ТОЧНЫМ ВРЕМЕНЕМ СООБЩЕНИЯ
                # ----------------------------------------------------
                has_boss = False
                has_research = False
                msg_date = time.time()

                for fwd in fwd_messages:
                    fwd_text = fwd.get("text", "").lower()
                    fwd_date = fwd.get("date", 0)

                    if (
                        "урон по боссу" in fwd_text
                        or "следующая атака" in fwd_text
                    ):
                        has_boss = True
                        if fwd_date > 0:
                            msg_date = fwd_date

                    if (
                        "очков исследования" in fwd_text
                        or "следующее исследование" in fwd_text
                    ):
                        has_research = True
                        if fwd_date > 0:
                            msg_date = fwd_date

                if has_boss or has_research:
                    u_str = str(user_id)
                    if u_str not in timers:
                        timers[u_str] = {}

                    # Время сработки = дата системного сообщения + 1 час
                    target_time = msg_date + 3600

                    # Если сообщение переслали с опозданием более 1 часа
                    if target_time <= time.time():
                        send_msg(
                            user_id,
                            "⚠️ С момента этого игрового события уже прошёл 1 час! Кулдаун завершён.",
                            keyboard=get_main_keyboard(),
                        )
                        continue

                    remaining_min = int((target_time - time.time()) // 60)

                    confirm_lines = [
                        "⏳ ТАЙМЕР ЗАПУЩЕН",
                        "━━━━━━━━━━━━━━━━━━",
                    ]

                    if has_boss:
                        timers[u_str]["boss"] = target_time
                        confirm_lines.append(
                            f"⚔️ Босс: Напоминание через {remaining_min} мин."
                        )

                    if has_research:
                        timers[u_str]["research"] = target_time
                        confirm_lines.append(
                            f"🔬 Исследования: Напоминание через {remaining_min} мин."
                        )

                    save_data(runes, users, timers)
                    send_msg(
                        user_id,
                        "\n".join(confirm_lines),
                        keyboard=get_main_keyboard(),
                    )
                    continue

                # ----------------------------------------------------
                # 2. ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ
                # ----------------------------------------------------
                if clean_text in [
                    "/статус",
                    "/прогресс",
                    "/руны",
                    "📊 статус рун",
                ]:
                    send_msg(
                        user_id,
                        get_status_text(runes),
                        keyboard=get_main_keyboard(),
                    )
                    continue

                # ----------------------------------------------------
                # 3. АДМИН-КОМАНДЫ (/код, /все, /add, /очистить)
                # ----------------------------------------------------
                if clean_text.startswith(("/код", "/все", "/add", "/очистить")):
                    if user_id != ADMIN_ID:
                        send_msg(
                            user_id,
                            "⛔ У вас нет доступа к этой команде.",
                            keyboard=get_main_keyboard(),
                        )
                        continue

                    if clean_text == "/код":
                        lines = []
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
                        send_msg(
                            user_id, msg_text, keyboard=get_main_keyboard()
                        )
                        continue

                    if clean_text.startswith("/все"):
                        broadcast_text = text[4:].strip()
                        if broadcast_text:
                            sent_count = 0
                            for u in list(users):
                                try:
                                    send_msg(
                                        u,
                                        f"📢 **Общее сообщение:**\n\n{broadcast_text}",
                                        keyboard=get_main_keyboard(),
                                    )
                                    sent_count += 1
                                except Exception:
                                    pass
                            send_msg(
                                user_id,
                                f"✅ Рассылка отправлена {sent_count} пользователям.",
                                keyboard=get_main_keyboard(),
                            )
                        else:
                            send_msg(
                                user_id,
                                "⚠️ Формат команды: /все <текст рассылки>",
                                keyboard=get_main_keyboard(),
                            )
                        continue

                    if clean_text.startswith("/add"):
                        parts = text.split()
                        if len(parts) >= 3:
                            aspect = parts[1].capitalize()
                            code = parts[2]
                            if aspect in ASPECTS:
                                was_complete = len(runes) == len(ASPECTS)
                                runes[aspect] = {
                                    "code": code,
                                    "time": time.time(),
                                }
                                save_data(runes, users, timers)

                                response_text = (
                                    f"✅ {aspect} установлена вручную!\n\n"
                                    f"{get_status_text(runes)}"
                                )
                                send_msg(
                                    user_id,
                                    response_text,
                                    keyboard=get_main_keyboard(),
                                )

                                if (
                                    len(runes) == len(ASPECTS)
                                    and not was_complete
                                ):
                                    broadcast_notification(users)
                        continue

                    if clean_text == "/очистить":
                        runes.clear()
                        save_data(runes, users, timers)
                        send_msg(
                            user_id,
                            "🗑 База рун полностью очищена.",
                            keyboard=get_main_keyboard(),
                        )
                        continue

                # ----------------------------------------------------
                # 4. ОБРАБОТКА ТРИГГЕРА АТРАКСИСА
                # ----------------------------------------------------
                current_time = time.time()
                trigger_status = check_atraxis_trigger(
                    fwd_messages, current_time, user_id
                )

                if trigger_status == "EXPIRED":
                    send_msg(
                        user_id,
                        "⚠️ Это сообщение от Атраксиса слишком старое!\n"
                        "Принимаются только свежие сообщения (не старше 15 минут).",
                        keyboard=get_main_keyboard(),
                    )
                    continue

                if trigger_status == "VALID":
                    if len(runes) == len(ASPECTS):
                        combo_str = " ".join(
                            [runes[a]["code"] for a in ASPECTS]
                        )
                        send_msg(
                            user_id,
                            f"✅ Запрос принят! Вот все 5 руноблоков:\n\n{combo_str}",
                            keyboard=get_main_keyboard(),
                        )
                    else:
                        send_msg(
                            user_id,
                            f"⚠️ Собраны еще не все руны ({len(runes)}/5). Ожидайте рассылки!",
                            keyboard=get_main_keyboard(),
                        )
                    continue

                # ----------------------------------------------------
                # 5. ОБРАБОТКА СБРОСА И СБОРА РУН ИЗ ПЕРЕСЛАННЫХ
                # ----------------------------------------------------
                for i, fwd in enumerate(fwd_messages):
                    fwd_text = fwd.get("text", "")
                    fwd_date = fwd.get("date", 0)
                    fwd_text_lower = fwd_text.lower()

                    # ПРОВЕРКА НА СБРОС АЛТАРЯ
                    if any(
                        trigger in fwd_text_lower
                        for trigger in RESET_TRIGGERS
                    ):
                        reset_time = fwd_date
                        reset_time_str = time.strftime(
                            "%H:%M:%S", time.localtime(reset_time)
                        )

                        old_count = len(runes)
                        runes = {
                            asp: data
                            for asp, data in runes.items()
                            if data.get("time", 0) >= reset_time
                        }
                        removed_count = old_count - len(runes)

                        save_data(runes, users, timers)

                        response = (
                            f"🔄 **Сброс алтаря зафиксирован ({reset_time_str})**\n\n"
                            f"• Удалено старых рун: {removed_count}\n"
                            f"• Сохранено новых рун: {len(runes)}\n\n"
                            f"{get_status_text(runes)}"
                        )
                        send_msg(
                            user_id, response, keyboard=get_main_keyboard()
                        )
                        continue

                    # АВТО-СБОР РУН
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

                                    if not code and i + 1 < len(fwd_messages):
                                        code = (
                                            fwd_messages[i + 1]
                                            .get("text", "")
                                            .strip()
                                        )

                                if aspect in ASPECTS and code:
                                    was_complete = len(runes) == len(ASPECTS)
                                    runes[aspect] = {
                                        "code": code,
                                        "time": fwd_date,
                                    }
                                    save_data(runes, users, timers)

                                    fwd_time_str = time.strftime(
                                        "%H:%M", time.localtime(fwd_date)
                                    )

                                    response_msg = (
                                        f"✅ {aspect} ({code}) принята! (от {fwd_time_str})\n\n"
                                        f"{get_status_text(runes)}"
                                    )
                                    send_msg(
                                        user_id,
                                        response_msg,
                                        keyboard=get_main_keyboard(),
                                    )

                                    if (
                                        len(runes) == len(ASPECTS)
                                        and not was_complete
                                    ):
                                        broadcast_notification(users)

    except Exception as e:
        print(f"⚠️ Ошибка VK: {e}")
        time.sleep(5)
