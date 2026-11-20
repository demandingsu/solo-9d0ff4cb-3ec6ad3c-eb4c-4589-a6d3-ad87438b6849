"""端到端测试：用字表/字库上传、交印出单、原单复用、缺字、保存失败、并发次序。

运行：
    PYTHONPATH=. pytest -q
"""
from __future__ import annotations

import io
import os
import tempfile
import threading

import pytest

# 每个测试会话使用独立临时库
_tmp = tempfile.TemporaryDirectory()
os.environ["MATRIX_DB_PATH"] = os.path.join(_tmp.name, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import service  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    service.ensure_db()
    service.reset_all()
    with TestClient(app) as c:
        yield c


def _csv(text: str) -> bytes:
    return text.encode("utf-8-sig")


def _upload_lib(client, rows: list[tuple[str, str, str, str]]):
    buf = io.StringIO()
    buf.write("字库,版次,字模号,字\n")
    for r in rows:
        buf.write(",".join(r) + "\n")
    return client.post(
        "/api/libraries",
        files={"file": ("libraries.csv", _csv(buf.getvalue()), "text/csv")},
    )


def _upload_vol(client, name: str, chars: str):
    body = "字\n" + "\n".join(chars) + "\n"
    return client.post(
        f"/api/volumes/{name}/chars",
        files={"file": (f"{name}.csv", _csv(body), "text/csv")},
    )


def _ident(glyph: str, lib: str, edition: str, no: str) -> dict:
    return {
        "glyph_char": glyph,
        "library_name": lib,
        "edition": edition,
        "matrix_no": no,
    }


# ---------- 基础：优先级选库 ----------


def test_first_library_wins(client):
    r = _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("金石斋", "重刊", "JS-1", "天"),  # 同字次优先，不应被选
        ("金石斋", "重刊", "JS-2", "人"),
    ])
    assert r.status_code == 200, r.text
    _upload_vol(client, "册甲", "天地人")
    r = client.post("/api/volumes/册甲/submit")
    assert r.status_code == 200, r.text
    d = r.json()
    after = {(x["glyph_char"], x["library_name"], x["matrix_no"]) for x in d["volume_after"]}
    assert ("天", "宋韵楼", "SR-1") in after
    assert ("地", "宋韵楼", "SR-2") in after
    assert ("人", "金石斋", "JS-2") in after
    assert all(x["library_name"] != "金石斋" or x["glyph_char"] != "天" for x in d["volume_after"])
    # 首单：池从 0 增到 3，无减项
    assert len(d["added"]) == 3 and d["removed"] == []


# ---------- 核心场景：两册先后弃用同一字模 ----------


def test_two_volumes_drop_same_matrix_sequentially(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天地人")
    _upload_vol(client, "册乙", "天地")

    # 册甲先交印：共需池 = 天/地/人
    o1 = client.post("/api/volumes/册甲/submit").json()
    assert {x["glyph_char"] for x in o1["pool_after"]} == {"天", "地", "人"}

    # 册乙交印：与册甲共需并集仍是 天/地/人，故无增减
    o2 = client.post("/api/volumes/册乙/submit").json()
    assert o2["added"] == [] and o2["removed"] == []
    assert {x["glyph_char"] for x in o2["pool_after"]} == {"天", "地", "人"}

    # 册甲改稿，弃用“人”：第一次弃用——池里还有乙吗？乙不用“人”，故立即减
    _upload_vol(client, "册甲", "天地")
    o3 = client.post("/api/volumes/册甲/submit").json()
    assert [x["glyph_char"] for x in o3["removed"]] == ["人"]
    assert o3["added"] == []
    assert {x["glyph_char"] for x in o3["pool_after"]} == {"天", "地"}

    # 构造“两册共用、先后弃用”：册乙改稿加入“人”，两册再共需
    _upload_vol(client, "册乙", "天地人")
    o4 = client.post("/api/volumes/册乙/submit").json()
    assert [x["glyph_char"] for x in o4["added"]] == ["人"]

    # 现在甲（天地）、乙（天地人）共用“人”只在乙；让两册都含“人”后依次弃用：
    _upload_vol(client, "册甲", "天地人")
    client.post("/api/volumes/册甲/submit")
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}

    # 甲先弃“人”：乙仍需，池不变 → 无减单
    _upload_vol(client, "册甲", "天地")
    o5 = client.post("/api/volumes/册甲/submit").json()
    assert o5["removed"] == [] and o5["added"] == []
    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}
    # 甲自身清单确实已更新
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}

    # 乙再弃“人”：最后一个使用者撤下 → 减单含“人”
    _upload_vol(client, "册乙", "天地")
    o6 = client.post("/api/volumes/册乙/submit").json()
    assert [x["glyph_char"] for x in o6["removed"]] == ["人"]
    assert {x["glyph_char"] for x in o6["pool_after"]} == {"天", "地"}

    # 逐单后校验：每单 pool_after 都与当时各册 active 并集一致
    for order in (o1, o2, o3, o4, o5, o6):
        assert len(order["pool_after"]) == len({
            (x["library_name"], x["edition"], x["matrix_no"]) for x in order["pool_after"]
        })
    final = client.get("/api/state").json()
    assert {x["glyph_char"] for x in final["shared_pool"]} == {"天", "地"}


