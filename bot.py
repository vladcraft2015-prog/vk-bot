import sys
sys.stdout.reconfigure(line_buffering=True)
import time
import random
import os
import re
import requests
import json
from datetime import datetime, timedelta

# ============================== НАСТРОЙКИ ==============================
VK_TOKEN = "vk1.a.ho5aCqOq4D7SaDwU6bJXeLccijn5JKvPTCbNGDUh0iOlgY3sFToACnycXIys6EVCYUt43Qe6sxREVHWxwDUwKBhpBg8xq8Z6_t9kjamyxViREIlRF5ZYDQ5TMvju--Mlnn8AG9yBTG8fQ6bpxoafEEFjbGelmjrLH805vnqyT-uxRLi4K8DcS04iiKgzPe_DIJZahi-VVdlxPlpFHMbgyg"   # лучше хранить в переменной окружения
VK_API_VERSION = "5.131"
MY_USER_ID = 550216110          # ваш ID для чтения команд из Избранного
HISTORY_FILE = "purchase_history.json"
MATRICES_FILE = "target_matrices.json"

CONFIRM_WAIT = 25               # сколько секунд ждать ответ лавки после последнего действия
CONFIRM_MAX_PRESSES = 3         # сколько раз подряд можно нажать «Подтвердить» (цена могла обновиться)
MAX_BUY_ATTEMPTS = 5            # защита от бесконечного цикла корректировок заказа

STORES = [
    {
        "id": 1, "name": "Dark Requiem", "peer_id": -220647125,
        "catalog_type": "text", "catalog_cmd": "Скрин", "confirm_mode": "callback", "disabled": False
    },
    {
        "id": 2, "name": "Алый Рассвет", "peer_id": -236362151,
        "catalog_type": "text", "catalog_cmd": "⭐ Ждун", "disabled": False
    },
    {
        "id": 3, "name": "Сердце Времени", "peer_id": -233363345,
        "catalog_type": "payload", "my_label": "❤️ Моё",
        "my_payload": {"guild_store": 1, "screen": "main", "button": "favorites"},
        "disabled": False
    },
    {
        "id": 4, "name": "Arcana Market", "peer_id": -239812778,
        "catalog_type": "payload", "my_label": "🎒 Моё",
        "my_payload": {"action": "shop.mine"}, "confirm_mode": "message", "disabled": False
    },
]

SUCCESS_KEYWORDS = ["куплено за", "результат сделки", "списано:", "покупка завершена", "куплено:"]
# слова, по которым понятно, что лавка дала окончательный ответ
FINAL_MARKERS = SUCCESS_KEYWORDS + ["нет на складе", "доступно:", "недостаточно", "нужно"]

last_check_time = "Ещё не проводилась"
last_processed_msg_id = 0


# ============================== ХРАНИЛИЩЕ ==============================
def load_json(file_path, default):
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Ошибка чтения {file_path}: {e}")
    return default


def save_json(file_path, data):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения {file_path}: {e}")


purchase_history = load_json(HISTORY_FILE, [])
target_matrices = load_json(MATRICES_FILE, [
    {"name": "Грави-всплеск", "stores": [1, 2, 3, 4]}
])


