import sqlite3
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

class BankDB:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS game (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT NOT NULL,
                start_balance INTEGER NOT NULL,
                salary INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                card_uid TEXT UNIQUE,
                balance INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'В игре',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_player_id INTEGER,
                target_player_id INTEGER,
                amount INTEGER NOT NULL CHECK (amount > 0),
                description TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                reverses_transaction_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(source_player_id) REFERENCES players(id),
                FOREIGN KEY(target_player_id) REFERENCES players(id),
                FOREIGN KEY(reverses_transaction_id) REFERENCES transactions(id)
            );
            """)

    def get_game(self):
        with self.connect() as con:
            row = con.execute("SELECT * FROM game WHERE id=1").fetchone()
            return dict(row) if row else None

    def create_game(self, name, start_balance, salary):
        with self.connect() as con:
            existing = con.execute("SELECT id FROM game WHERE id=1").fetchone()
            if existing:
                raise ValueError("Партия уже существует. Для новой партии сначала потребуется отдельная функция завершения/архивации.")
            con.execute(
                "INSERT INTO game(id,name,start_balance,salary,created_at) VALUES(1,?,?,?,?)",
                (name.strip() or "Monopoly", start_balance, salary, datetime.now().isoformat(timespec="seconds"))
            )

    def update_game_rules(self, salary, start_balance):
        with self.connect() as con:
            con.execute("UPDATE game SET salary=?, start_balance=? WHERE id=1", (salary, start_balance))

    def add_player(self, name, card_uid=None):
        name = name.strip()
        if not name:
            raise ValueError("Укажи имя игрока.")
        game = self.get_game()
        if not game:
            raise ValueError("Сначала создай партию.")
        with self.connect() as con:
            if card_uid and con.execute("SELECT id FROM players WHERE card_uid=?", (card_uid,)).fetchone():
                raise ValueError("Эта карта уже привязана к другому игроку.")
            cur = con.execute(
                "INSERT INTO players(name,card_uid,balance,created_at) VALUES(?,?,?,?)",
                (name, card_uid, game["start_balance"], datetime.now().isoformat(timespec="seconds"))
            )
            player_id = cur.lastrowid
            con.execute(
                """INSERT INTO transactions(source_player_id,target_player_id,amount,description,kind,status,created_at)
                   VALUES(NULL,?,?,?,'initial_balance','completed',?)""",
                (player_id, game["start_balance"], "Стартовый капитал", datetime.now().isoformat(timespec="seconds"))
            )

    def bind_card(self, player_id, card_uid):
        if not card_uid:
            raise ValueError("Сначала приложи карту или введи UID.")
        with self.connect() as con:
            duplicate = con.execute(
                "SELECT id FROM players WHERE card_uid=? AND id<>?", (card_uid, player_id)
            ).fetchone()
            if duplicate:
                raise ValueError("Эта карта уже принадлежит другому игроку.")
            con.execute("UPDATE players SET card_uid=? WHERE id=?", (card_uid, player_id))

    def get_players(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM players ORDER BY id").fetchall()]

    def get_player_by_card(self, uid):
        with self.connect() as con:
            row = con.execute("SELECT * FROM players WHERE card_uid=?", (uid,)).fetchone()
            return dict(row) if row else None

    def transfer(self, source_id, target_id, amount, description, kind="transfer"):
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Сумма должна быть больше нуля.")
        if source_id is not None and source_id == target_id:
            raise ValueError("Нельзя переводить деньги самому себе.")
        description = description.strip() or "Игровая операция"

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if source_id is not None:
                source = con.execute("SELECT balance FROM players WHERE id=?", (source_id,)).fetchone()
                if not source:
                    raise ValueError("Плательщик не найден.")
                if source["balance"] < amount:
                    raise ValueError(f"Недостаточно средств. Доступно: {source['balance']} ₽.")
            if target_id is not None:
                target = con.execute("SELECT id FROM players WHERE id=?", (target_id,)).fetchone()
                if not target:
                    raise ValueError("Получатель не найден.")

            if source_id is not None:
                con.execute("UPDATE players SET balance=balance-? WHERE id=?", (amount, source_id))
            if target_id is not None:
                con.execute("UPDATE players SET balance=balance+? WHERE id=?", (amount, target_id))

            con.execute(
                """INSERT INTO transactions
                (source_player_id,target_player_id,amount,description,kind,status,created_at)
                VALUES(?,?,?,?,?,'completed',?)""",
                (source_id, target_id, amount, description, kind, datetime.now().isoformat(timespec="seconds"))
            )

    def get_transactions(self):
        with self.connect() as con:
            rows = con.execute("""
                SELECT t.*, s.name AS source_name, d.name AS target_name
                FROM transactions t
                LEFT JOIN players s ON s.id=t.source_player_id
                LEFT JOIN players d ON d.id=t.target_player_id
                ORDER BY t.id DESC
            """).fetchall()
            return [dict(r) for r in rows]

    def reverse_transaction(self, transaction_id, reason):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            tx = con.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if not tx or tx["status"] != "completed":
                raise ValueError("Операция не найдена или уже отменена.")
            if tx["reverses_transaction_id"] is not None:
                raise ValueError("Нельзя отменить обратную транзакцию.")
            if con.execute(
                "SELECT id FROM transactions WHERE reverses_transaction_id=?", (transaction_id,)
            ).fetchone():
                raise ValueError("Эта операция уже отменена.")

            source_id = tx["target_player_id"]
            target_id = tx["source_player_id"]
            amount = tx["amount"]

            if source_id is not None:
                source = con.execute("SELECT balance FROM players WHERE id=?", (source_id,)).fetchone()
                if not source or source["balance"] < amount:
                    raise ValueError("Нельзя отменить: у получателя уже недостаточно денег для возврата.")
            if source_id is not None:
                con.execute("UPDATE players SET balance=balance-? WHERE id=?", (amount, source_id))
            if target_id is not None:
                con.execute("UPDATE players SET balance=balance+? WHERE id=?", (amount, target_id))

            con.execute(
                "UPDATE transactions SET status='reversed' WHERE id=?", (transaction_id,)
            )
            con.execute(
                """INSERT INTO transactions
                (source_player_id,target_player_id,amount,description,kind,status,reverses_transaction_id,created_at)
                VALUES(?,?,?,?,?,'completed',?,?)""",
                (source_id, target_id, amount, f"Отмена #{transaction_id}: {reason}", "reversal",
                 transaction_id, datetime.now().isoformat(timespec="seconds"))
            )