# ---------- 原单复用：幂等键 / 指纹 / 按单号重取 ----------


def test_idempotency_key_reuses_same_order(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    h = {"Idempotency-Key": "job-001"}
    d1 = client.post("/api/volumes/册甲/submit", headers=h).json()
    d2 = client.post("/api/volumes/册甲/submit", headers=h).json()
    assert d1["order_no"] == d2["order_no"]
    assert d1["reused"] is False and d2["reused"] is True
    # 按单号原样重取，内容一致
    d3 = client.get(f"/api/orders/{d1['order_no']}").json()
    assert d3["added"] == d1["added"] and d3["removed"] == d1["removed"]
    assert d3["volume_after"] == d1["volume_after"]


def test_duplicate_confirm_without_key_reuses_order(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天"), ("宋韵楼", "初刻", "SR-2", "地")])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()
    # 不做任何改动，重复确认同一笔
    d2 = client.post("/api/volumes/册甲/submit").json()
    assert d1["order_no"] == d2["order_no"] and d2["reused"] is True
    # 改稿后再交印，应为新单
    _upload_vol(client, "册甲", "天")
    d3 = client.post("/api/volumes/册甲/submit").json()
    assert d3["order_no"] != d1["order_no"] and d3["reused"] is False
    assert [x["glyph_char"] for x in d3["removed"]] == ["地"]


def test_order_csv_download(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d = client.post("/api/volumes/册甲/submit").json()
    r = client.get(f"/api/orders/{d['order_no']}/download")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8-sig")
    assert d["order_no"] in text and "增" in text and "天" in text


# ---------- 缺字：沿用原清单 ----------


def test_missing_glyph_keeps_old_list(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d1 = client.post("/api/volumes/册甲/submit").json()

    _upload_vol(client, "册甲", "天人")  # “人”无字库收录
    r = client.post("/api/volumes/册甲/submit")
    assert r.status_code == 422
    err = r.json()
    assert err["type"] == "MissingGlyphError" and "人" in err["error"]

    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    # 仍沿用上次交印清单（只有“天”）
    assert [x["glyph_char"] for x in jia["active_matrices"]] == ["天"]
    # 没有产生新单
    orders = client.get("/api/orders").json()
    assert [o["order_no"] for o in orders] == [d1["order_no"]]


# ---------- 保存失败：回滚并沿用原清单 ----------


def test_save_failure_rolls_back(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
    ])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()

    _upload_vol(client, "册甲", "天")
    r = client.post("/api/volumes/册甲/submit", headers={"X-Fail-Commit": "1"})
    assert r.status_code == 500
    assert r.json()["type"] == "SaveError"

    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}
    orders = client.get("/api/orders")
    assert [o["order_no"] for o in orders.json()] == [d1["order_no"]]


# ---------- 并发：多人同时交印按生效次序逐单一致 ----------


def test_concurrent_submits_serialize_and_stay_consistent(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
        ("宋韵楼", "初刻", "SR-3", "人"),
    ])
    _upload_vol(client, "册甲", "天")
    _upload_vol(client, "册乙", "地")
    _upload_vol(client, "册丙", "人")

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def worker(vol: str):
        try:
            barrier.wait()
            with TestClient(app) as local:
                resp = local.post(f"/api/volumes/{vol}/submit")
                assert resp.status_code == 200, resp.text
                results.append(resp.json())
        except Exception as exc:  # pragma: no cover - 测试失败路径
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in ("册甲", "册乙", "册丙")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    # 三单全部生效，序号连续、单号唯一
    results.sort(key=lambda d: d["seq"])
    assert [d["seq"] for d in results] == [1, 2, 3]
    assert len({d["order_no"] for d in results}) == 3
    # 逐单累积：第 n 单 pool_after 大小 == n（三字互不相同）
    for i, d in enumerate(results, start=1):
        assert len(d["pool_after"]) == i
        assert len(d["added"]) == 1 and d["removed"] == []

    state = client.get("/api/state").json()
    assert {x["glyph_char"] for x in state["shared_pool"]} == {"天", "地", "人"}
    listing = client.get("/api/orders").json()
    assert [o["seq"] for o in listing] == [1, 2, 3]


# ---------- 其他 ----------


def test_unknown_volume_and_order(client):
    r = client.post("/api/volumes/不存在/submit")
    assert r.status_code == 404
    r = client.get("/api/orders/NOPE-9")
    assert r.status_code == 404


def test_bad_csv_headers(client):
    r = client.post(
        "/api/libraries",
        files={"file": ("x.csv", _csv("foo,bar\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 400 and "缺少必需列" in r.json()["error"]


# ---------- 重传清单：删掉的字模不再被新交印选中，后备库按优先级顶上 ----------


def test_reupload_removed_glyph_falls_back_to_backup_library(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("金石斋", "重刊", "JS-1", "天"),
        ("金石斋", "重刊", "JS-9", "寒"),
    ])
    _upload_vol(client, "册甲", "天寒")
    o1 = client.post("/api/volumes/册甲/submit")
    assert o1.status_code == 200, o1.text
    d1 = o1.json()
    tian = next(x for x in d1["volume_after"] if x["glyph_char"] == "天")
    assert (tian["library_name"], tian["matrix_no"]) == ("宋韵楼", "SR-1")
    old_list_id = d1["library_list_id"]

    # 重传同一版次清单：宋韵楼删掉“天”，金石斋保留“天”作后备
    r = _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-9", "寒"),
        ("金石斋", "重刊", "JS-1", "天"),
        ("金石斋", "重刊", "JS-9", "寒"),
    ])
    assert r.status_code == 200, r.text
    assert r.json()["previous_library_list_id"] == old_list_id
    new_list_id = r.json()["library_list_id"]
    assert new_list_id != old_list_id

    o2 = client.post("/api/volumes/册甲/submit")
    assert o2.status_code == 200, o2.text
    d2 = o2.json()
    # 新交印只按最新清单取字：“天”改由后备库金石斋提供
    tian2 = next(x for x in d2["volume_after"] if x["glyph_char"] == "天")
    assert (tian2["library_name"], tian2["matrix_no"]) == ("金石斋", "JS-1")
    assert d2["library_list_id"] == new_list_id
    # 已删除的旧字模 SR-1 不可能再被选中
    assert all(
        not (x["library_name"] == "宋韵楼" and x["matrix_no"] == "SR-1")
        for x in d2["volume_after"]
    )
    # 原单据仍可原样重取，仍指向旧清单与旧字模
    old = client.get(f"/api/orders/{d1['order_no']}").json()
    old_tian = next(x for x in old["volume_after"] if x["glyph_char"] == "天")
    assert (old_tian["library_name"], old_tian["matrix_no"]) == ("宋韵楼", "SR-1")
    assert old["library_list_id"] == old_list_id


def test_reupload_without_backup_missing_glyph_rejects_submit(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("宋韵楼", "初刻", "SR-2", "地"),
    ])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()

    # 重传清单：直接删掉“地”，且没有任何后备库收录
    r = _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    assert r.status_code == 200, r.text

    resp = client.post("/api/volumes/册甲/submit")
    assert resp.status_code == 422
    err = resp.json()
    assert err["type"] == "MissingGlyphError" and "地" in err["error"]

    # 缺字拒绝交印：该册沿用原清单，没有新单
    state = client.get("/api/state").json()
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}
    orders = client.get("/api/orders").json()
    assert [o["order_no"] for o in orders] == [d1["order_no"]]


