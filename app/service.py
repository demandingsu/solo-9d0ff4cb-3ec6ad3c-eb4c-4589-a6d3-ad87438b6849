"""核心业务逻辑：清单版本化装载、逐字取字库、交印出单。

规则要点
--------
1. 每次“成功上传”的字库清单整体存为一个不可变版本；app_meta 中的指针始终
   指向最近一次成功上传的清单。上传失败（含校验失败/保存失败）指针不动，
   继续使用上一份清单。
2. 一笔交印只引用一份完整清单：进入交印事务时锁定当前清单 ID，逐字按该清单
   的文件优先顺序取第一个收录它的字库，以 (字库, 版次, 字模号) 识别字模；
   有缺字则整笔拒绝，各册仍沿用原清单。
3. 已交印册的字模以 glyph_id 固定在各自交印时的清单版本上，历史单据原样保留；
   旧增减单仍可按单号原样重取与下载。
4. 同一字库同一版次内，字模号始终对应同一个字：新清单与任一历史版本冲突即拒绝。
5. 交印一册只更新该册选用的字模，其余册沿用各自上次交印的清单。
6. 比对交印前后“各册共需的字模（并集）”生成增减单。
7. 上传与交印共用进程内互斥锁，叠加 BEGIN IMMEDIATE + busy_timeout，逐单生效，
   每单提交前后都校验并集与各册清单一致。
8. 重复确认同一笔交印复用原单，按单号可原样重取。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

from .database import META_CURRENT_LIST, connect, init_db
from .errors import DomainError, MissingGlyphError, SaveError
from .parsers import LibraryRow

# 进程内互斥：上传替换与交印共用，保证清单指针切换与交印选字互不穿插。
# 数据库层 BEGIN IMMEDIATE + busy_timeout 则在多进程/多连接下兜底。
_write_lock = threading.Lock()

IDENTITY_KEYS = ("library_name", "edition", "matrix_no", "glyph_char")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _identity(row: sqlite3.Row | dict) -> dict:
    return {k: row[k] for k in IDENTITY_KEYS}


def _ident_key(ident: dict) -> tuple:
    return tuple(ident[k] for k in IDENTITY_KEYS)


def _sorted_idents(idents: list[dict]) -> list[dict]:
    return sorted(idents, key=_ident_key)


def _get_current_list_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key=?", (META_CURRENT_LIST,)
    ).fetchone()
    return int(row["value"]) if row is not None else None


def replace_libraries(rows: list[LibraryRow]) -> dict:
    """全量替换字库清单：整单校验通过后写入新版本并切换当前清单指针。

    任何校验或保存失败都不会改动指针，系统继续使用上一份清单。
    """
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous_list_id = _get_current_list_id(conn)

            # 1. 恒定不变量：同一 (字库, 版次, 字模号) 在所有历史版本中必须对应同一个字。
            for r in rows:
                clash = conn.execute(
                    """
                    SELECT g.glyph_char AS old_char
                    FROM glyphs g
                    JOIN libraries l ON l.library_id = g.library_id
                    WHERE l.name=? AND l.edition=? AND g.matrix_no=?
                      AND g.glyph_char != ?
                    LIMIT 1
                    """,
                    (r.library, r.edition, r.matrix_no, r.glyph_char),
                ).fetchone()
                if clash is not None:
                    raise DomainError(
                        f"字模号恒定校验失败：{r.library}/{r.edition}/{r.matrix_no} "
                        f"历史清单对应“{clash['old_char']}”，本次清单对应“{r.glyph_char}”；"
                        "同一字库同一版次内字模号必须始终对应同一个字，本次上传未生效"
                    )

            # 2. 整单写入一个全新的不可变清单版本（旧版本原样保留）。
            list_id = conn.execute(
                "INSERT INTO library_lists(source) VALUES ('upload')"
            ).lastrowid

            lib_id: dict[tuple[str, str], int] = {}
            priority = 0
            for r in rows:
                key = (r.library, r.edition)
                if key not in lib_id:
                    priority += 1
                    lid = conn.execute(
                        "INSERT INTO libraries(list_id, name, edition, priority) "
                        "VALUES (?,?,?,?)",
                        (list_id, r.library, r.edition, priority),
                    ).lastrowid
                    lib_id[key] = lid
                conn.execute(
                    "INSERT INTO glyphs(library_id, list_id, matrix_no, glyph_char) "
                    "VALUES (?,?,?,?)",
                    (lib_id[key], list_id, r.matrix_no, r.glyph_char),
                )

            # 3. 指针切换到新清单；只有走到这里提交成功，新清单才生效。
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (META_CURRENT_LIST, str(list_id)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        "library_list_id": list_id,
        "previous_library_list_id": previous_list_id,
        "library_count": len({(r.library, r.edition) for r in rows}),
        "glyph_count": len(rows),
        "replaced_at": _now_iso(),
    }


def _get_or_create_volume(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT volume_id FROM volumes WHERE name=?", (name,)).fetchone()
    if row:
        return row["volume_id"]
    cur = conn.execute("INSERT INTO volumes(name) VALUES (?)", (name,))
    return cur.lastrowid


def replace_volume_chars(volume_name: str, chars: list[str]) -> dict:
    """全量替换某册用字表（不动已交印的字模清单，交印时才重算）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        volume_id = _get_or_create_volume(conn, volume_name)
        conn.execute("DELETE FROM volume_chars WHERE volume_id=?", (volume_id,))
        conn.executemany(
            "INSERT INTO volume_chars(volume_id, glyph_char, source_order) VALUES (?,?,?)",
            [(volume_id, ch, i) for i, ch in enumerate(chars)],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"volume": volume_name, "char_count": len(chars), "updated_at": _now_iso()}