# ============================== VK API ==============================
def vk_api_request(method, params=None, retries=3):
    if not VK_TOKEN:
        print("⚠️ Ошибка: VK_TOKEN пустой!")
        return None
    params = dict(params or {})
    params['access_token'] = VK_TOKEN
    params['v'] = VK_API_VERSION
    url = f"https://api.vk.ru/method/{method}"

    for attempt in range(1, retries + 1):
        try:
            return requests.post(url, data=params, timeout=15).json()
        except Exception as e:
            print(f"⚠️ Сетевая ошибка VK API ({method}), попытка {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def send_message(peer_id, message=" ", payload=None):
    message = message or " "
    print(f"📤 Отправка в peer {peer_id}: «{message}» (payload: {payload is not None})")

    params = {
        'peer_id': peer_id,
        'message': message,
        'random_id': random.randint(1, 2147483647)
    }
    if payload:
        params['payload'] = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    data = vk_api_request("messages.send", params)
    if data and 'response' in data:
        print("✅ Сообщение успешно доставлено!")
        return True
    print(f"❌ Ошибка отправки: {data}")
    return False


def get_history(peer_id, count=5):
    data = vk_api_request("messages.getHistory", {'peer_id': peer_id, 'count': count})
    if data and 'response' in data:
        return data['response'].get('items', [])
    return []


def get_last_msg_id(peer_id):
    items = get_history(peer_id, 1)
    return items[0].get('id', 0) if items else 0


# ============================== КНОПКА ПОДТВЕРЖДЕНИЯ ==============================
def find_confirm_button(msg):
    """Ищет в сообщении кнопку «Подтвердить». Возвращает action кнопки или None."""
    kb = msg.get('keyboard') or msg.get('inline_keyboard')
    if not kb:
        return None
    for row in kb.get('buttons', []):
        for btn in (row if isinstance(row, list) else [row]):
            action = btn.get('action', {})
            label = (action.get('label') or '').lower()
            payload = str(action.get('payload', '')).lower()
            if 'отмен' in label or 'cancel' in payload:
                continue
            if 'подтверд' in label or 'confirm' in payload:
                return action
    return None


CALLBACK_METHODS = ["messages.sendMessageEvent", "messages.getCallbackAnswer"]


def press_message_button(peer_id, action):
    """Лавка №4 (Arcana Market): как и раньше — отправляем сообщение с payload кнопки."""
    label = action.get('label') or "Подтвердить"
    print(f"🎯 [message] Нажимаем кнопку «{label}»")
    return send_message(peer_id, label, action.get('payload'))


def press_callback_button(peer_id, msg, action):
    """
    Лавка №1 (Dark Requiem): callback-кнопка. В чат ничего не отправляется —
    нажатие идёт отдельным запросом к API, как в приложении ВК.
    """
    payload = action.get('payload')
    print(f"🎯 [callback] Нажимаем «{action.get('label')}» в сообщении id={msg.get('id')}, "
          f"cmid={msg.get('conversation_message_id')}, payload={payload}")
    for method in CALLBACK_METHODS:
        resp = vk_api_request(method, {
            'peer_id': peer_id,
            'message_id': msg.get('id'),
            'conversation_message_id': msg.get('conversation_message_id'),
            'payload': payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        })
        print(f"   ↳ {method}: {resp}")
        if resp and 'error' not in resp:
            return True
    print("❌ Не удалось нажать callback-кнопку ни одним методом.")
    return False


def press_button(store, msg, action):
    mode = store.get('confirm_mode')
    if mode == 'callback':
        return press_callback_button(store['peer_id'], msg, action)
    if mode == 'message':
        return press_message_button(store['peer_id'], action)
    return False


def wait_for_result(store, after_id):
    """
    Ждёт ответ лавки на команду покупки. Если приходит сообщение с кнопкой
    «Подтвердить» — нажимает её (только на НОВЫХ сообщениях, старые игнорируются).
    Возвращает текст финального ответа лавки (или пустую строку по таймауту).
    """
    peer_id = store['peer_id']
    pressed_ids = set()
    presses = 0
    deadline = time.time() + CONFIRM_WAIT
    result_text = ""

    while time.time() < deadline:
        time.sleep(2)
        msgs = [m for m in get_history(peer_id, 6)
                if m.get('id', 0) > after_id and not m.get('out')]
        msgs.sort(key=lambda m: m['id'])

        # 1) есть новая кнопка подтверждения — жмём (самую свежую)
        pressed_now = False
        for m in reversed(msgs):
            if m['id'] in pressed_ids:
                continue
            action = find_confirm_button(m) if store.get('confirm_mode') else None
            if action:
                if presses >= CONFIRM_MAX_PRESSES:
                    print("⚠️ Достигнут лимит нажатий подтверждения.")
                    return result_text
                if not press_button(store, m, action):
                    return result_text
                pressed_ids.add(m['id'])
                presses += 1
                deadline = time.time() + CONFIRM_WAIT   # даём время на ответ после нажатия
                pressed_now = True
                break
        if pressed_now:
            continue

        # 2) кнопок нет — смотрим, пришёл ли итоговый ответ
        final_msgs = [m.get('text', '') for m in msgs
                      if not (store.get('confirm_mode') and find_confirm_button(m))
                      and m['id'] not in pressed_ids]
        text = "\n".join(final_msgs)
        if text:
            result_text = text
            if any(k in text.lower() for k in FINAL_MARKERS):
                return text

    return result_text


# ============================== СТАТИСТИКА ==============================
def get_weekly_stats_text():
    if not purchase_history:
        return "• Покупок пока зафиксировано не было."

    now_dt = datetime.now()
    start_of_week = (now_dt - timedelta(days=now_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()

    week, total = {}, {}
    for item in purchase_history:
        key = f"{item['store']} ➔ {item.get('matrix', 'Грави-всплеск')}"
        total[key] = total.get(key, 0) + item['amount']
        if item['time'] >= start_of_week:
            week[key] = week.get(key, 0) + item['amount']

    lines = ["📅 **Статистика за текущую неделю:**"]
    if week:
        for key, amount in week.items():
            lines.append(f"  ▪️ «{key}» — **{amount}** шт.")
        lines.append(f"📦 **Итого за неделю:** {sum(week.values())} шт.")
    else:
        lines.append("  ▪️ На этой неделе покупок ещё не было.")

    lines.append("\n🏆 **Статистика за всё время:**")
    for key, amount in total.items():
        lines.append(f"  ▪️ «{key}» — **{amount}** шт.")
    lines.append(f"📦 **Итого за всё время:** {sum(total.values())} шт.")
    return "\n".join(lines)


def record_purchase(amount, store_name, matrix_name):
    purchase_history.append({
        'time': time.time(), 'amount': amount,
        'store': store_name, 'matrix': matrix_name
    })
    save_json(HISTORY_FILE, purchase_history)


# ============================== КОМАНДЫ ИЗ ИЗБРАННОГО ==============================
HELP_TEXT = (
    "🤖 **Панель управления ботом**\n\n"
    "🎯 **Управление матрицами:**\n"
    "• `/добавить Название` — искать во всех лавках\n"
    "• `/добавить Панцирь, Ускользание 2 3` — искать в лавках №2 и №3\n"
    "• `/удалить Панцирь` — убрать матрицу\n"
    "• `/матрицы` — список отслеживаемых позиций\n"
    "• `/очистить матрицы` — удалить все цели\n\n"
    "🏪 **Управление лавками:**\n"
    "• `/лавки` — статус и список лавок\n"
    "• `/off` — поставить авто-покупку на паузу\n"
    "• `/on` — возобновить работу\n"
    "• `/off 3` / `/on 3` — выключить/включить конкретную лавку\n\n"
    "📊 **Информация:**\n"
    "• `/статус` — общая сводка и отчёты\n"
    "• `/сброс` — очистить историю покупок"
)


def check_commands(current_state):
    global last_processed_msg_id, purchase_history, target_matrices

    data = vk_api_request("messages.getHistory", {'user_id': MY_USER_ID, 'count': 1})
    if not (data and 'response' in data):
        return current_state
    items = data['response'].get('items', [])
    if not items:
        return current_state

    msg = items[0]
    msg_id = msg.get('id', 0)
    if msg_id <= last_processed_msg_id:
        return current_state
    last_processed_msg_id = msg_id

    raw_text = msg.get('text', '').strip()
    text = raw_text.lower()

    if text in ['/хелп', '/help', '/команды']:
        send_message(MY_USER_ID, HELP_TEXT)
        return current_state

    # --- /добавить ---
    if text.startswith('/добавить'):
        payload = raw_text[9:].strip()
        if not payload:
            send_message(MY_USER_ID, "⚠️ **Ошибка:** Укажите название.\n\n*Пример:*\n`/добавить Ускользание`\n`/добавить Панцирь, Ускользание 2 3`")
            return current_state

        target_store_ids = [1, 2, 3, 4]
        parts_space = payload.split()
        trailing_ids = []
        while len(parts_space) > 1 and parts_space[-1] in ['1', '2', '3', '4']:
            trailing_ids.insert(0, int(parts_space.pop()))
        if trailing_ids:
            target_store_ids = trailing_ids
            payload = " ".join(parts_space)

        matrix_names = [p.strip() for p in payload.split(',') if p.strip()]
        if not matrix_names:
            send_message(MY_USER_ID, "⚠️ Не удалось распознать названия матриц.")
            return current_state

        added = []
        for m_name in matrix_names:
            existing = next((i for i in target_matrices if i['name'].lower() == m_name.lower()), None)
            if existing:
                existing['stores'] = target_store_ids
            else:
                target_matrices.append({'name': m_name, 'stores': target_store_ids})
            added.append(f"• «{m_name}» ➔ Лавки: [{', '.join(map(str, target_store_ids))}]")

        save_json(MATRICES_FILE, target_matrices)
        send_message(MY_USER_ID, "✅ **Успешно добавлено в отслеживание:**\n" + "\n".join(added))
        return current_state

    # --- /удалить ---
    if text.startswith('/удалить') or text.startswith('/снять'):
        payload = raw_text.split(maxsplit=1)[1].strip() if len(raw_text.split()) > 1 else ""
        if not payload:
            send_message(MY_USER_ID, "⚠️ **Ошибка:** Укажите название.\n\n*Пример:* `/удалить Панцирь`")
            return current_state

        parts = [p.strip() for p in payload.split(',') if p.strip()]
        target_store_id = None
        if len(parts) > 1 and parts[-1] in ['1', '2', '3', '4']:
            target_store_id = int(parts[-1])
            m_names = [p.lower() for p in parts[:-1]]
        else:
            m_names = [p.lower() for p in parts]

        removed_info, new_matrices = [], []
        for item in target_matrices:
            if item['name'].lower() not in m_names:
                new_matrices.append(item)
                continue
            if target_store_id:
                if target_store_id in item['stores']:
                    item['stores'].remove(target_store_id)
                    removed_info.append(f"• «{item['name']}» (отключена для Лавки №{target_store_id})")
                if item['stores']:
                    new_matrices.append(item)
                else:
                    removed_info.append(f"• «{item['name']}» (удалена полностью)")
            else:
                removed_info.append(f"• «{item['name']}» (удалена полностью)")

        target_matrices = new_matrices
        save_json(MATRICES_FILE, target_matrices)
        if removed_info:
            send_message(MY_USER_ID, "🗑️ **Изменения в отслеживании:**\n" + "\n".join(removed_info))
        else:
            send_message(MY_USER_ID, "⚠️ Указанные матрицы не найдены в вашем списке.")
        return current_state

    if text in ['/матрицы', '/список']:
        if not target_matrices:
            send_message(MY_USER_ID, "🛒 **Список матриц пуст.** Используйте `/добавить Название`.")
        else:
            lines = ["🛒 **Отслеживаемые матрицы:**\n"]
            for i, m in enumerate(target_matrices, 1):
                stores_str = ", ".join(f"№{s}" for s in m['stores'])
                lines.append(f"  {i}. «**{m['name']}**» \n     └ Лавки: [{stores_str}]")
            send_message(MY_USER_ID, "\n".join(lines))
        return current_state

    if text == '/очистить матрицы':
        target_matrices = []
        save_json(MATRICES_FILE, target_matrices)
        send_message(MY_USER_ID, "🗑️ Список матриц полностью очищен.")
        return current_state

    m_off = re.match(r"^/off\s+(\d+)$", text)
    m_on = re.match(r"^/on\s+(\d+)$", text)
    if m_off:
        idx = int(m_off.group(1)) - 1
        if 0 <= idx < len(STORES):
            STORES[idx]['disabled'] = True
            send_message(MY_USER_ID, f"🔴 Лавка №{idx+1} («{STORES[idx]['name']}») временно отключена.")
        return current_state
    if m_on:
        idx = int(m_on.group(1)) - 1
        if 0 <= idx < len(STORES):
            STORES[idx]['disabled'] = False
            send_message(MY_USER_ID, f"🟢 Лавка №{idx+1} («{STORES[idx]['name']}») снова активна!")
        return current_state

    if text == '/off':
        send_message(MY_USER_ID, "🔴 Авто-покупка поставлена на паузу.")
        return False

    if text in ['/on', '/включить']:
        for store in STORES:
            store['disabled'] = False
        send_message(MY_USER_ID, "🟢 Авто-покупка успешно возобновлена!")
        return True

    if text in ['/лавки', '/магазины']:
        lines = ["🏪 **Состояние лавок:**\n"]
        for i, s in enumerate(STORES, 1):
            status = "🔴 Отключена" if s['disabled'] else "🟢 Активна"
            lines.append(f"  {i}. «**{s['name']}**» — {status}")
        send_message(MY_USER_ID, "\n".join(lines))
        return current_state

    if text in ['/статус', '/status']:
        state_str = "🟢 Активен" if current_state else "🔴 На паузе (/off)"
        stores_status = [f"  • №{i} «{s['name']}»: {'🔴 Выкл' if s['disabled'] else '🟢 Вкл'}"
                         for i, s in enumerate(STORES, 1)]
        m_list = [m['name'] for m in target_matrices] or ["Список пуст"]
        send_message(MY_USER_ID, (
            f"📊 **Системный статус бота**\n\n"
            f"⚙️ **Режим работы:** {state_str}\n"
            f"🎯 **Цели поиска:** {', '.join(m_list)}\n"
            f"🕒 **Последняя проверка:** {last_check_time}\n\n"
            f"🏪 **Лавки:**\n" + "\n".join(stores_status) + "\n\n"
            f"────────────────────────\n{get_weekly_stats_text()}"
        ))
        return current_state

    if text in ['/очистить', '/сброс', '/reset']:
        purchase_history = []
        save_json(HISTORY_FILE, purchase_history)
        send_message(MY_USER_ID, "🗑️ История покупок успешно сброшена!")
        return current_state

    return current_state


# ============================== ПАРСЕРЫ КАТАЛОГОВ ==============================
def parse_store_1(text, name):
    m = re.search(f"{re.escape(name)}.*?{{\\d+\\s*\\|\\s*(\\d+)}}.*?\\[(\\d+)/\\d+\\]",
                  text, re.DOTALL | re.IGNORECASE)
    return (int(m.group(2)), int(m.group(1))) if m else (0, 0)


def parse_store_2(text, name):
    m = re.search(f"{re.escape(name)}[^\\n]*?/?\\s*(\\d+)\\s*·\\s*\\[(\\d+)/", text, re.IGNORECASE)
    return (int(m.group(2)), int(m.group(1))) if m else (0, 0)


def parse_store_3(text, name):
    m = re.search(f"{re.escape(name)}.*?(?:\\((\\d+)/)?.*?\\[(\\d+)/\\d+\\]", text, re.DOTALL | re.IGNORECASE)
    if m:
        return (int(m.group(2)) if m.group(2) else 0), (int(m.group(1)) if m.group(1) else 0)
    return 0, 0


def parse_store_4(text, name):
    m = re.search(f"{re.escape(name)}.*?🔮\\s*([\\d\\s]+).*?\\[(\\d+)/", text, re.DOTALL | re.IGNORECASE)
    if m:
        price_str = m.group(1).replace(" ", "").replace("\xa0", "")
        return int(m.group(2)), (int(price_str) if price_str.isdigit() else 0)
    return 0, 0


PARSERS = {1: parse_store_1, 2: parse_store_2, 3: parse_store_3, 4: parse_store_4}


def parse_price_and_qty(text, store_id, matrix_name):
    parser = PARSERS.get(store_id)
    return parser(text, matrix_name) if parser else (0, 0)


def build_purchase_command(items):
    parts = [f"{i['name']} {i['qty']}" if i['qty'] > 1 else i['name'] for i in items]
    return "Купить " + ", ".join(parts)


def to_int(s):
    s = (s or "").replace(" ", "").replace("\xa0", "")
    return int(s) if s.isdigit() else None


# ============================== ПРОВЕРКА ЛАВКИ ==============================
def check_store(store):
    if store['disabled']:
        print(f"⏩ Пропускаем «{store['name']}» — лавка отключена.")
        return

    active = [m for m in target_matrices if store['id'] in m['stores']]
    if not active:
        print(f"⏩ Для лавки «{store['name']}» нет активных матриц в списке.")
        return

    print("\n----------------------------------------")
    print(f"📦 Проверяем магазин «{store['name']}»...")

    if store['catalog_type'] == 'payload':
        send_message(store['peer_id'], store['my_label'], store['my_payload'])
    else:
        send_message(store['peer_id'], store['catalog_cmd'])

    time.sleep(random.randint(14, 19))

    catalog_text = ""
    for item in get_history(store['peer_id'], 3):
        text = item.get('text', '')
        if any(m['name'].lower() in text.lower() for m in active):
            catalog_text = text
            break

    if not catalog_text:
        print(f"⏳ Ответ от «{store['name']}» получен, но искомые матрицы не найдены.")
        return

    current_items = []
    for mat in active:
        qty, price = parse_price_and_qty(catalog_text, store['id'], mat['name'])
        if qty > 0:
            current_items.append({'name': mat['name'], 'qty': qty, 'price': price})

    if not current_items:
        print(f"⏳ В «{store['name']}» нет доступных матриц (0 шт.).")
        return

    for _ in range(MAX_BUY_ATTEMPTS):
        if not current_items:
            break

        buy_cmd = build_purchase_command(current_items)
        print(f"🛒 Отправка команды в «{store['name']}»: «{buy_cmd}»")

        # запоминаем последнее сообщение ДО покупки — всё, что новее, относится к этой сделке
        before_id = get_last_msg_id(store['peer_id'])
        send_message(store['peer_id'], buy_cmd)

        # ждём ответ, при необходимости сами жмём «Подтвердить»
        ans_text = wait_for_result(store, before_id)
        ans_lower = ans_text.lower()

        if not ans_text:
            print(f"⏳ «{store['name']}» не дала итогового ответа.")
            return

        if "нет на складе" in ans_lower:
            print(f"⏳ Товар раскупили на складе «{store['name']}». Пропускаем.")
            return

        # ---------- успех ----------
        if any(k in ans_lower for k in SUCCESS_KEYWORDS) and "куплено за 0" not in ans_lower:
            for item in current_items:
                m = re.search(rf"{re.escape(item['name'])}.*?[×xX*]\s*(\d+)", ans_text, re.IGNORECASE)
                real_qty = int(m.group(1)) if m else item['qty']
                record_purchase(real_qty, store['name'], item['name'])
                print(f"✅ Выкуплено: {item['name']} ({real_qty} шт.) в «{store['name']}»!")

            m_left = re.search(r"(?:счет|баланс)[:\s\-]*([\d\s\xa0]+)", ans_text, re.IGNORECASE)
            if m_left:
                left = to_int(m_left.group(1))
                price = current_items[-1]['price']
                if left is not None and price > 0 and left < price:
                    store['disabled'] = True
                    print(f"⚠️ Остаток ({left}) меньше цены ({price}). Отключаем «{store['name']}».")
                    send_message(MY_USER_ID, f"⚠️ **Баланс исчерпан!** В лавке «{store['name']}» осталось {left}. Лавка отключена.")
            return

        # ---------- на складе меньше, чем просили ----------
        m_stock = re.search(r"Доступно:\s*(\d+)\s*шт\.\s*\"([^\"]+)\"", ans_text, re.IGNORECASE)
        if m_stock:
            avail = int(m_stock.group(1))
            stock_name = m_stock.group(2).lower()
            for item in current_items:
                if item['name'].lower() in stock_name:
                    print(f"⚠️ На складе меньше «{item['name']}»: {item['qty']} → {avail} шт.")
                    item['qty'] = avail
                    break
            continue

        # ---------- не хватает средств ----------
        m_need = re.search(r"Нужно[:\s\-]*(\d+)", ans_text, re.IGNORECASE)
        m_bal = re.search(r"(?:доступно|баланс)[:\s\-]*(\d+)", ans_text, re.IGNORECASE)

        if "недостаточно" in ans_lower or (m_need and m_bal):
            needed = None
            if m_need and m_bal:
                needed = int(m_need.group(1))
                deficit = needed - int(m_bal.group(1))
                print(f"💡 Недостаточно средств! Нужно: {needed}, баланс: {m_bal.group(1)}, дефицит: {deficit}")
            else:
                deficit = 1
                print("⚠️ Нехватка средств, цифры не распарсились. Уменьшаем заказ.")

            while deficit > 0 and current_items:
                last = current_items[-1]
                unit = last['price']
                if unit <= 0 and needed and last['qty'] > 0:
                    unit = needed // last['qty']
                if unit <= 0:
                    current_items.pop()
                    continue

                reduce_qty = (deficit + unit - 1) // unit if needed else 1
                if reduce_qty < last['qty']:
                    last['qty'] -= reduce_qty
                    deficit -= reduce_qty * unit
                    print(f"✂️ «{last['name']}» −{reduce_qty} шт. Новый заказ: {last['qty']} шт.")
                else:
                    deficit -= last['qty'] * unit
                    print(f"🗑️ «{current_items.pop()['name']}» убрана из заказа.")

            if not current_items:
                store['disabled'] = True
                print(f"⚠️ Баланс в «{store['name']}» исчерпан. Отключаем лавку.")
                send_message(MY_USER_ID, f"⚠️ **Недостаточно баланса!** В лавке «{store['name']}» закончились средства.")
                return
            continue

        print(f"❓ Непонятный ответ лавки «{store['name']}»: {ans_text[:200]!r}")
        return


# ============================== ГЛАВНЫЙ ЦИКЛ ==============================
def main():
    global last_check_time, last_processed_msg_id
    print("🚀 Скрипт авто-покупки матриц запущен!")

    if not VK_TOKEN:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: VK_TOKEN не задан!")
        return

    is_active = True
    last_processed_msg_id = get_last_msg_id_user(MY_USER_ID)

    while True:
        is_active = check_commands(is_active)
        if is_active:
            last_check_time = time.strftime("%H:%M:%S (%d.%m)")
            for store in STORES:
                try:
                    check_store(store)
                except Exception as e:
                    print(f"💥 Ошибка при работе с «{store['name']}»: {e}")
                time.sleep(random.randint(5, 10))

            wait_seconds = random.randint(20 * 60, 40 * 60)
            print(f"⏳ Следующий цикл проверки через {wait_seconds // 60} минут...\n")
        else:
            print("💤 Бот на паузе (/off)...", end="\r")
            wait_seconds = 10

        elapsed = 0
        while elapsed < wait_seconds:
            time.sleep(5)
            elapsed += 5
            is_active = check_commands(is_active)


def get_last_msg_id_user(user_id):
    data = vk_api_request("messages.getHistory", {'user_id': user_id, 'count': 1})
    if data and 'response' in data:
        items = data['response'].get('items', [])
        if items:
            return items[0].get('id', 0)
    return 0


if __name__ == "__main__":
    main()