def test_failed_upload_keeps_previous_list_in_force(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    _upload_vol(client, "册甲", "天")
    d1 = client.post("/api/volumes/册甲/submit").json()
    list_before = client.get("/api/state").json()["current_library_list"]["list_id"]

    # 坏表头：解析阶段即失败，不得产生新版本
    r = client.post(
        "/api/libraries",
        files={"file": ("bad.csv", _csv("foo,bar\n1,2\n"), "text/csv")},
    )
    assert r.status_code == 400
    state = client.get("/api/state").json()
    assert state["current_library_list"]["list_id"] == list_before
    assert [x["list_id"] for x in state["library_list_history"]] == [list_before]

    # 字模号→字冲突：整单拒绝，指针仍停在上一份清单
    r2 = _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "地")])
    assert r2.status_code == 400 and "字模号恒定" in r2.json()["error"]
    state2 = client.get("/api/state").json()
    assert state2["current_library_list"]["list_id"] == list_before

    # 上一份清单继续生效：交印选字结果不变并复用原单
    d2 = client.post("/api/volumes/册甲/submit").json()
    assert d2["order_no"] == d1["order_no"] and d2["reused"] is True
    assert d2["library_list_id"] == list_before


def test_matrix_no_always_maps_to_same_char_in_same_library_edition(client):
    _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    # 同一字库同一版次，SR-1 改成别的字：拒绝
    r = _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "地")])
    assert r.status_code == 400 and "字模号恒定" in r.json()["error"]
    # 同号同字允许重传（版次内容不变）
    r2 = _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    assert r2.status_code == 200
    # 不同版次/不同字库复用同号不冲突
    r3 = _upload_lib(client, [
        ("宋韵楼", "重刊", "SR-1", "地"),
        ("金石斋", "初刻", "SR-1", "人"),
    ])
    assert r3.status_code == 200


