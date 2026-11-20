"""SQLite 连接、建表与旧版数据升级。

数据库路径由环境变量 MATRIX_DB_PATH 决定，默认 data/matrix.db。
连接采用 autocommit + 显式 BEGIN IMMEDIATE，配合忙等待，保证交印事务串行生效。

清单版本化
----------
每次“成功上传”的字库清单都整体存为一个不可变版本（library_lists），
app_meta 中的 current_library_list_id 指向最近一次成功上传的清单。
交印只按该清单的优先顺序取字；上传失败则指针不动，继续使用上一份清单。
旧版数据库（libraries.current 模型）在首次打开时自动迁移为“升级前清单”，
首次成功重传前的交印沿用升级前的选字结果。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("MATRIX_DB_PATH", str(BASE_DIR / "data" / "matrix.db"))

META_CURRENT_LIST = "current_library_list_id"

# 全新数据库使用的建表语句。
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS library_lists (
        list_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS libraries (
        library_id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id    INTEGER NOT NULL REFERENCES library_lists(list_id),
        name       TEXT NOT NULL,
        edition    TEXT NOT NULL,
        priority   INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(list_id, name, edition)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS glyphs (
        glyph_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id INTEGER NOT NULL REFERENCES libraries(library_id),
        list_id    INTEGER NOT NULL REFERENCES library_lists(list_id),
        matrix_no  TEXT NOT NULL,
        glyph_char TEXT NOT NULL,
        UNIQUE(library_id, matrix_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_glyphs_list_char ON glyphs(list_id, glyph_char)",
    """
    CREATE TABLE IF NOT EXISTS volumes (
        volume_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_chars (
        volume_id    INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_char   TEXT NOT NULL,
        source_order INTEGER NOT NULL,
        PRIMARY KEY (volume_id, glyph_char)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_matrices (
        volume_id INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE,
        glyph_id  INTEGER NOT NULL REFERENCES glyphs(glyph_id),
        PRIMARY KEY (volume_id, glyph_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS submit_seq (seq INTEGER NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_no       TEXT PRIMARY KEY,
        seq            INTEGER NOT NULL UNIQUE,
        volume_id      INTEGER NOT NULL,
        volume_name    TEXT NOT NULL,
        idem_key       TEXT,
        fingerprint    TEXT NOT NULL,
        payload_json   TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem
    ON orders(idem_key) WHERE idem_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]


def connect(db_path: str | None = None, *, pragma: bool = True) -> sqlite3.Connection:
    path = db_path or DB_PATH
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if pragma:
        conn.execute("PRAGMA foreign_keys=ON")
        # journal_mode=WAL 只能在事务外设置，且会引发隐式提交。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _is_legacy_schema(conn: sqlite3.Connection) -> bool:
    """旧版 libraries 表带 current 列、且没有清单版本表。"""
    cols = _table_columns(conn, "libraries")
    if not cols or "current" not in cols:
        return False
    lists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_lists'"
    ).fetchone()
    return lists is None


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把旧版（libraries.current）数据库升级为清单版本模型。

    - 旧 current=1 的字库整体封存为“升级前清单”，并置为当前清单，
      因此首次成功重传前的交印沿用升级前的选字结果；
    - current=0 的历史字库封存为“升级前历史归档清单”，仅用于保住
      active_matrices / orders 快照引用，不参与新交印选字；
    - 保留原有 library_id / glyph_id / volume_id，已交印册与历史单据原样可用。

    迁移使用 autocommit DDL（journal_mode 等 PRAGMA 会引发隐式提交，
    无法包在单个事务里）。SQLite 外键在 schema 中记录父表内部名，
    重建父表后必须连带重建所有引用它的子表，否则外键悬挂、FK=ON 时 DML 报错。
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    legacy_current = conn.execute(
        "SELECT COUNT(*) AS c FROM libraries WHERE current=1"
    ).fetchone()["c"]
    legacy_archived = conn.execute(
        "SELECT COUNT(*) AS c FROM libraries WHERE current=0"
    ).fetchone()["c"]
    archive_id = 2 if legacy_current else 1
    try:
        # 1. 清单版本表与元数据表
        conn.execute(
            "CREATE TABLE library_lists ("
            "list_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        if legacy_current:
            conn.execute(
                "INSERT INTO library_lists(list_id, source) "
                "VALUES (1, 'legacy-current')"
            )
        if legacy_archived:
            conn.execute(
                "INSERT INTO library_lists(list_id, source) VALUES (?, 'legacy-archive')",
                (archive_id,),
            )
        conn.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if legacy_current:
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES (?, '1')",
                (META_CURRENT_LIST,),
            )

        # 2. 重建 libraries（父表）：去掉 current/UNIQUE(name,edition)，按清单归属
        conn.execute("ALTER TABLE libraries RENAME TO libraries_old")
        conn.execute(
            "CREATE TABLE libraries ("
            "library_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "list_id INTEGER NOT NULL REFERENCES library_lists(list_id), "
            "name TEXT NOT NULL, edition TEXT NOT NULL, priority INTEGER NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "UNIQUE(list_id, name, edition))"
        )
        conn.execute(
            "INSERT INTO libraries(library_id, list_id, name, edition, priority, created_at) "
            "SELECT library_id, CASE WHEN current=1 THEN 1 ELSE ? END, "
            "name, edition, priority, created_at FROM libraries_old",
            (archive_id,),
        )

        # 3. 重建 glyphs（父表）：字模显式归属清单版本
        conn.execute("DROP INDEX IF EXISTS idx_glyphs_char")
        conn.execute("ALTER TABLE glyphs RENAME TO glyphs_old")
        conn.execute(
            "CREATE TABLE glyphs ("
            "glyph_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "library_id INTEGER NOT NULL REFERENCES libraries(library_id), "
            "list_id INTEGER NOT NULL REFERENCES library_lists(list_id), "
            "matrix_no TEXT NOT NULL, glyph_char TEXT NOT NULL, "
            "UNIQUE(library_id, matrix_no))"
        )
        conn.execute(
            "INSERT INTO glyphs(glyph_id, library_id, list_id, matrix_no, glyph_char) "
            "SELECT g.glyph_id, g.library_id, l.list_id, g.matrix_no, g.glyph_char "
            "FROM glyphs_old g JOIN libraries l ON l.library_id = g.library_id"
        )
        conn.execute("CREATE INDEX idx_glyphs_list_char ON glyphs(list_id, glyph_char)")

        # 4. volumes 未变结构但被重建的子表引用，原样重建以刷新内部名
        conn.execute("ALTER TABLE volumes RENAME TO volumes_old")
        conn.execute(
            "CREATE TABLE volumes ("
            "volume_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL UNIQUE, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO volumes(volume_id, name, created_at) "
            "SELECT volume_id, name, created_at FROM volumes_old"
        )

        # 5. 重建引用 glyphs/volumes 的子表
        conn.execute("ALTER TABLE volume_chars RENAME TO volume_chars_old")
        conn.execute(
            "CREATE TABLE volume_chars ("
            "volume_id INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE, "
            "glyph_char TEXT NOT NULL, source_order INTEGER NOT NULL, "
            "PRIMARY KEY (volume_id, glyph_char))"
        )
        conn.execute(
            "INSERT INTO volume_chars(volume_id, glyph_char, source_order) "
            "SELECT volume_id, glyph_char, source_order FROM volume_chars_old"
        )

        conn.execute("ALTER TABLE active_matrices RENAME TO active_matrices_old")
        conn.execute(
            "CREATE TABLE active_matrices ("
            "volume_id INTEGER NOT NULL REFERENCES volumes(volume_id) ON DELETE CASCADE, "
            "glyph_id INTEGER NOT NULL REFERENCES glyphs(glyph_id), "
            "PRIMARY KEY (volume_id, glyph_id))"
        )
        conn.execute(
            "INSERT INTO active_matrices(volume_id, glyph_id) "
            "SELECT volume_id, glyph_id FROM active_matrices_old"
        )

        conn.execute("DROP INDEX IF EXISTS idx_orders_idem")
        conn.execute("ALTER TABLE orders RENAME TO orders_old")
        conn.execute(
            "CREATE TABLE orders ("
            "order_no TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, "
            "volume_id INTEGER NOT NULL, volume_name TEXT NOT NULL, "
            "idem_key TEXT, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO orders(order_no, seq, volume_id, volume_name, idem_key, "
            "fingerprint, payload_json, created_at) "
            "SELECT order_no, seq, volume_id, volume_name, idem_key, "
            "fingerprint, payload_json, created_at FROM orders_old"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_orders_idem ON orders(idem_key) "
            "WHERE idem_key IS NOT NULL"
        )

        # 6. 全部成功后清理旧表
        for t in (
            "orders_old",
            "active_matrices_old",
            "volume_chars_old",
            "volumes_old",
            "glyphs_old",
            "libraries_old",
        ):
            conn.execute(f"DROP TABLE {t}")
    except Exception:
        # 尽力回滚：删除新建表，把重命名的旧表改回原名。
        conn.execute("PRAGMA foreign_keys=OFF")
        existing = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            ).fetchall()
        }
        for t in (
            "orders",
            "active_matrices",
            "volume_chars",
            "volumes",
            "glyphs",
            "libraries",
        ):
            if f"{t}_old" in existing:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
                conn.execute(f"ALTER TABLE {t}_old RENAME TO {t}")
        for t in ("app_meta", "library_lists"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("DROP INDEX IF EXISTS idx_glyphs_list_char")
        conn.execute("DROP INDEX IF EXISTS idx_orders_idem")
        # 恢复迁移前旧库自带的两个索引。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_glyphs_char ON glyphs(glyph_char)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idem "
            "ON orders(idem_key) WHERE idem_key IS NOT NULL"
        )
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: str | None = None) -> None:
    path = db_path or DB_PATH
    # 仅当库文件已存在时才做遗留结构探测；mig 连接不能先建空文件，
    # 否则全新部署会留下一个不含任何表的空库，导致后续判断失真。
    if Path(path).exists():
        mig_conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        mig_conn.row_factory = sqlite3.Row
        try:
            if _is_legacy_schema(mig_conn):
                _migrate_legacy(mig_conn)
        finally:
            mig_conn.close()

    conn = connect(path)
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.execute("INSERT OR IGNORE INTO submit_seq(seq) VALUES (0)")
    finally:
        conn.close()
