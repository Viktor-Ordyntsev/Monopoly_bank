import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import serial
import serial.tools.list_ports
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from bank import BankDB

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "monopoly_bank.sqlite3"

st.set_page_config(
    page_title="Monopoly Bank",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "selected_player" not in st.session_state:
    st.session_state.selected_player = None

@st.cache_resource
def get_bank():
    return BankDB(DB_PATH)

@st.cache_resource
def get_rfid_runtime():
    return {
        "events": queue.Queue(),
        "thread": None,
        "stop": threading.Event(),
        "port": None,
        "error": None,
        "connected": False,
    }

bank = get_bank()
runtime = get_rfid_runtime()


# ==========================================
# СОСТОЯНИЕ RFID И БАНКОВСКИХ ОПЕРАЦИЙ
# ==========================================

DEFAULT_STATE = {
    "selected_player": None,

    # Подготовленная операция
    "operation_preview": None,

    # Этап подтверждения:
    # None - нет операции
    # sender - ожидаем карту отправителя
    # receiver - ожидаем карту получателя
    # single - ожидаем карту игрока для операции с банком
    "confirmation_stage": None,

    "sender_verified": False,
    "receiver_verified": False,

    "expected_sender_id": None,
    "expected_receiver_id": None,

    # Сообщения интерфейса
    "rfid_message": "",
    "last_card_uid": None,
    "last_card_player_id": None,
    "last_card_at": None,
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value


def serial_worker(port, baud, events, stop_event, runtime_state):
    try:
        with serial.Serial(port, baud, timeout=0.5) as ser:
            runtime_state["connected"] = True
            runtime_state["error"] = None
            while not stop_event.is_set():
                try:
                    line = ser.readline().decode("ascii", errors="ignore").strip()
                    if line.startswith("CARD:"):
                        uid = line.split(":", 1)[1].strip().upper()
                        if uid:
                            events.put(uid)
                except serial.SerialException as exc:
                    runtime_state["error"] = str(exc)
                    break
    except Exception as exc:
        runtime_state["error"] = str(exc)
    finally:
        runtime_state["connected"] = False

def start_reader(port, baud):
    if runtime["thread"] and runtime["thread"].is_alive():
        if runtime["port"] == port:
            return
        runtime["stop"].set()
        runtime["thread"].join(timeout=1.0)

    runtime["stop"] = threading.Event()
    runtime["port"] = port
    runtime["error"] = None
    thread = threading.Thread(
        target=serial_worker,
        args=(port, baud, runtime["events"], runtime["stop"], runtime),
        daemon=True,
    )
    runtime["thread"] = thread
    thread.start()

def stop_reader():
    if runtime["thread"] and runtime["thread"].is_alive():
        runtime["stop"].set()
        runtime["thread"].join(timeout=1.0)
    runtime["connected"] = False
    runtime["port"] = None

def money(value):
    return f"{int(value):,}".replace(",", " ") + " ₽"

def render_player_cards(players):
    st.subheader("🎲 Игроки")

    cols = st.columns(3)

    for i, player in enumerate(players):
        with cols[i % 3]:
            selected = (
                st.session_state.selected_player == player["id"]
            )

            with st.container(border=True):
                if selected:
                    st.markdown("🟢 **ВЫБРАН**")

                st.markdown(f"### 🎮 {player['name']}")

                st.metric(
                    "Баланс",
                    money(player["balance"])
                )

                if player["card_uid"]:
                    st.caption("🪪 RFID-карта привязана")
                else:
                    st.caption("⚠️ Карта не привязана")

                if st.button(
                    "Выбрать игрока",
                    key=f"player_card_{player['id']}",
                    use_container_width=True
                ):
                    st.session_state.selected_player = player["id"]
                    st.rerun()



# ==========================================
# ОБРАБОТКА RFID-КАРТ
# ==========================================

def process_card_events():

    while True:
        try:
            uid = runtime["events"].get_nowait()
        except queue.Empty:
            break

        uid = str(uid).strip().upper()

        if not uid:
            continue

        # Ищем игрока по UID
        player = bank.get_player_by_card(uid)

        if player is None:
            st.session_state.rfid_message = (
                f"Неизвестная RFID-карта: {uid}"
            )
            continue

        player_id = player["id"]

        # Сохраняем информацию о последнем сканировании
        st.session_state.last_card_uid = uid
        st.session_state.last_card_player_id = player_id
        st.session_state.last_card_at = datetime.now().strftime("%H:%M:%S")

        stage = st.session_state.confirmation_stage

        # ======================================
        # ЭТАП 1. ПОДТВЕРЖДЕНИЕ ОТПРАВИТЕЛЯ
        # ======================================

        if stage == "sender":

            expected_id = st.session_state.expected_sender_id

            if player_id != expected_id:
                st.session_state.rfid_message = (
                    f"Это карта игрока {player['name']}. "
                    "Ожидается карта отправителя."
                )
                continue

            st.session_state.sender_verified = True

            preview = st.session_state.operation_preview

            # Если это перевод между игроками,
            # переходим к подтверждению получателя
            if preview["kind"] == "transfer":

                st.session_state.confirmation_stage = "receiver"

                st.session_state.rfid_message = (
                    f"Отправитель {player['name']} подтвердил перевод. "
                    "Теперь приложите карту получателя."
                )

            else:
                # Для операций с банком достаточно одной карты
                st.session_state.rfid_message = (
                    f"Игрок {player['name']} подтвердил операцию."
                )

                execute_operation()

        # ======================================
        # ЭТАП 2. ПОДТВЕРЖДЕНИЕ ПОЛУЧАТЕЛЯ
        # ======================================

        elif stage == "receiver":

            expected_id = st.session_state.expected_receiver_id

            if player_id == st.session_state.expected_sender_id:
                st.session_state.rfid_message = (
                    "Отправитель уже подтвердил операцию. "
                    "Теперь нужна карта получателя."
                )
                continue

            if player_id != expected_id:
                st.session_state.rfid_message = (
                    f"Карта игрока {player['name']} не подходит. "
                    "Ожидается карта получателя."
                )
                continue

            st.session_state.receiver_verified = True

            st.session_state.rfid_message = (
                f"Получатель {player['name']} подтвердил перевод. "
                "Выполняем операцию..."
            )

            execute_operation()

        # ======================================
        # НЕТ АКТИВНОЙ ОПЕРАЦИИ
        # ======================================

        else:

            st.session_state.selected_player = player_id

            st.session_state.rfid_message = (
                f"Выбран игрок: {player['name']}"
            )


# ==========================================
# ВЫПОЛНЕНИЕ ПОДТВЕРЖДЕННОЙ ОПЕРАЦИИ
# ==========================================

def execute_operation():

    preview = st.session_state.operation_preview

    if preview is None:
        return

    # Проверяем подтверждение отправителя
    if not st.session_state.sender_verified:
        return

    # Для перевода требуется подтверждение получателя
    if preview["kind"] == "transfer":

        if not st.session_state.receiver_verified:
            return

    try:

        bank.transfer(
            source_id=preview["source_id"],
            target_id=preview["target_id"],
            amount=preview["amount"],
            description=preview["description"],
            kind=preview["kind"]
        )

        st.session_state.rfid_message = (
            f"Операция успешно выполнена! "
            f"Сумма: {money(preview['amount'])}"
        )

    except Exception as error:

        st.session_state.rfid_message = (
            f"Ошибка выполнения операции: {error}"
        )

    finally:

        # Сбрасываем состояние операции
        st.session_state.operation_preview = None

        st.session_state.confirmation_stage = None

        st.session_state.sender_verified = False
        st.session_state.receiver_verified = False

        st.session_state.expected_sender_id = None
        st.session_state.expected_receiver_id = None



st_autorefresh(interval=700, key="rfid_poll")
process_card_events()

st.title("🏦 Monopoly Bank")
last_uid = st.session_state.get("last_card_uid")
last_player_id = st.session_state.get("last_card_player_id")
if last_uid:
    recognized = bank.get_player_by_card(last_uid)
    if recognized:
        st.success(f"RFID: распознан игрок **{recognized['name']}** · баланс {money(recognized['balance'])}")
    else:
        st.warning(f"RFID: считана незарегистрированная карта `{last_uid}`. Привяжи её на вкладке «Игроки».")
st.caption("Цифровой банк настольной игры")

with st.sidebar:
    st.header("Подключение RFID")
    ports = [p.device for p in serial.tools.list_ports.comports()]
    selected_port = st.selectbox(
        "COM-порт / Serial port",
        options=ports if ports else [""],
        help="Выбери порт Arduino Nano. Закрой Serial Monitor в Arduino IDE перед подключением.",
    )
    baud = st.selectbox("Скорость", [9600], index=0)
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Подключить", use_container_width=True, disabled=not selected_port):
            start_reader(selected_port, baud)
    with col_b:
        if st.button("Отключить", use_container_width=True):
            stop_reader()

    if runtime["connected"]:
        st.success(f"Подключено: {runtime['port']}")
    elif runtime["error"]:
        st.error(runtime["error"])
    else:
        st.info("RFID не подключён")

    st.divider()
    st.caption("Сначала прошей Arduino и закрой Serial Monitor.")

tabs = st.tabs(["🎲 Партии и игроки", "🎮 Игра", "📜 История", "⚙️ Настройки"])

with tabs[0]:
    st.subheader("Партии")
    games = bank.list_games()
    active_id = bank.get_active_game_id()
    if games:
        labels = {f"{'▶ ' if g['id'] == active_id else ''}{g['name']} · {g['status']} · игроков: {g['player_count']}": g['id'] for g in games}
        chosen_label = st.selectbox("Выбранная партия", list(labels), key="game_picker")
        chosen_id = labels[chosen_label]
        cols = st.columns(3)
        if cols[0].button("Сделать активной", disabled=chosen_id == active_id, use_container_width=True):
            bank.set_active_game(chosen_id); st.rerun()
        if cols[1].button("Завершить партию", disabled=games[[g['id'] for g in games].index(chosen_id)]['status'] != 'active', use_container_width=True):
            bank.finish_game(chosen_id); st.rerun()
        with st.expander("Удалить партию", expanded=False):
            st.warning("Удалятся игроки и история этой партии без возможности восстановления.")
            if st.button("Удалить выбранную партию", key="delete_game"):
                bank.delete_game(chosen_id); st.rerun()
    with st.expander("Создать новую партию", expanded=not bool(games)):
        with st.form("new_game_form"):
            game_name = st.text_input("Название", "Вечерняя Монополия")
            start_balance = st.number_input("Стартовый капитал", min_value=0, value=1500, step=100)
            salary = st.number_input("Зарплата за «Вперёд»", min_value=0, value=200, step=50)
            if st.form_submit_button("Создать и выбрать партию", type="primary"):
                try: bank.create_game(game_name, int(start_balance), int(salary)); st.rerun()
                except ValueError as exc: st.error(str(exc))

    game = bank.get_game()
    st.divider(); st.subheader("Игроки текущей партии")
    if not game:
        st.info("Выбери или создай партию, чтобы управлять игроками.")
    else:
        players = bank.get_players()
        if players:
            st.dataframe(pd.DataFrame([{"ID":p['id'],"Имя":p['name'],"Баланс":money(p['balance']),"UID карты":p['card_uid'] or '—',"Статус":p['status']} for p in players]), use_container_width=True, hide_index=True)
            pmap={f"{p['name']} · ID {p['id']}":p for p in players}
            with st.expander("Изменить игрока"):
                label=st.selectbox("Игрок",list(pmap),key="edit_player_pick"); p=pmap[label]
                with st.form("edit_player_form"):
                    new_name=st.text_input("Имя",p['name']); new_uid=st.text_input("UID карты",p['card_uid'] or "")
                    status=st.selectbox("Статус",["В игре","Банкрот"],index=0 if p['status']=="В игре" else 1)
                    if st.form_submit_button("Сохранить изменения"):
                        try: bank.update_player(p['id'],new_name,new_uid,status); st.rerun()
                        except (ValueError,sqlite3.IntegrityError) as exc: st.error(f"Не удалось сохранить: {exc}")
                if st.button("Удалить игрока",key=f"del_player_{p['id']}"):
                    bank.delete_player(p['id']); st.rerun()
        with st.expander("Добавить игрока"):
            with st.form("add_player_form"):
                name=st.text_input("Имя игрока",key="new_player_name")
                uid=st.text_input("UID RFID (необязательно)",value=st.session_state.get("last_card_uid", ""),key="new_player_uid")
                if st.form_submit_button("Добавить игрока"):
                    try: bank.add_player(name,uid or None); st.rerun()
                    except (ValueError,sqlite3.IntegrityError) as exc: st.error(f"Не удалось добавить: {exc}")
        if st.session_state.get("last_card_uid"):
            st.caption(f"Последняя считанная карта: {st.session_state.last_card_uid}")



# ==========================================
# ВКЛАДКА 3. БАНКОВСКИЕ ОПЕРАЦИИ
# ==========================================

with tabs[1]:

    st.subheader("🏦 Банковские операции")

    players = bank.get_players()
    game = bank.get_game()

    selected_id = st.session_state.selected_player

    player = next(
        (p for p in players if p["id"] == selected_id),
        None
    )

    # --------------------------------------
    # RFID-СООБЩЕНИЯ
    # --------------------------------------

    if st.session_state.rfid_message:
        st.info(st.session_state.rfid_message)

    # --------------------------------------
    # ЕСЛИ ИГРОК НЕ ВЫБРАН
    # --------------------------------------

    if player is None:

        st.warning(
            "Приложите RFID-карту игрока, "
            "чтобы выбрать его для операции."
        )

    else:

        st.success(f"Выбран игрок: {player['name']}")

        st.metric(
            "Текущий баланс",
            money(player["balance"])
        )

        st.divider()

        # ----------------------------------
        # ЕСЛИ ОПЕРАЦИЯ УЖЕ ПОДГОТОВЛЕНА
        # ----------------------------------

        preview = st.session_state.operation_preview

        if preview is not None:

            st.subheader("📋 Подтверждение операции")

            sender = next(
                (
                    p for p in players
                    if p["id"] == preview["source_id"]
                ),
                None
            ) if preview["source_id"] is not None else None

            receiver = next(
                (
                    p for p in players
                    if p["id"] == preview["target_id"]
                ),
                None
            ) if preview["target_id"] is not None else None

            # ----------------------------------
            # ИНФОРМАЦИЯ ОБ ОПЕРАЦИИ
            # ----------------------------------

            st.markdown(
                f"**Тип операции:** {preview['title']}"
            )

            st.markdown(
                f"**Сумма:** {money(preview['amount'])}"
            )

            st.markdown(
                f"**Назначение:** {preview['description']}"
            )

            if sender:
                st.markdown(
                    f"**Отправитель:** {sender['name']}"
                )
            else:
                st.markdown("**Отправитель:** Банк")

            if receiver:
                st.markdown(
                    f"**Получатель:** {receiver['name']}"
                )
            else:
                st.markdown("**Получатель:** Банк")

            st.divider()

            # ----------------------------------
            # ИНДИКАТОРЫ ПОДТВЕРЖДЕНИЯ
            # ----------------------------------

            if preview["kind"] == "transfer":

                col1, col2 = st.columns(2)

                with col1:

                    if st.session_state.sender_verified:
                        st.success("✅ Отправитель подтверждён")
                    else:
                        st.warning("⏳ Ожидаем отправителя")

                with col2:

                    if st.session_state.receiver_verified:
                        st.success("✅ Получатель подтверждён")
                    else:
                        st.warning("⏳ Ожидаем получателя")

                # Текущий этап
                if st.session_state.confirmation_stage == "sender":

                    st.warning(
                        "📡 Приложите RFID-карту отправителя "
                        "для подтверждения списания денег."
                    )

                elif st.session_state.confirmation_stage == "receiver":

                    st.warning(
                        "📡 Отправитель подтверждён. "
                        "Теперь приложите RFID-карту получателя."
                    )

            else:

                if st.session_state.sender_verified:
                    st.success("✅ Игрок подтвердил операцию")
                else:
                    st.warning(
                        "📡 Приложите RFID-карту игрока "
                        "для подтверждения операции."
                    )

            st.divider()

            # ----------------------------------
            # ОТМЕНА ОПЕРАЦИИ
            # ----------------------------------

            if st.button(
                "❌ Отменить операцию",
                use_container_width=True
            ):

                st.session_state.operation_preview = None
                st.session_state.confirmation_stage = None

                st.session_state.sender_verified = False
                st.session_state.receiver_verified = False

                st.session_state.expected_sender_id = None
                st.session_state.expected_receiver_id = None

                st.session_state.rfid_message = (
                    "Операция отменена."
                )

                st.rerun()

        # ----------------------------------
        # ЕСЛИ ОПЕРАЦИЯ ЕЩЁ НЕ ПОДГОТОВЛЕНА
        # ----------------------------------

        else:

            st.subheader("Новая операция")

            operation = st.selectbox(
                "Выберите операцию",
                [
                    "Перевод другому игроку",
                    "Оплата банку",
                    "Получить деньги из банка",
                    "Зарплата за прохождение «Вперёд»"
                ],
                key="operation_type"
            )

            amount = 0
            target_id = None
            description = ""
            kind = ""
            title = operation

            other_players = [
                p for p in players
                if p["id"] != selected_id
            ]

            # ==================================
            # ПЕРЕВОД ИГРОКУ
            # ==================================

            if operation == "Перевод другому игроку":

                st.markdown("### 💸 Перевод")

                if other_players:

                    target_id = st.selectbox(
                        "Получатель",
                        options=[
                            p["id"] for p in other_players
                        ],
                        format_func=lambda pid: next(
                            p["name"]
                            for p in other_players
                            if p["id"] == pid
                        )
                    )

                    amount = st.number_input(
                        "Сумма перевода",
                        min_value=1,
                        step=100,
                        value=1000
                    )

                    description = st.text_input(
                        "Назначение перевода",
                        value="Перевод между игроками"
                    )

                    kind = "transfer"

                else:

                    st.warning(
                        "Для перевода нужен хотя бы один другой игрок."
                    )

            # ==================================
            # ОПЛАТА БАНКУ
            # ==================================

            elif operation == "Оплата банку":

                st.markdown("### 🏦 Оплата банку")

                amount = st.number_input(
                    "Сумма оплаты",
                    min_value=1,
                    step=100,
                    value=1000
                )

                description = st.text_input(
                    "Назначение платежа",
                    value="Оплата банку"
                )

                kind = "payment"

            # ==================================
            # ПОЛУЧЕНИЕ ДЕНЕГ
            # ==================================

            elif operation == "Получить деньги из банка":

                st.markdown("### 💰 Получение денег")

                amount = st.number_input(
                    "Сумма начисления",
                    min_value=1,
                    step=100,
                    value=1000
                )

                description = st.text_input(
                    "Причина начисления",
                    value="Начисление от банка"
                )

                kind = "income"

            # ==================================
            # ЗАРПЛАТА
            # ==================================

            elif operation == "Зарплата за прохождение «Вперёд»":

                st.markdown("### 🏁 Зарплата")

                salary = game["salary"]

                st.metric(
                    "Размер зарплаты",
                    money(salary)
                )

                amount = salary

                description = "Прохождение «Вперёд»"

                kind = "salary"

            st.divider()

            # ==================================
            # ПОДГОТОВКА ОПЕРАЦИИ
            # ==================================

            if st.button(
                "📋 Подготовить операцию",
                use_container_width=True,
                type="primary"
            ):

                if amount <= 0:

                    st.error("Сумма должна быть больше нуля.")

                elif operation == "Перевод другому игроку" and target_id is None:

                    st.error("Выберите получателя.")

                elif operation == "Перевод другому игроку" and not description.strip():

                    st.error("Укажите назначение перевода.")

                elif operation == "Оплата банку" and not description.strip():

                    st.error("Укажите назначение платежа.")

                elif operation == "Получить деньги из банка" and not description.strip():

                    st.error("Укажите причину начисления.")

                elif kind in ("transfer", "payment") and player["balance"] < amount:

                    st.error("Недостаточно средств у игрока.")

                else:

                    # Источник и получатель
                    if kind in ("transfer", "payment"):
                        source_id = selected_id
                    else:
                        source_id = None

                    if kind == "payment":
                        destination_id = None
                    else:
                        destination_id = (
                            target_id
                            if kind == "transfer"
                            else selected_id
                        )

                    # Сохраняем операцию
                    st.session_state.operation_preview = {
                        "source_id": source_id,
                        "target_id": destination_id,
                        "amount": int(amount),
                        "description": description,
                        "kind": kind,
                        "title": title
                    }

                    # Подтверждения сбрасываются
                    st.session_state.sender_verified = False
                    st.session_state.receiver_verified = False

                    st.session_state.expected_sender_id = selected_id

                    st.session_state.expected_receiver_id = (
                        target_id
                        if kind == "transfer"
                        else None
                    )

                    # Для всех операций сначала ожидаем
                    # повторное сканирование карты игрока
                    st.session_state.confirmation_stage = "sender"

                    st.session_state.rfid_message = (
                        "Операция подготовлена. "
                        "Для продолжения приложите RFID-карту отправителя."
                    )

                    st.rerun()


with tabs[2]:
    st.subheader("История транзакций")
    transactions = bank.get_transactions()
    if transactions:
        st.dataframe(pd.DataFrame([{
            "Время": t["created_at"],
            "Откуда": t["source_name"] or "Банк",
            "Куда": t["target_name"] or "Банк",
            "Сумма": money(t["amount"]),
            "Назначение": t["description"],
            "Тип": t["kind"],
            "Статус": t["status"],
        } for t in transactions]), use_container_width=True, hide_index=True)

        reversible = [t for t in transactions if t["status"] == "completed"]
        if reversible:
            options = {
                f'#{t["id"]} · {t["description"]} · {money(t["amount"])}': t["id"]
                for t in reversible
            }
            with st.form("reverse_form"):
                chosen = st.selectbox("Выбери операцию для отмены", list(options))
                reason = st.text_input("Причина отмены", "Ошибочная операция")
                reverse = st.form_submit_button("Отменить через обратную транзакцию")
                if reverse:
                    try:
                        bank.reverse_transaction(options[chosen], reason)
                        st.success("Создана обратная транзакция.")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    else:
        st.info("Операций пока нет.")

with tabs[3]:
    st.subheader("Правила партии")
    game = bank.get_game()
    if not game:
        st.info("Сначала создай партию.")
    else:
        with st.form("rules_form"):
            salary = st.number_input("Выплата за «Вперёд»", min_value=0, value=int(game["salary"]), step=100)
            start_balance = st.number_input("Стартовый капитал (для справки)", min_value=0, value=int(game["start_balance"]), step=500)
            save = st.form_submit_button("Сохранить настройки")
            if save:
                bank.update_game_rules(int(salary), int(start_balance))
                st.success("Настройки сохранены.")
                st.rerun()

        st.markdown("#### Подсказки по домашним правилам")
        st.write(
            "Для любого нестандартного правила используй вкладку «Операции»: "
            "задай плательщика, получателя, сумму и понятное назначение. "
            "Например: «каждый платит владельцу банка 300 ₽» или "
            "«Игрок 2 возвращает долг Игроку 1»."
        )

st.divider()
st.caption("Monopoly Bank 0.1 · Локальный прототип. Не является официальным продуктом Hasbro.")