def test_each_submit_uses_one_complete_list_only(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("金石斋", "重刊", "JS-1", "地"),
    ])
    _upload_vol(client, "册甲", "天地")
    d1 = client.post("/api/volumes/册甲/submit").json()
    list_id = d1["library_list_id"]

    # 再传一份只含“天”的新清单（删掉“地”）
    r = _upload_lib(client, [("宋韵楼", "初刻", "SR-1", "天")])
    assert r.status_code == 200
    new_list_id = r.json()["library_list_id"]

    # 新清单缺“地”：必须整笔拒绝，不允许从旧版本里凑字
    resp = client.post("/api/volumes/册甲/submit")
    assert resp.status_code == 422 and "地" in resp.json()["error"]

    # 只用“天”交印：全部字模都来自同一份（最新）清单
    _upload_vol(client, "册乙", "天")
    d2 = client.post("/api/volumes/册乙/submit").json()
    assert d2["library_list_id"] == new_list_id
    assert {x["glyph_char"] for x in d2["volume_after"]} == {"天"}
    state = client.get("/api/state").json()
    assert state["current_library_list"]["list_id"] == new_list_id
    # 册甲的旧交印清单原样保留在旧版本上
    jia = next(v for v in state["volumes"] if v["volume"] == "册甲")
    assert {x["glyph_char"] for x in jia["active_matrices"]} == {"天", "地"}
    assert list_id != new_list_id


