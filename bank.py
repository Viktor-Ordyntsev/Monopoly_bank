import sqlite3
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager


def now():
    return datetime.now().isoformat(timespec="seconds")


class BankDB:
    """SQLite storage for games, players, and an immutable-style transaction ledger."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                start_balance INTEGER NOT NULL CHECK(start_balance >= 0),
                salary INTEGER NOT NULL CHECK(salary >= 0),
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                card_uid TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'В игре',
                created_at TEXT NOT NULL,
                UNIQUE(game_id, card_uid)
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                source_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
                target_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
                amount INTEGER NOT NULL CHECK(amount > 0),
                description TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                reverses_transaction_id INTEGER REFERENCES transactions(id),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY, value TEXT
            );
            """)
            # One-time migration from the original single-game schema.
            old = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='game'").fetchone()
            if old:
                row = con.execute("SELECT * FROM game WHERE id=1").fetchone()
                if row and not con.execute("SELECT 1 FROM games LIMIT 1").fetchone():
                    con.execute("INSERT INTO games(id,name,start_balance,salary,status,created_at) VALUES(?,?,?,?,?,?)",
                                (row['id'], row['name'], row['start_balance'], row['salary'], 'active', row['created_at']))
                    con.execute("INSERT OR REPLACE INTO app_state(key,value) VALUES('active_game_id','1')")
                    # SQLite cannot add a NOT NULL FK without rebuilding tables. Rebuild while preserving IDs/history.
                    con.execute("ALTER TABLE transactions RENAME TO transactions_legacy")
                    con.execute("ALTER TABLE players RENAME TO players_legacy")
                    con.executescript("""
                    CREATE TABLE players (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                        name TEXT NOT NULL, card_uid TEXT, balance INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'В игре', created_at TEXT NOT NULL, UNIQUE(game_id,card_uid));
                    CREATE TABLE transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                        source_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL,
                        target_player_id INTEGER REFERENCES players(id) ON DELETE SET NULL, amount INTEGER NOT NULL CHECK(amount>0),
                        description TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'completed',
                        reverses_transaction_id INTEGER REFERENCES transactions(id), created_at TEXT NOT NULL);
                    """)
                    con.execute("INSERT INTO players SELECT id,1,name,card_uid,balance,status,created_at FROM players_legacy")
                    con.execute("INSERT INTO transactions SELECT id,1,source_player_id,target_player_id,amount,description,kind,status,reverses_transaction_id,created_at FROM transactions_legacy")
                    con.execute("DROP TABLE transactions_legacy")
                    con.execute("DROP TABLE players_legacy")
                con.execute("DROP TABLE game")
            con.execute("INSERT OR IGNORE INTO app_state(key,value) VALUES('active_game_id', NULL)")

    def get_active_game_id(self):
        with self.connect() as con:
            row = con.execute("SELECT value FROM app_state WHERE key='active_game_id'").fetchone()
            return int(row['value']) if row and row['value'] else None

    def set_active_game(self, game_id):
        with self.connect() as con:
            if game_id is not None and not con.execute("SELECT id FROM games WHERE id=?", (game_id,)).fetchone():
                raise ValueError("Партия не найдена.")
            con.execute("UPDATE app_state SET value=? WHERE key='active_game_id'", (str(game_id) if game_id else None,))

    def list_games(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT g.*,COUNT(p.id) player_count FROM games g LEFT JOIN players p ON p.game_id=g.id GROUP BY g.id ORDER BY g.id DESC")]

    def get_game(self, game_id=None):
        game_id = game_id or self.get_active_game_id()
        if game_id is None: return None
        with self.connect() as con:
            row = con.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            return dict(row) if row else None

    def create_game(self, name, start_balance, salary):
        name = name.strip()
        if not name: raise ValueError("Укажи название партии.")
        with self.connect() as con:
            cur = con.execute("INSERT INTO games(name,start_balance,salary,created_at) VALUES(?,?,?,?)", (name,int(start_balance),int(salary),now()))
            game_id = cur.lastrowid
            con.execute("UPDATE app_state SET value=? WHERE key='active_game_id'", (str(game_id),))
            return game_id

    def update_game(self, game_id, name, start_balance, salary):
        if not name.strip(): raise ValueError("Название партии не может быть пустым.")
        with self.connect() as con:
            cur=con.execute("UPDATE games SET name=?,start_balance=?,salary=? WHERE id=?", (name.strip(),int(start_balance),int(salary),game_id))
            if not cur.rowcount: raise ValueError("Партия не найдена.")

    def finish_game(self, game_id):
        with self.connect() as con:
            con.execute("UPDATE games SET status='finished',finished_at=? WHERE id=?", (now(),game_id))
            con.execute("UPDATE app_state SET value=NULL WHERE key='active_game_id' AND value=?", (str(game_id),))

    def delete_game(self, game_id):
        with self.connect() as con:
            con.execute("DELETE FROM games WHERE id=?", (game_id,))
            con.execute("UPDATE app_state SET value=NULL WHERE key='active_game_id' AND value=?", (str(game_id),))

    def update_game_rules(self, salary, start_balance):
        game = self.get_game()
        if not game: raise ValueError("Нет выбранной партии.")
        self.update_game(game['id'], game['name'], start_balance, salary)

    def add_player(self, name, card_uid=None):
        game=self.get_game()
        if not game: raise ValueError("Сначала выбери или создай партию.")
        name=name.strip()
        if not name: raise ValueError("Укажи имя игрока.")
        card_uid=card_uid.strip().upper() if card_uid else None
        with self.connect() as con:
            cur=con.execute("INSERT INTO players(game_id,name,card_uid,balance,created_at) VALUES(?,?,?,?,?)", (game['id'],name,card_uid,game['start_balance'],now()))
            con.execute("INSERT INTO transactions(game_id,target_player_id,amount,description,kind,created_at) VALUES(?,?,?,'Стартовый капитал','initial_balance',?)", (game['id'],cur.lastrowid,max(1,game['start_balance']),now()))
            return cur.lastrowid

    def update_player(self, player_id, name, card_uid=None, status=None):
        if not name.strip(): raise ValueError("Имя игрока не может быть пустым.")
        with self.connect() as con:
            if status is None: con.execute("UPDATE players SET name=?,card_uid=? WHERE id=?", (name.strip(),card_uid.strip().upper() or None if card_uid else None,player_id))
            else: con.execute("UPDATE players SET name=?,card_uid=?,status=? WHERE id=?", (name.strip(),card_uid.strip().upper() or None if card_uid else None,status,player_id))

    def delete_player(self, player_id):
        with self.connect() as con:
            con.execute("DELETE FROM players WHERE id=?", (player_id,))

    def bind_card(self, player_id, card_uid):
        if not card_uid: raise ValueError("Сначала приложи карту или введи UID.")
        with self.connect() as con:
            con.execute("UPDATE players SET card_uid=? WHERE id=?", (card_uid.strip().upper(),player_id))

    def get_players(self):
        game_id=self.get_active_game_id()
        if game_id is None: return []
        with self.connect() as con: return [dict(r) for r in con.execute("SELECT * FROM players WHERE game_id=? ORDER BY id",(game_id,))]

    def get_player_by_card(self, uid):
        with self.connect() as con:
            row=con.execute("SELECT p.* FROM players p JOIN games g ON g.id=p.game_id WHERE p.card_uid=? AND p.game_id=?",(uid,self.get_active_game_id())).fetchone()
            return dict(row) if row else None

    def transfer(self, source_id, target_id, amount, description, kind="transfer"):
        amount=int(amount); game=self.get_game()
        if not game: raise ValueError("Нет выбранной партии.")
        if amount<=0: raise ValueError("Сумма должна быть больше нуля.")
        if source_id is not None and source_id==target_id: raise ValueError("Нельзя переводить деньги самому себе.")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for pid in (source_id,target_id):
                if pid is not None and not con.execute("SELECT id FROM players WHERE id=? AND game_id=?",(pid,game['id'])).fetchone(): raise ValueError("Игрок не найден в выбранной партии.")
            if source_id is not None:
                row=con.execute("SELECT balance FROM players WHERE id=?",(source_id,)).fetchone()
                if row['balance']<amount: raise ValueError(f"Недостаточно средств. Доступно: {row['balance']} ₽.")
                con.execute("UPDATE players SET balance=balance-? WHERE id=?",(amount,source_id))
            if target_id is not None: con.execute("UPDATE players SET balance=balance+? WHERE id=?",(amount,target_id))
            con.execute("INSERT INTO transactions(game_id,source_player_id,target_player_id,amount,description,kind,created_at) VALUES(?,?,?,?,?,?,?)",(game['id'],source_id,target_id,amount,description.strip() or 'Игровая операция',kind,now()))

    def get_transactions(self):
        game_id=self.get_active_game_id()
        if game_id is None: return []
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT t.*,s.name source_name,d.name target_name FROM transactions t LEFT JOIN players s ON s.id=t.source_player_id LEFT JOIN players d ON d.id=t.target_player_id WHERE t.game_id=? ORDER BY t.id DESC",(game_id,))]

    def reverse_transaction(self, transaction_id, reason):
        with self.connect() as con:
            tx=con.execute("SELECT * FROM transactions WHERE id=? AND game_id=?",(transaction_id,self.get_active_game_id())).fetchone()
            if not tx or tx['status']!='completed' or tx['reverses_transaction_id'] is not None: raise ValueError("Операция не найдена или уже отменена.")
            if con.execute("SELECT 1 FROM transactions WHERE reverses_transaction_id=?",(transaction_id,)).fetchone(): raise ValueError("Эта операция уже отменена.")
            self.transfer(tx['target_player_id'],tx['source_player_id'],tx['amount'],f"Отмена #{transaction_id}: {reason}",'reversal')
            con.execute("UPDATE transactions SET status='reversed' WHERE id=?",(transaction_id,))
