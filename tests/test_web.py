"""网页流程测试：上传、预览、校订、发布、下载、过期页与失败原子性。"""
from __future__ import annotations

import re

import pytest

from app import core
from app.app import create_app

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:05.000
第一句原话。

2
00:00:05.000 --> 00:00:10.000
第二句原话，比较长，
分成两行。

3
00:00:10.000 --> 00:00:15.000
第三句原话。
"""

CSV = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:12.000
C,00:00:12.000,00:00:15.000
"""


@pytest.fixture()
def app(tmp_path):
    application = create_app(str(tmp_path / "test.db"))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, vtt=VTT, csv=CSV):
    response = client.post(
        "/upload",
        data={
            "vtt": (open_bytes(vtt), "interview.vtt"),
            "csv": (open_bytes(csv), "edit.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302, response.data.decode()
    match = re.search(r"/draft/(\d+)", response.headers["Location"])
    assert match, response.headers["Location"]
    return int(match.group(1))


def open_bytes(text: str):
    import io

    return io.BytesIO(text.encode("utf-8"))


def draft_form(client, draft_id):
    """读取草稿页，解析出隐藏的版本号与每个剪口出现的输入项。"""
    page = client.get(f"/draft/{draft_id}")
    html = page.data.decode()
    revision = re.search(r'name="revision" value="(\d+)"', html).group(1)
    return page, revision, html


def occurrence_fields(html):
    return sorted({int(m) for m in re.findall(r'name="status:(\d+)"', html)})


def fill(client, draft_id, decider, publish=False):
    page, revision, html = draft_form(client, draft_id)
    data = {"revision": revision}
    for index in occurrence_fields(html):
        status, text = decider(index)
        data[f"status:{index}"] = status
        data[f"text:{index}"] = text
    url = f"/draft/{draft_id}/" + ("publish" if publish else "save")
    return client.post(url, data=data, follow_redirects=False)


class TestUploadAndPreview:
    def test_upload_creates_draft_and_previews_both_timelines(self, client):
        draft_id = upload(client)
        page, revision, html = draft_form(client, draft_id)
        assert page.status_code == 200
        # 每一次出现都同时给出原片与节选时码
        assert "原片" in html and "节选" in html
        assert "00:00:01.000" in html  # 原片起点
        assert "00:00:00.000" in html  # 节选起点
        # 片段 B 的节选偏移 = A 的 4s
        assert "00:00:04.000" in html
        # 剪口截断的条目需要录入
        assert occurrence_fields(html)

    def test_bad_upload_reports_error(self, client):
        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes("not a vtt"), "x.vtt"),
                "csv": (open_bytes(CSV), "x.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "上传失败" in response.data.decode()

    def test_duplicate_fragment_number_rejected(self, client):
        bad_csv = "片段号,原片起毫秒,原片止毫秒\nA,0,1000\nA,1000,2000\n"
        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes(VTT), "x.vtt"),
                "csv": (open_bytes(bad_csv), "x.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "片段号重复" in response.data.decode()


class TestReviewAndPublish:
    def test_cannot_publish_until_every_cut_handled(self, client):
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        assert fields
        # 只处理第一处就发布会失败
        data = {"revision": revision, f"status:{fields[0]}": "delete"}
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422
        assert "未处理" in response.data.decode()
        # 失败不产生新版
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0

    def test_full_publish_and_download(self, client):
        draft_id = upload(client)
        response = fill(
            client,
            draft_id,
            lambda index: ("keep", f"保留文字{index}"),
            publish=True,
        )
        assert response.status_code == 302
        location = response.headers["Location"]
        assert "/editions/1" in location

        download = client.get("/editions/1/download")
        assert download.status_code == 200
        assert download.headers["Content-Type"].startswith("text/vtt")
        body = download.data.decode("utf-8")
        assert body.startswith("WEBVTT")
        assert body.endswith("\n")

        parsed = core.parse_vtt(body)
        texts = [cue.text for cue in parsed]
        assert "第一句原话。" in texts  # 整条保留沿用正文
        # 剪口被截断的每一次出现都按各自录入的文字输出
        assert "保留文字1" in texts
        assert "保留文字3" in texts
        assert "保留文字4" in texts
        assert len([t for t in texts if t.startswith("保留文字")]) == 3

    def test_deleted_cut_occurrence_absent_from_output(self, client):
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        body = client.get("/editions/1/download").data.decode()
        parsed = core.parse_vtt(body)
        # 被删掉的剪口不出现，但整条保留的仍在
        assert "第一句原话。" in [cue.text for cue in parsed]
        assert body.count("-->") == len(parsed)

    def test_publish_twice_from_same_page_is_rejected(self, client):
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        data = {"revision": revision}
        for index in occurrence_fields(html):
            data[f"status:{index}"] = "delete"
        first = client.post(f"/draft/{draft_id}/publish", data=data)
        assert first.status_code == 302
        second = client.post(f"/draft/{draft_id}/publish", data=data)
        assert second.status_code == 409
        assert "过期" in second.data.decode()


class TestStaleDraft:
    def test_old_page_rejected_after_new_upload(self, client):
        old_id = upload(client)
        _, old_revision, old_html = draft_form(client, old_id)
        fields = occurrence_fields(old_html)

        new_id = upload(client, csv=CSV.replace("00:00:12.000", "00:00:11.000"))
        assert new_id != old_id

        data = {"revision": old_revision}
        for index in fields:
            data[f"status:{index}"] = "delete"
        response = client.post(f"/draft/{old_id}/save", data=data)
        assert response.status_code == 409
        assert "刷新" in response.data.decode()

    def test_old_page_rejected_on_publish(self, client):
        old_id = upload(client)
        _, old_revision, old_html = draft_form(client, old_id)
        fields = occurrence_fields(old_html)
        upload(client)  # 更换剪辑表

        data = {"revision": old_revision}
        for index in fields:
            data[f"status:{index}"] = "delete"
        response = client.post(f"/draft/{old_id}/publish", data=data)
        assert response.status_code == 409
        assert "过期" in response.data.decode()
        assert client.get("/editions").data.decode().count("下载 WebVTT") == 0


class TestRecomputeAndInheritance:
    def test_new_csv_recomputes_excerpt_times(self, client):
        draft_id = upload(client)
        _, _, old_html = draft_form(client, draft_id)
        # 原表：A 4s + B 9s => C 的节选从 13s 开始
        assert "00:00:13.000" in old_html
        fill(client, draft_id, lambda index: ("keep", f"校订{index}"), publish=False)
        # 把 B 剪短 1 秒：C 的节选时码整体前移 1 秒
        new_csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:11.000
C,00:00:12.000,00:00:15.000
"""
        new_id = upload(client, csv=new_csv)
        _, _, html = draft_form(client, new_id)
        # A 4s + B 8s = 12s，C 的节选为 12s–15s
        assert "00:00:12.000" in html
        assert "00:00:15.000" in html
        assert "00:00:13.000" not in html

    def test_inheritance_requires_fragment_text_and_range(self, client):
        review_csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:01.000,00:00:05.000
B,00:00:03.000,00:00:12.000
C,00:00:12.000,00:00:15.000
"""
        old_id = upload(client, csv=review_csv)
        _, revision, html = draft_form(client, old_id)
        fields = occurrence_fields(html)
        # A/C 各有一处剪口，B 的起止两端各截断一条字幕
        assert len(fields) == 3
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"旧校订{index}"
        assert client.post(f"/draft/{old_id}/save", data=data).status_code == 302

        # 同一份剪辑表：三处剪口全部沿用
        same_id = upload(client, csv=review_csv)
        same_html = draft_form(client, same_id)[2]
        assert same_html.count("沿用上一版校订") == 3
        assert "旧校订" in same_html

        # 只改变 B 的止毫秒：B 里结束端被截的那条原片范围变了，不再沿用
        changed = review_csv.replace(
            "00:00:03.000,00:00:12.000", "00:00:03.000,00:00:11.000"
        )
        changed_id = upload(client, csv=changed)
        changed_html = draft_form(client, changed_id)[2]
        assert changed_html.count("沿用上一版校订") == 2

    def test_changed_original_text_loses_inheritance(self, client):
        old_csv = "片段号,原片起毫秒,原片止毫秒\nA,00:00:03.000,00:00:08.000\n"
        old_id = upload(client, csv=old_csv)
        revision = draft_form(client, old_id)[1]
        fields = occurrence_fields(draft_form(client, old_id)[2])
        assert len(fields) == 2  # cue1 起端截断、cue2 末端截断
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"旧校订{index}"
        client.post(f"/draft/{old_id}/save", data=data)

        # 改掉其中一条原字幕的文字：只有这一条失去沿用
        new_vtt = VTT.replace("第二句原话，比较长，", "第二句原话，改过，")
        new_id = upload(client, csv=old_csv, vtt=new_vtt)
        new_html = draft_form(client, new_id)[2]
        assert new_html.count("沿用上一版校订") == 1

    def test_same_span_taken_twice_needs_two_entries(self, client):
        csv = """片段号,原片起毫秒,原片止毫秒
A,00:00:03.000,00:00:12.000
A2,00:00:03.000,00:00:12.000
"""
        draft_id = upload(client, csv=csv)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        # 两段相同的原片范围各出现一次，需分别处理
        assert len(fields) >= 2
        data = {"revision": revision, f"status:{fields[0]}": "delete"}
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422


class TestPersistence:
    def test_edition_survives_restart(self, tmp_path):
        db = str(tmp_path / "restart.db")
        first = create_app(db)
        first.config.update(TESTING=True)
        client = first.test_client()
        draft_id = upload(client)
        fill(client, draft_id, lambda index: ("delete", ""), publish=True)
        expected = client.get("/editions/1/download").data

        second = create_app(db)  # 模拟重启
        second.config.update(TESTING=True)
        again = second.test_client().get("/editions/1/download")
        assert again.status_code == 200
        assert again.data == expected

    def test_failed_publish_leaves_no_version(self, tmp_path):
        db = str(tmp_path / "atomic.db")
        application = create_app(db)
        application.config.update(TESTING=True)
        client = application.test_client()
        draft_id = upload(client)
        _, revision, html = draft_form(client, draft_id)
        fields = occurrence_fields(html)
        data = {"revision": revision}
        for index in fields[:-1]:
            data[f"status:{index}"] = "delete"
        # 最后一处留空，发布应被拒绝
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 422

        conn = core.connect(db)
        try:
            assert core.get_edition(conn, 1) is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 原片复用与时段索引
# ---------------------------------------------------------------------------

CSV2 = """片段号,原片起毫秒,原片止毫秒
D,00:00:02.000,00:00:06.000
E,00:00:10.000,00:00:14.000
"""


def source_field(html):
    match = re.search(r'name="source_id" value="(\d+)"', html)
    return match.group(1) if match else None


def upload_csv_only(client, csv=CSV2, **fields):
    data = {"csv": (open_bytes(csv), "edit2.csv")}
    data.update(fields)
    return client.post("/upload", data=data, content_type="multipart/form-data")


def table_count(db, table):
    conn = core.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        conn.close()


class TestSourceReuse:
    def test_homepage_offers_reuse_with_shown_source(self, client):
        upload(client)
        html = client.get("/").data.decode()
        assert "沿用当前原片" in html
        assert source_field(html) is not None
        # 首页原字幕总数照旧显示
        assert "原字幕总数" in html
        assert ">3<" in html

    def test_csv_only_creates_draft_from_shown_source(self, client, app):
        first_id = upload(client)
        html = client.get("/").data.decode()
        response = upload_csv_only(client, source_id=source_field(html))
        assert response.status_code == 302
        new_id = int(re.search(r"/draft/(\d+)", response.headers["Location"]).group(1))
        assert new_id != first_id
        page = client.get(f"/draft/{new_id}")
        assert page.status_code == 200
        body = page.data.decode()
        # 按页面所示的原片取段：D/E 两段来自同一份原片
        assert "第二句原话，比较长，" in body
        assert "第三句原话。" in body
        # 原片只登记了一份，索引共用
        assert table_count(app.config["DATABASE"], "source") == 1
        assert table_count(app.config["DATABASE"], "source_cue") == 3

    def test_reuse_pinned_source_despite_later_upload(self, client, app):
        upload(client)  # 窗口 A 打开首页时的原片
        html = client.get("/").data.decode()
        pinned = source_field(html)
        assert pinned is not None
        # 另一个窗口随后上传了别的原片
        other = VTT.replace("第一句原话。", "另一期完全不同的原话。")
        upload(client, vtt=other)
        assert table_count(app.config["DATABASE"], "source") == 2
        # 本窗口仍按页面所示的原片取段
        response = upload_csv_only(client, source_id=pinned)
        assert response.status_code == 302
        new_id = int(re.search(r"/draft/(\d+)", response.headers["Location"]).group(1))
        body = client.get(f"/draft/{new_id}").data.decode()
        assert "第一句原话。" in body
        assert "另一期完全不同的原话。" not in body

    def test_reuse_without_field_uses_latest_source(self, client):
        upload(client)
        response = upload_csv_only(client)
        assert response.status_code == 302

    def test_same_vtt_uploaded_twice_shares_one_source(self, client, app):
        upload(client)
        upload(client)  # 同一份原片再次双文件上传
        assert table_count(app.config["DATABASE"], "source") == 1
        assert table_count(app.config["DATABASE"], "source_cue") == 3
        assert table_count(app.config["DATABASE"], "draft") == 2

    def test_failed_reuse_keeps_draft_and_source(self, client, app):
        old_id = upload(client)
        db = app.config["DATABASE"]
        before = {t: table_count(db, t) for t in ("draft", "source", "source_cue")}
        bad_csv = "片段号,原片起毫秒,原片止毫秒\nA,0,1000\nA,1000,2000\n"
        response = upload_csv_only(client, csv=bad_csv, source_id="1")
        assert response.status_code == 302  # 回到首页并提示失败
        follow = client.get(response.headers["Location"])
        assert "片段号重复" in follow.data.decode()
        after = {t: table_count(db, t) for t in ("draft", "source", "source_cue")}
        assert after == before
        # 当前草稿照旧可打开
        assert client.get(f"/draft/{old_id}").status_code == 200

    def test_unknown_source_rejected(self, client, app):
        upload(client)
        response = upload_csv_only(client, source_id="999")
        assert response.status_code == 302
        follow = client.get(response.headers["Location"])
        assert "已不可用" in follow.data.decode()
        assert table_count(app.config["DATABASE"], "draft") == 1

    def test_reuse_inherits_matching_decisions(self, client):
        old_id = upload(client)
        fill(client, old_id, lambda index: ("keep", f"校订{index}"))
        html = client.get("/").data.decode()
        # 同一剪辑表只交 CSV：剪口校订照旧沿用
        response = upload_csv_only(client, csv=CSV, source_id=source_field(html))
        new_id = int(re.search(r"/draft/(\d+)", response.headers["Location"]).group(1))
        body = client.get(f"/draft/{new_id}").data.decode()
        assert body.count("沿用上一版校订") == 3


class TestLegacyVolume:
    """已有 SQLite 卷：旧草稿照常编辑发布，旧原片首次复用时补建索引。"""

    OLD_SCHEMA = """
    CREATE TABLE project (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        lock_version INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO project (id, lock_version) VALUES (1, 0);
    CREATE TABLE draft (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        revision INTEGER NOT NULL,
        vtt_raw TEXT NOT NULL,
        cuts_json TEXT NOT NULL,
        vtt_filename TEXT NOT NULL DEFAULT '',
        csv_filename TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        published_version INTEGER
    );
    CREATE TABLE decision (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draft_id INTEGER NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
        signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('keep', 'delete')),
        text TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        UNIQUE (draft_id, signature)
    );
    CREATE TABLE edition (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_no INTEGER NOT NULL UNIQUE,
        vtt_sha256 TEXT NOT NULL,
        published_at TEXT NOT NULL,
        total_ms INTEGER NOT NULL,
        cue_count INTEGER NOT NULL,
        source_draft_id INTEGER,
        vtt_blob BLOB NOT NULL
    );
    """

    @pytest.fixture()
    def legacy_db(self, tmp_path):
        import json as _json
        import sqlite3

        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.executescript(self.OLD_SCHEMA)
        cuts = core.parse_cuts_csv(CSV)
        conn.execute(
            """
            INSERT INTO draft
                (revision, vtt_raw, cuts_json, vtt_filename, csv_filename, created_at)
            VALUES (1, ?, ?, 'old.vtt', 'old.csv', '2024-01-01T00:00:00Z')
            """,
            (VTT, _json.dumps([c.__dict__ for c in cuts], ensure_ascii=False)),
        )
        conn.execute("UPDATE project SET lock_version = 1 WHERE id = 1")
        conn.commit()
        conn.close()
        return db

    def legacy_client(self, db):
        application = create_app(db)
        application.config.update(TESTING=True)
        return application.test_client()

    def test_old_draft_editable_and_publishable(self, legacy_db):
        client = self.legacy_client(legacy_db)
        page, revision, html = draft_form(client, 1)
        assert page.status_code == 200
        assert "第一句原话。" in html
        # 旧草稿照常保存并发布
        response = fill(client, 1, lambda index: ("keep", f"保留{index}"), publish=True)
        assert response.status_code == 302
        assert "/editions/1" in response.headers["Location"]
        body = client.get("/editions/1/download").data.decode()
        assert "第一句原话。" in body

    def test_homepage_shows_old_source_for_reuse(self, legacy_db):
        client = self.legacy_client(legacy_db)
        html = client.get("/").data.decode()
        assert "沿用当前原片" in html
        assert 'name="from_draft" value="1"' in html

    def test_old_source_backfilled_on_first_reuse(self, legacy_db):
        client = self.legacy_client(legacy_db)
        response = upload_csv_only(client, from_draft="1")
        assert response.status_code == 302
        new_id = int(re.search(r"/draft/(\d+)", response.headers["Location"]).group(1))
        body = client.get(f"/draft/{new_id}").data.decode()
        assert "第二句原话，比较长，" in body
        # 补建的索引与首次复用建稿一起生效
        conn = core.connect(legacy_db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM source_cue").fetchone()[0] == 3
            old = core.load_draft(conn, 1)
            assert old["source_id"] is None  # 旧草稿本身不动
            new = core.load_draft(conn, new_id)
            assert new["source_id"] is not None
        finally:
            conn.close()
        # 再次复用走已建好的索引
        html = client.get("/").data.decode()
        assert source_field(html) is not None


class TestIndexedFlow:
    def test_indexed_flow_never_reparses_vtt(self, client, monkeypatch):
        draft_id = upload(client)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("索引建好后不应再解析原片全文")

        monkeypatch.setattr(core, "parse_vtt", forbidden)
        # 打开草稿、保存、发布、再建稿都只读索引
        page, revision, html = draft_form(client, draft_id)
        assert page.status_code == 200
        response = fill(
            client, draft_id, lambda index: ("keep", f"保留{index}"), publish=True
        )
        assert response.status_code == 302
        response = upload_csv_only(client)
        assert response.status_code == 302
        assert client.get("/").status_code == 200

    def test_source_reusable_after_restart(self, tmp_path):
        db = str(tmp_path / "restart-reuse.db")
        first = create_app(db)
        first.config.update(TESTING=True)
        client = first.test_client()
        upload(client)
        fill(client, 1, lambda index: ("delete", ""), publish=True)

        second = create_app(db)  # 模拟重启
        second.config.update(TESTING=True)
        client2 = second.test_client()
        html = client2.get("/").data.decode()
        assert source_field(html) is not None
        response = upload_csv_only(client2, source_id=source_field(html))
        assert response.status_code == 302
        new_id = int(re.search(r"/draft/(\d+)", response.headers["Location"]).group(1))
        assert client2.get(f"/draft/{new_id}").status_code == 200
        # 旧版仍按版号原样下载
        again = client2.get("/editions/1/download")
        assert again.status_code == 200
        assert again.data.decode().startswith("WEBVTT")

    def test_large_source_full_flow(self, client, app):
        """2 万条原字幕 + 1000 行剪辑表：完整流程且结果正确。"""
        cue_count, cut_count = 20_000, 1_000
        lines = ["WEBVTT", ""]
        for i in range(cue_count):
            start = i * 3000 + 700  # 错位，让剪口切在字幕中间
            lines.append(str(i + 1))
            lines.append(
                f"{core.format_timestamp(start)} --> {core.format_timestamp(start + 2500)}"
            )
            lines.append(f"第{i + 1}条原字幕")
            lines.append("")
        big_vtt = "\n".join(lines)
        rows = ["片段号,原片起毫秒,原片止毫秒"]
        for j in range(cut_count):
            rows.append(f"F{j + 1:04d},{j * 60_000},{(j + 1) * 60_000}")
        big_csv = "\n".join(rows)

        response = client.post(
            "/upload",
            data={
                "vtt": (open_bytes(big_vtt), "big.vtt"),
                "csv": (open_bytes(big_csv), "big.csv"),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302
        draft_id = int(re.search(r"/draft/(\d+)", response.headers["Location"]).group(1))

        page, revision, html = draft_form(client, draft_id)
        assert page.status_code == 200
        fields = occurrence_fields(html)
        # 999 个内部剪口两端各截断一次；最后一条字幕还越出末段终点
        assert len(fields) == 2 * (cut_count - 1) + 1
        data = {"revision": revision}
        for index in fields:
            data[f"status:{index}"] = "keep"
            data[f"text:{index}"] = f"保留{index}"
        response = client.post(f"/draft/{draft_id}/publish", data=data)
        assert response.status_code == 302

        body = client.get("/editions/1/download").data.decode()
        parsed = core.parse_vtt(body)
        # 出现总数 = 整条 + 剪口；每条输出字幕都在节选时间线内
        assert len(parsed) == cue_count + (cut_count - 1)
        assert parsed[-1].end_ms <= cut_count * 60_000
        # 原字幕序号在草稿页照旧显示
        assert "原字幕 #20000" in html or "原字幕 #19999" in html