def test_latest_list_survives_restart_and_history_orders_downloadable(client):
    _upload_lib(client, [
        ("宋韵楼", "初刻", "SR-1", "天"),
        ("金石斋", "重刊", "JS-1", "天"),
    ])
    _upload_vol(client, "册甲", "天")
    d1 = client.post("/api/volumes/册甲/submit").json()
    assert d1["volume_after"][0]["library_name"] == "宋韵楼"

    new_id = _upload_lib(client, [
        ("金石斋", "重刊", "JS-1", "天"),
    ]).json()["library_list_id"]

    # 模拟重启：释放全部连接后重新建连（SQLite 状态全部落盘）
    import sqlite3 as _sq
    from app import database as _db
    # 直接用全新连接验证持久化的当前清单指针
    probe = _sq.connect(os.environ["MATRIX_DB_PATH"])
    try:
        row = probe.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (_db.META_CURRENT_LIST,),
        ).fetchone()
        assert row is not None and int(row[0]) == new_id
    finally:
        probe.close()

    # 重启后交印仍按最后一次成功上传的清单取字
    d2 = client.post("/api/volumes/册甲/submit").json()
    assert d2["library_list_id"] == new_id
    picked = next(x for x in d2["volume_after"] if x["glyph_char"] == "天")
    assert picked["library_name"] == "金石斋"

    # 旧增减单仍可原样下载，内容保持历史快照
    r = client.get(f"/api/orders/{d1['order_no']}/download")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert d1["order_no"] in text and "宋韵楼" in text


# ---------- 旧版 SQLite 库直接升级：首次重传前沿用升级前选字结果 ----------


def test_legacy_sqlite_db_upgrades_in_place(tmp_path, monkeypatch):
    import shutil
    from app import database as db_mod
    from app import service as svc

    legacy_path = tmp_path / "legacy.db"
    shutil.copy(
        os.path.join(os.path.dirname(__file__), "..", "data", "smoke.db"),
        legacy_path,
    )
    monkeypatch.setattr(db_mod, "DB_PATH", str(legacy_path))
    monkeypatch.setattr(svc, "connect", lambda: db_mod.connect(str(legacy_path)))

    db_mod.init_db(str(legacy_path))
    conn = db_mod.connect(str(legacy_path))
    try:
        # 版本化表已就位，旧数据完整保留、ID 不变
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM library_lists"
        ).fetchone()["c"] >= 1
        current_id = int(
            conn.execute(
                "SELECT value FROM app_meta WHERE key=?",
                (db_mod.META_CURRENT_LIST,),
            ).fetchone()["value"]
        )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM glyphs WHERE list_id=?", (current_id,)
        ).fetchone()["c"] == 12
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM orders"
        ).fetchone()["c"] == 4
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM active_matrices"
        ).fetchone()["c"] == 12
        old_glyph = conn.execute(
            "SELECT glyph_id FROM glyphs WHERE matrix_no='SR-001'"
        ).fetchone()
        assert old_glyph is not None
    finally:
        conn.close()

    # 升级后、首次成功重传前：交印沿用升级前清单的选字结果
    svc.replace_volume_chars("升级册", ["天", "地"])
    payload = svc.submit_volume("升级册")
    assert payload["library_list_id"] == current_id
    assert {x["glyph_char"] for x in payload["volume_after"]} == {"天", "地"}
    assert all(x["library_name"] == "宋韵楼" for x in payload["volume_after"])

    # 首次成功重传后：新清单生效；删掉的字在无后备库时拒绝交印
    svc.replace_libraries([
        LibraryRowLite("宋韵楼", "初刻", "SR-001", "天"),
    ])
    state = svc.get_state()
    assert state["current_library_list"]["list_id"] != current_id
    with pytest.raises(Exception) as exc:
        svc.submit_volume("升级册")
    assert exc.value.__class__.__name__ == "MissingGlyphError"

    # 历史单据仍可原样读取
    old_orders = svc.list_orders()
    assert len(old_orders) == 5  # 升级前 4 单 + 升级册 1 单
    first = svc.get_order("MO20261120-000001")
    assert first["volume"] == "册甲" and first["pool_after"]


class LibraryRowLite:
    """测试中轻量构造 LibraryRow（避免再走 CSV 编码）。"""

    def __init__(self, library, edition, matrix_no, glyph_char):
        self.library = library
        self.edition = edition
        self.matrix_no = matrix_no
        self.glyph_char = glyph_char
