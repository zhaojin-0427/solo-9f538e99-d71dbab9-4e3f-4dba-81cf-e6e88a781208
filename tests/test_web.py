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