def _resolve_char(conn: sqlite3.Connection, list_id: int, ch: str) -> dict | None:
    """只在指定清单版本内，按字库优先顺序取第一个收录该字的字模。"""
    row = conn.execute(
        """
        SELECT l.name AS library_name, l.edition, g.matrix_no, g.glyph_char,
               g.glyph_id, l.library_id
        FROM glyphs g
        JOIN libraries l ON l.library_id = g.library_id
        WHERE g.list_id = ? AND g.glyph_char = ?
        ORDER BY l.priority ASC, g.matrix_no ASC
        LIMIT 1
        """,
        (list_id, ch),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _load_active_identities(conn: sqlite3.Connection, volume_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.name AS library_name, l.edition, g.matrix_no, g.glyph_char
        FROM active_matrices a
        JOIN glyphs g ON g.glyph_id = a.glyph_id
        JOIN libraries l ON l.library_id = g.library_id
        WHERE a.volume_id = ?
        ORDER BY l.name, l.edition, g.matrix_no
        """,
        (volume_id,),
    ).fetchall()
    return [_identity(r) for r in rows]


def _load_pool(conn: sqlite3.Connection, exclude_volume: int | None = None) -> list[dict]:
    """各册当前共需字模并集。"""
    sql = """
        SELECT DISTINCT l.name AS library_name, l.edition, g.matrix_no, g.glyph_char
        FROM active_matrices a
        JOIN glyphs g ON g.glyph_id = a.glyph_id
        JOIN libraries l ON l.library_id = g.library_id
    """
    params: tuple = ()
    if exclude_volume is not None:
        sql += " WHERE a.volume_id != ?"
        params = (exclude_volume,)
    sql += " ORDER BY l.name, l.edition, g.matrix_no"
    rows = conn.execute(sql, params).fetchall()
    return [_identity(r) for r in rows]


def _fingerprint(volume_id: int, chars: list[str], target: list[dict]) -> str:
    """同一笔交印的判重指纹：同册、同用字、同解析目标字模。"""
    raw = json.dumps(
        {
            "volume_id": volume_id,
            "chars": chars,
            "target": _sorted_idents(target),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _assert_pool_consistent(conn: sqlite3.Connection, stage: str) -> None:
    """校验：各册 active 并集 == 按各册 active 清单解析出的需求并集（已交印册）。"""
    actual = {_ident_key(x) for x in _load_pool(conn)}
    expected: set[tuple] = set()
    vols = conn.execute("SELECT volume_id FROM volumes").fetchall()
    for v in vols:
        vid = v["volume_id"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM active_matrices WHERE volume_id=?", (vid,)
        ).fetchone()["c"]
        if active == 0:
            continue  # 尚未交印过的册不参与共需
        for ident in _load_active_identities(conn, vid):
            expected.add(_ident_key(ident))
    if actual != expected:
        raise SaveError(f"{stage} 共需字模一致性校验失败")


def submit_volume(
    volume_name: str,
    idem_key: str | None = None,
    fail_commit: bool = False,
) -> dict:
    """交印一册，返回增减单（原单复用则 reused=True）。"""
    with _write_lock:
        return _submit_tx(volume_name, idem_key, fail_commit)


def _submit_tx(volume_name: str, idem_key: str | None, fail_commit: bool) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        vol = conn.execute(
            "SELECT volume_id FROM volumes WHERE name=?", (volume_name,)
        ).fetchone()
        if vol is None:
            raise DomainError(f"册“{volume_name}”不存在，请先上传用字表", 404)
        volume_id = vol["volume_id"]

        # 整笔交印只使用一份完整清单：事务开始即锁定当前清单版本。
        list_id = _get_current_list_id(conn)
        if list_id is None:
            raise DomainError("尚未成功上传过字库清单，无法交印", 400)

        # 1. 幂等键命中：原样复用
        if idem_key:
            old = conn.execute(
                "SELECT payload_json FROM orders WHERE idem_key=?", (idem_key,)
            ).fetchone()
            if old is not None:
                payload = json.loads(old["payload_json"])
                payload["reused"] = True
                conn.rollback()
                return payload

        chars = [
            r["glyph_char"]
            for r in conn.execute(
                "SELECT glyph_char FROM volume_chars WHERE volume_id=? ORDER BY source_order",
                (volume_id,),
            ).fetchall()
        ]
        if not chars:
            raise DomainError(f"册“{volume_name}”的用字表为空", 404)

        # 2. 只按锁定的清单版本逐字取第一个收录它的字库；有缺字则整笔拒绝，
        #    不动任何册的原清单，也不产生新单。
        resolved: list[dict] = []
        missing: list[str] = []
        for ch in chars:
            found = _resolve_char(conn, list_id, ch)
            if found is None:
                missing.append(ch)
            else:
                resolved.append(found)
        if missing:
            raise MissingGlyphError(missing)

        # 按字模去重（不同字不会同号，去重仅为防御并保证确定性顺序）
        unique: dict[int, dict] = {}
        for item in resolved:
            unique.setdefault(item["glyph_id"], item)
        after_idents = _sorted_idents([_identity(x) for x in unique.values()])
        before_idents = _load_active_identities(conn, volume_id)
        before_keys = {_ident_key(x) for x in before_idents}
        target_keys = {_ident_key(x) for x in after_idents}

        # 3. 无幂等键时的重复确认：同册、同用字、解析目标与当前已交印清单
        #    完全相同（即没有任何变化）时，复用该册最近一笔原单
        fp = _fingerprint(volume_id, chars, after_idents)
        if not idem_key and before_keys == target_keys:
            old = conn.execute(
                "SELECT payload_json FROM orders "
                "WHERE volume_id=? ORDER BY seq DESC LIMIT 1",
                (volume_id,),
            ).fetchone()
            if old is not None:
                payload = json.loads(old["payload_json"])
                payload["reused"] = True
                conn.rollback()
                return payload

        # 4. 交印前共需并集（含本册旧清单）
        pool_before = _load_pool(conn)
        before_pool_keys = {_ident_key(x) for x in pool_before}

        # 5. 只更新本册
        conn.execute("DELETE FROM active_matrices WHERE volume_id=?", (volume_id,))
        conn.executemany(
            "INSERT INTO active_matrices(volume_id, glyph_id) VALUES (?,?)",
            [(volume_id, gid) for gid in unique],
        )

        # 6. 交印后共需并集，逐字模比对生成增减单
        pool_after = _load_pool(conn)
        after_pool_keys = {_ident_key(x) for x in pool_after}
        added = _sorted_idents([x for x in pool_after if _ident_key(x) not in before_pool_keys])
        removed = _sorted_idents([x for x in pool_before if _ident_key(x) not in after_pool_keys])

        # 7. 逐单校验：提交前后共需并集都必须与各册 active 清单一致
        _assert_pool_consistent(conn, "交印后")

        # 8. 单号与落库
        row = conn.execute("SELECT seq FROM submit_seq").fetchone()
        next_seq = row["seq"] + 1
        conn.execute("UPDATE submit_seq SET seq=?", (next_seq,))
        order_no = f"MO{datetime.now().strftime('%Y%m%d')}-{next_seq:06d}"

        payload = {
            "order_no": order_no,
            "seq": next_seq,
            "volume": volume_name,
            "idem_key": idem_key,
            "reused": False,
            "fingerprint": fp,
            "library_list_id": list_id,
            "effective_at": _now_iso(),
            "pool_before": pool_before,
            "pool_after": pool_after,
            "added": added,
            "removed": removed,
            "volume_before": before_idents,
            "volume_after": after_idents,
        }
        conn.execute(
            "INSERT INTO orders(order_no, seq, volume_id, volume_name, idem_key, "
            "fingerprint, payload_json) VALUES (?,?,?,?,?,?,?)",
            (
                order_no,
                next_seq,
                volume_id,
                volume_name,
                idem_key,
                fp,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

        if fail_commit:
            # 测试钩子：模拟保存失败
            raise SaveError("模拟保存失败（测试）")
        conn.commit()
        return payload
    except DomainError:
        conn.rollback()
        raise
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise SaveError(f"数据库保存失败，已回滚，仍沿用原清单: {exc}")
    except Exception:
        conn.rollback()
        raise SaveError()
    finally:
        conn.close()


def get_order(order_no: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT payload_json FROM orders WHERE order_no=?", (order_no,)
        ).fetchone()
        if row is None:
            raise DomainError(f"单号 {order_no} 不存在", 404)
        payload = json.loads(row["payload_json"])
        payload["reused"] = False
        return payload
    finally:
        conn.close()


def list_orders(volume_name: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if volume_name:
            rows = conn.execute(
                "SELECT order_no, seq, volume_name, created_at, payload_json "
                "FROM orders WHERE volume_name=? ORDER BY seq",
                (volume_name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT order_no, seq, volume_name, created_at, payload_json "
                "FROM orders ORDER BY seq"
            ).fetchall()
        result = []
        for r in rows:
            p = json.loads(r["payload_json"])
            result.append(
                {
                    "order_no": r["order_no"],
                    "seq": r["seq"],
                    "volume": r["volume_name"],
                    "created_at": r["created_at"],
                    "library_list_id": p.get("library_list_id"),
                    "added_count": len(p["added"]),
                    "removed_count": len(p["removed"]),
                }
            )
        return result
    finally:
        conn.close()


def get_state() -> dict:
    """当前工位全貌：清单版本、字库、各册用字与已交印字模、共需并集。"""
    conn = connect()
    try:
        current_list_id = _get_current_list_id(conn)
        current_list = None
        if current_list_id is not None:
            row = conn.execute(
                "SELECT list_id, source, created_at FROM library_lists WHERE list_id=?",
                (current_list_id,),
            ).fetchone()
            if row is not None:
                current_list = dict(row)
        libs = [
            dict(r)
            for r in conn.execute(
                "SELECT name, edition, priority FROM libraries "
                "WHERE list_id=? ORDER BY priority",
                (current_list_id,),
            ).fetchall()
        ] if current_list_id is not None else []
        list_history = [
            dict(r)
            for r in conn.execute(
                "SELECT list_id, source, created_at FROM library_lists ORDER BY list_id"
            ).fetchall()
        ]
        volumes = []
        for v in conn.execute("SELECT volume_id, name FROM volumes ORDER BY volume_id"):
            chars = [
                r["glyph_char"]
                for r in conn.execute(
                    "SELECT glyph_char FROM volume_chars WHERE volume_id=? "
                    "ORDER BY source_order",
                    (v["volume_id"],),
                ).fetchall()
            ]
            active = _load_active_identities(conn, v["volume_id"])
            volumes.append(
                {"volume": v["name"], "chars": chars, "active_matrices": active}
            )
        pool = _load_pool(conn)
        return {
            "current_library_list": current_list,
            "library_list_history": list_history,
            "libraries": libs,
            "volumes": volumes,
            "shared_pool": pool,
        }
    finally:
        conn.close()


def reset_all() -> None:
    """清空全部业务数据（开发/演示用）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in (
            "orders",
            "active_matrices",
            "volume_chars",
            "volumes",
            "glyphs",
            "libraries",
            "library_lists",
            "app_meta",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE submit_seq SET seq=0")
        # 清空后 AUTOINCREMENT 序列归零，便于演示反复使用。
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_db() -> None:
    """建表/升级旧库并初始化序号（连接时会自动创建数据目录）。"""
    init_db()
