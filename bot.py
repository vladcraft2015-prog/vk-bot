import json
import os
import re
import threading
import time
import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll
from vk_api.utils import get_random_id

# ==========================================
# ⚙️ НАСТРОЙКИ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================================
TOKEN = os.environ.get("VK_TOKEN", "ВАШ_ТОКЕН_ПОЛЬЗОВАТЕЛЯ")
ADMIN_ID = 550216110

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "runes_data.json")

# Инициализация сессии ВК
vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()

data_lock = threading.Lock()

# 5 аспектов рун Атраксиса
ASPECTS = ["Мутация", "Стабильность", "Память", "Забвение", "Очищение"]

# ID бесед из вашего конфига
RESET_PEER_ID = 2000000012  # Чат-источник (отслеживание ивентов Авроры и сбросов)
RUNES_PEER_IDS = [2000000011, 2000000013]  # Ваши 2 рабочих чата для анонсов

# Триггеры сброса алтаря
RESET_TRIGGERS = [
    "рунический свет погас",
    "разгадал символы",
    "символы алтаря",
    "алтарь сброшен",
    "ритуал соверш",
    "ритуал заверш",
]


# ==========================================
# 🛠 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_status_text(runes):
    lines = ["📜 **Текущий прогресс рун:**\n"]
    count = 0
    for asp in ASPECTS:
        if asp in runes:
            lines.append(f"✅ {asp}: `{runes[asp]['code']}`")
            count += 1
        else:
            lines.append(f"❌ {asp}: *не найдено*")
    lines.append(f"\n📊 Собрано: {count}/{len(ASPECTS)}")
    return "\n".join(lines)


def send_msg(peer_id, text):
    try:
        vk.messages.send(peer_id=peer_id, message=text, random_id=get_random_id())
    except Exception:
        pass


def send_to_work_chats(text):
    for pid in RUNES_PEER_IDS:
        send_msg(pid, text)


def safe_delete_msg(message_id):
    try:
        vk.messages.delete(message_ids=[message_id], delete_for_all=1)
    except Exception:
        pass


# ==========================================
# 🚀 ОСНОВНОЙ ЦИКЛ БОТА
# ==========================================
def main():
    while True:
        # Автореконнект при сетевых сбоях Bothost
        try:
            longpoll = VkLongPoll(vk_session)

            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW:
                    continue

                if event.from_me:
                    continue

                text = event.text or ""
                clean_text = text.lower()
                peer_id = event.peer_id

                # ------------------------------------------
                # 1. КОМАНДЫ И РИТУАЛЫ
                # ------------------------------------------

                # Проверка работоспособности
                if clean_text in ["/ping", "/пинг"]:
                    send_msg(peer_id, "🏓 Понг! Бот работает 24/7.")
                    continue

                # Статус сбора рун
                if clean_text in ["/статус", "/прогресс", "/руны"]:
                    with data_lock:
                        runes = load_data()
                        status_msg = get_status_text(runes)
                    send_msg(peer_id, status_msg)
                    continue

                # Очистка базы рун
                if clean_text == "/очистить":
                    with data_lock:
                        save_data({})
                    send_msg(peer_id, "🧹 База данных рун очищена! Статус 0/5.")
                    continue

                # Массовая рассылка во все рабочие чаты
                if clean_text.startswith("/все ") or clean_text.startswith("/все\n"):
                    broadcast_text = text[4:].strip()
                    if broadcast_text:
                        send_to_work_chats(f"📢 **ОБЩЕЕ ОБЪЯВЛЕНИЕ:**\n\n{broadcast_text}")
                        send_msg(peer_id, "✅ Объявление отправлено в рабочие чаты.")
                    else:
                        send_msg(peer_id, "⚠️ Формат команды: `/все <текст рассылки>`")
                    continue

                # АВТО-ВЫДАЧА 5 РУН ПРИ РИТУАЛЕ
                if "введите все 5 руноблоков" in clean_text:
                    with data_lock:
                        runes = load_data()
                        if len(runes) == len(ASPECTS):
                            codes = [runes[asp]["code"] for asp in ASPECTS if asp in runes]
                            combo_msg = " ".join(codes)
                            send_msg(peer_id, combo_msg)
                        else:
                            send_msg(
                                peer_id,
                                f"⚠️ Невозможно провести ритуал: собрано {len(runes)}/5 рун!"
                            )
                    continue

                # ДОБАВЛЕНИЕ РУНЫ В БАЗУ
                if clean_text.startswith("/add"):
                    parts = text.split()
                    if len(parts) >= 3:
                        aspect = parts[1].capitalize()
                        code = parts[2]
                        if aspect in ASPECTS:
                            with data_lock:
                                runes = load_data()
                                was_complete = len(runes) == len(ASPECTS)
                                runes[aspect] = {
                                    "code": code,
                                    "time": time.time(),
                                }
                                save_data(runes)
                                status_str = get_status_text(runes)

                            send_msg(
                                peer_id,
                                f"✅ Руна [{aspect}] добавлена!\n\n{status_str}",
                            )

                            # Анонс в рабочие чаты при сборе 5/5
                            if len(runes) == len(ASPECTS) and not was_complete:
                                alert_msg = (
                                    f"@all 🏛 **Алтарь готов к ритуалу!** (Собрано 5/5)\n\n"
                                    f"Найди алтарь в игре и перешли сюда системное сообщение:\n"
                                    f"«✍ Введите все 5 руноблоков через пробел. Порядок не важен. Есть лишь один шанс...»\n\n"
                                    f"Бот мгновенно выдаст тебе все руноблоки 📜"
                                )
                                send_to_work_chats(alert_msg)
                        else:
                            send_msg(
                                peer_id,
                                f"❌ Неверный аспект! Допустимые: {', '.join(ASPECTS)}",
                            )
                    else:
                        send_msg(
                            peer_id, "⚠️ Формат команды: `/add <Аспект> <Код>`"
                        )
                    continue

                # ------------------------------------------
                # 2. ОПОВЕЩЕНИЯ ИИ АВРОРА И СБРОС АЛТАРЯ
                # (Считывание СТРОГО из RESET_PEER_ID: 2000000012)
                # ------------------------------------------
                if peer_id == RESET_PEER_ID:

                    # А) Пробуждение Колосса (@online)
                    if (
                        "пробудился" in clean_text
                        and "колосс" in clean_text
                        and "повержен" not in clean_text
                        and "уничтожен" not in clean_text
                        and "заверш" not in clean_text
                    ):
                        event_msg = (
                            f"@online ☄️ **ОБНАРУЖЕНА УГРОЗА!**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"👹 В чате пробудился Колосс!\n"
                            f"📍 Сообщение: {text}"
                        )
                        send_to_work_chats(event_msg)

                    # Б) Босс на стене группы / Вторжение (@all)
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
                            f"@all 🛑 **БОСС НА СТЕНЕ ГРУППЫ!**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"👾 В группе пробудился мировой босс!\n"
                            f"⚔️ Переходите к посту на стене и вступайте в бой."
                        )
                        send_to_work_chats(event_msg)

                    # В) Аномальные возмущения (@all)
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

                    # Г) Турнир на Арене (@all)
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

                    # Д) Сброс алтаря (без тегов)
                    elif any(trig in clean_text for trig in RESET_TRIGGERS):
                        with data_lock:
                            save_data({})
                        reset_msg = (
                            f"🔄 **Алтарь был сброшен!**\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"База рун очищена. Статус: 0/5"
                        )
                        send_to_work_chats(reset_msg)

        except Exception:
            time.sleep(5)


if __name__ == "__main__":
    main()
