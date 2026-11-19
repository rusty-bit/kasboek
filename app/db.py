"""SQLite storage. One file under DATA_DIR, plain SQL, no ORM: light enough for a 4 GB NAS."""
import os
import sqlite3
from contextlib import contextmanager

def data_dir():
    return os.environ.get("DATA_DIR", "/data")


def db_path():
    """Resolved on each call so tests (and a changed env) can point it elsewhere."""
    return os.environ.get("DB_PATH", os.path.join(data_dir(), "kasboek.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  iban TEXT UNIQUE,
  bank TEXT,
  kind TEXT NOT NULL DEFAULT 'checking',      -- checking | savings | credit
  owner TEXT NOT NULL DEFAULT 'shared',        -- shared | a person's name
  balance REAL,
  balance_date TEXT,
  source TEXT NOT NULL DEFAULT 'file',         -- file | enablebanking
  eb_uid TEXT,
  eb_session TEXT,
  included INTEGER NOT NULL DEFAULT 1,
  ext_key TEXT                                 -- account identity for files without an IBAN, e.g. comdirect:<nr>:Girokonto
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL,
  kind TEXT NOT NULL,                          -- expense | income | transfer
  essential INTEGER NOT NULL DEFAULT 0,
  everyday INTEGER NOT NULL DEFAULT 0,         -- repeats without being a bill (groceries)
  icon TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  date TEXT NOT NULL,
  amount REAL NOT NULL,                        -- negative = money out, positive = money in
  currency TEXT NOT NULL DEFAULT 'EUR',
  counterparty TEXT DEFAULT '',
  counterparty_iban TEXT DEFAULT '',
  description TEXT DEFAULT '',
  category_id INTEGER REFERENCES categories(id),
  cat_source TEXT,                             -- manual | transfer | rule | memory | keyword | llm
  dedupe_key TEXT UNIQUE NOT NULL,
  ignored INTEGER NOT NULL DEFAULT 0,
  note TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS tx_cat ON transactions(category_id);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  match_text TEXT NOT NULL,
  field TEXT NOT NULL DEFAULT 'any',           -- any | counterparty | description | iban
  direction TEXT,                              -- in | out | NULL
  amount_min REAL,
  amount_max REAL,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS merchant_memory (
  norm TEXT NOT NULL,
  direction TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (norm, direction)
);
CREATE TABLE IF NOT EXISTS llm_guesses (         -- AI suggestions; shown as 'AI guess' until you confirm
  norm TEXT NOT NULL,
  direction TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (norm, direction)
);
CREATE TABLE IF NOT EXISTS recurring (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  merchant TEXT NOT NULL,
  frequency TEXT NOT NULL,
  average REAL NOT NULL,
  last_amount REAL NOT NULL,
  prev_typical REAL,
  last_date TEXT NOT NULL,
  first_date TEXT,
  next_date TEXT NOT NULL,
  is_income INTEGER NOT NULL,
  status TEXT NOT NULL,
  category_id INTEGER,
  count INTEGER,
  -- contract folder (Finanzguru "Vertragsordner"), filled in by you
  is_contract INTEGER NOT NULL DEFAULT 0,
  contract_end TEXT,
  notice_days INTEGER,
  notes TEXT DEFAULT '',
  dismissed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pots (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  icon TEXT DEFAULT '',
  target REAL NOT NULL,
  target_date TEXT,                            -- YYYY-MM
  monthly_plan REAL NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 2,         -- 1 high, 2 normal, 3 nice to have
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS pot_moves (
  id INTEGER PRIMARY KEY,
  pot_id INTEGER NOT NULL REFERENCES pots(id),
  date TEXT NOT NULL,
  amount REAL NOT NULL,
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS habits (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category_id INTEGER,
  match_text TEXT,                             -- optional: only spending whose name contains this
  target REAL NOT NULL,                        -- monthly cap you are aiming for
  pot_id INTEGER REFERENCES pots(id),          -- where the money you don't spend should go
  created_at TEXT DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS budgets (
  category_id INTEGER PRIMARY KEY,
  monthly REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS analyses (            -- saved dashboard analyses ("Italy", "Bike")
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  categories TEXT DEFAULT '',                  -- comma-separated category keys; empty = all spending
  match_text TEXT DEFAULT '',                  -- name or description contains this
  date_from TEXT, date_to TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS eb_sessions (
  session_id TEXT PRIMARY KEY,
  aspsp TEXT, country TEXT, valid_until TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""

# key, label, kind, essential, everyday
CATEGORIES = [
    ("salary", "Salary", "income", 0, 0),
    ("child_benefit", "Child benefit / Kindergeld", "income", 0, 0),
    ("benefits", "Allowances & tax refunds", "income", 0, 0),
    ("side_income", "Side business income", "income", 0, 0),
    ("other_income", "Other income", "income", 0, 0),
    ("contribution", "Partner's deposit into the joint account", "income", 0, 0),  # from an account not in Kasboek
    ("housing", "Mortgage / rent", "expense", 1, 0),
    ("energy", "Energy & water", "expense", 1, 0),
    ("insurance", "Insurance", "expense", 1, 0),
    ("telecom", "Internet & phone", "expense", 1, 0),
    ("taxes", "Taxes & municipality", "expense", 1, 0),
    ("groceries", "Groceries", "expense", 1, 1),
    ("eating_out", "Eating out & takeaway", "expense", 0, 1),
    ("fuel", "Fuel & charging", "expense", 1, 1),
    ("car", "Car (maintenance, parking)", "expense", 0, 0),
    ("public_transport", "Public transport", "expense", 0, 1),
    ("kids", "Kids & childcare", "expense", 1, 0),
    ("health", "Health & pharmacy", "expense", 1, 0),
    ("personal_care", "Personal care", "expense", 0, 1),
    ("shopping", "Shopping", "expense", 0, 1),
    ("clothing", "Clothing", "expense", 0, 0),
    ("home", "Home, garden & DIY", "expense", 0, 0),
    ("subscriptions", "Subscriptions & media", "expense", 0, 0),
    ("holidays", "Holidays & travel", "expense", 0, 0),
    ("gifts", "Gifts & donations", "expense", 0, 0),
    ("hobbies", "Hobbies & sport", "expense", 0, 0),
    ("pets", "Pets", "expense", 0, 0),
    ("animals", "Animals", "expense", 0, 0),
    ("business", "Side business costs", "expense", 0, 0),
    ("cash", "Cash withdrawal (into the wallet)", "transfer", 0, 0),  # spending is what you log from the wallet
    ("fees", "Bank fees & interest", "expense", 0, 0),
    ("other", "Other spending", "expense", 0, 0),
    ("transfer", "Between own accounts", "transfer", 0, 0),
    ("investment", "Investments", "transfer", 0, 0),       # not spending: money moved into your depot
]


def connect():
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


# Columns added after the first schema. CREATE TABLE IF NOT EXISTS does not add them to an existing DB.
MIGRATIONS = [
    ("accounts", "ext_key", "TEXT"),
    ("transactions", "mcc", "TEXT"),                       # merchant category code (Enable Banking cards)
    ("transactions", "source", "TEXT NOT NULL DEFAULT 'file'"),  # file | enablebanking
    ("transactions", "alt_key", "TEXT"),                   # dedupe key of the same booking from the other source
    ("eb_sessions", "last_sync", "TEXT"),
    ("eb_sessions", "last_error", "TEXT"),
    ("recurring", "anchor_day", "INTEGER"),                 # day of month payments land on
    ("recurring", "kind", "TEXT"),                          # income | expense | transfer (category kind)
    ("recurring", "on_checking", "INTEGER NOT NULL DEFAULT 1"),  # paid from/to a checking account
    ("transactions", "personal_for", "TEXT NOT NULL DEFAULT ''"),  # only for this person (fair share)
]


def migrate(con):
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS accounts_ext_key ON accounts(ext_key)")
    con.execute("CREATE INDEX IF NOT EXISTS tx_alt_key ON transactions(alt_key)")
    con.execute("CREATE INDEX IF NOT EXISTS tx_account_date ON transactions(account_id, date)")


def init():
    with db() as con:
        con.executescript(SCHEMA)
        migrate(con)
        for key, label, kind, ess, every in CATEGORIES:
            con.execute(
                "INSERT OR IGNORE INTO categories(key,label,kind,essential,everyday) VALUES (?,?,?,?,?)",
                (key, label, kind, ess, every),
            )
        # Cash became a move into the wallet (2026-11-19): what you buy with it is logged by hand.
        con.execute("UPDATE categories SET kind='transfer', label=? WHERE key='cash' AND kind != 'transfer'",
                    (dict((k, l) for k, l, *_ in CATEGORIES)["cash"],))


def setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return os.environ.get(key.upper(), default)


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def cat_ids(con):
    return {r["key"]: r["id"] for r in con.execute("SELECT id,key FROM categories")}
