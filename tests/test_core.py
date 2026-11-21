"""领域逻辑单元测试：时码、WebVTT、剪辑表、出现计算、校订继承。"""
from __future__ import annotations

import pytest

from app import core


def cues(spec):
    return [
        core.Cue(index=i, identifier="", start_ms=s, end_ms=e, text=t)
        for i, (s, e, t) in enumerate(spec)
    ]


def cuts(spec):
    return [
        core.Cut(fragment_no=no, start_ms=s, end_ms=e, line_no=i + 2)
        for i, (no, s, e) in enumerate(spec)
    ]


class TestTimestamps:
    @pytest.mark.parametrize(
        "text,ms",
        [
            ("00:00:01.000", 1000),
            ("00:00:01.5", 1500),
            ("00:01:02,250", 62250),
            ("01:02:03.004", 3723004),
            ("02:03.456", 123456),
            ("1234", 1234),
        ],
    )
    def test_parse(self, text, ms):
        assert core.parse_timestamp(text) == ms

    def test_format_roundtrip(self):
        for ms in (0, 1, 999, 1000, 61_001, 3_723_004):
            assert core.parse_timestamp(core.format_timestamp(ms)) == ms

    def test_format_pads(self):
        assert core.format_timestamp(5) == "00:00:00.005"
        assert core.format_timestamp(3_723_004) == "01:02:03.004"

    @pytest.mark.parametrize("bad", ["", "abc", "00:99:00.000", "00:00:60.000", "::"])
    def test_rejects_bad(self, bad):
        with pytest.raises(core.ParseError):
            core.parse_timestamp(bad)


class TestVtt:
    def test_parses_blocks_and_multiline(self):
        raw = (
            "WEBVTT\n\nNOTE comment\n\nintro\n00:00:01.000 --> 00:00:03.000 align:start\n"
            "第一行\n第二行\n\n00:00:03.000 --> 00:00:05.000\n\n"
        )
        parsed = core.parse_vtt(raw)
        assert len(parsed) == 2
        assert parsed[0].identifier == "intro"
        assert parsed[0].text == "第一行\n第二行"
        assert parsed[0].start_ms == 1000

    def test_rejects_non_vtt(self):
        with pytest.raises(core.ParseError):
            core.parse_vtt("1\n00:00:01.000 --> 00:00:02.000\nhi\n")

    def test_rejects_empty(self):
        with pytest.raises(core.ParseError):
            core.parse_vtt("WEBVTT\n\n")

    def test_build_output_is_valid_vtt(self):
        body = core.build_vtt([(0, 1500, ["甲"]), (1500, 3000, ["乙", "丙"])])
        assert body.startswith("WEBVTT\n")
        assert "00:00:00.000 --> 00:00:01.500\n甲" in body
        assert core.parse_vtt(body)[1].text == "乙\n丙"


class TestCutCsv:
    def test_header_and_order(self):
        parsed = core.parse_cuts_csv(
            "片段号,原片起毫秒,原片止毫秒\nB,00:00:02.000,00:00:04.000\nA,1000,2000\n"
        )
        assert [c.fragment_no for c in parsed] == ["B", "A"]
        assert parsed[0].start_ms == 2000

    def test_headerless(self):
        parsed = core.parse_cuts_csv("A,1000,2000\nB,00:00:01.000,00:00:03.000\n")
        assert [c.fragment_no for c in parsed] == ["A", "B"]

    def test_duplicate_fragment_no_rejected(self):
        with pytest.raises(core.ParseError, match="重复"):
            core.parse_cuts_csv("A,0,1000\nA,1000,2000\n")

    def test_duplicate_time_span_allowed(self):
        parsed = core.parse_cuts_csv("A,0,1000\nB,0,1000\n")
        assert len(parsed) == 2

    def test_end_not_after_start_rejected(self):
        with pytest.raises(core.ParseError, match="大于"):
            core.parse_cuts_csv("A,1000,1000\n")

    def test_zero_span_row_rejected(self):
        with pytest.raises(core.ParseError):
            core.parse_cuts_csv("A,1000,2000\nB,2000,2000\n")

    def test_bad_header_rejected(self):
        with pytest.raises(core.ParseError, match="缺少列"):
            core.parse_cuts_csv("x,y,z\n1,2,3\n")


class TestOccurrences:
    def test_offsets_follow_row_order(self):
        frags = core.compute_occurrences(
            cuts([("B", 2000, 4000), ("A", 1000, 3000)]),
            cues([(0, 5000, "一"), (5000, 8000, "二")]),
        )
        assert [f.fragment_no for f in frags] == ["B", "A"]
        assert frags[0].excerpt_start == 0
        assert frags[1].excerpt_start == 2000
        b = frags[0].occurrences[0]
        # 原片 2000-4000 整段落在 B 内，节选 0-2000
        assert (b.out_start, b.out_end) == (0, 2000)
        a = frags[1].occurrences
        # A 取原片 1000-3000，其中 2000-3000 是第二条字幕的前半段
        assert [(o.out_start, o.out_end) for o in a] == [(2000, 4000)]

    def test_same_span_taken_by_two_fragments(self):
        frags = core.compute_occurrences(
            cuts([("A", 0, 3000), ("B", 0, 3000)]),
            cues([(1000, 2000, "重复取用")]),
        )
        assert [len(f.occurrences) for f in frags] == [1, 1]
        first, second = frags[0].occurrences[0], frags[1].occurrences[0]
        assert (first.out_start, first.out_end) == (1000, 2000)
        assert (second.out_start, second.out_end) == (4000, 5000)
        # 片段号不同 => 校订签名不同，不能互相沿用
        assert first.signature != second.signature

    def test_whole_vs_cut(self):
        frags = core.compute_occurrences(
            cuts([("A", 0, 1000), ("B", 500, 2000)]),
            cues([(0, 1000, "整条"), (1000, 2000, "被截")]),
        )
        b = frags[1].occurrences
        # 第一条字幕从 B 的起点被截去一半
        assert b[0].kind == "cut"
        assert b[0].text == "整条"
        assert b[0].cut_start and not b[0].cut_end
        # 第二条字幕完整落在 B 内
        assert b[1].kind == "whole"
        assert b[1].text == "被截"

    def test_exact_boundary_is_whole(self):
        frags = core.compute_occurrences(
            cuts([("A", 1000, 2000)]), cues([(1000, 2000, "边界")])
        )
        occ = frags[0].occurrences[0]
        assert occ.kind == "whole" and not occ.cut_start and not occ.cut_end

    def test_cue_straddling_cut_edge(self):
        frags = core.compute_occurrences(
            cuts([("A", 1000, 2000)]), cues([(500, 2500, "跨两端")])
        )
        occ = frags[0].occurrences[0]
        assert occ.kind == "cut"
        assert occ.ov_start == 1000 and occ.ov_end == 2000
        assert (occ.out_start, occ.out_end) == (0, 1000)


class TestDecisions:
    def setup_method(self):
        self.frags = core.compute_occurrences(
            cuts([("A", 0, 3000), ("B", 1000, 4000), ("A2", 0, 3000)]),
            cues([(0, 3000, "整条正文"), (3000, 5000, "被截的正文")]),
        )

    def test_whole_uses_original_text(self):
        decisions = {}
        out = core.build_output(self.frags, decisions)
        assert "整条正文" in out
        # 尚未处理的剪口不输出
        assert "被截的正文" not in out

    def test_pending_requires_each_occurrence(self):
        occs = core.flatten(self.frags)
        cut_occs = [o for o in occs if o.kind == "cut"]
        assert len(cut_occs) == 2  # B 中一条，A2 中一条
        decisions = {
            cut_occs[0].signature: {"status": "keep", "text": "保留文字"}
        }
        pending = core.pending_occurrences(self.frags, decisions)
        assert pending == [cut_occs[1]]

    def test_delete_omits(self):
        occ = [o for o in core.flatten(self.frags) if o.kind == "cut"][0]
        decisions = {occ.signature: {"status": "delete", "text": ""}}
        out = core.build_output(self.frags, decisions)
        assert "被截的正文" not in out

    def test_keep_empty_is_pending(self):
        occ = [o for o in core.flatten(self.frags) if o.kind == "cut"][0]
        decisions = {occ.signature: {"status": "keep", "text": "   "}}
        assert core.is_pending(occ, decisions)


class TestInheritance:
    def test_signature_requires_all_three(self):
        base = core.make_signature("A", "原字幕", 1000, 2000)
        assert base != core.make_signature("B", "原字幕", 1000, 2000)
        assert base != core.make_signature("A", "另一条", 1000, 2000)
        assert base != core.make_signature("A", "原字幕", 1000, 2001)
        assert base == core.make_signature("A", "原字幕", 1000, 2000)

    def test_inherit_only_matching(self):
        frags = core.compute_occurrences(
            cuts([("A", 500, 2500), ("B", 500, 2500)]),
            cues([(0, 3000, "同一条字幕")]),
        )
        occs = core.flatten(frags)
        assert all(occ.kind == "cut" for occ in occs)
        previous = {occs[0].signature: {"status": "keep", "text": "校订一"}}
        inherited = core.inherited_decisions(frags, previous)
        assert inherited == {occs[0].signature: {"status": "keep", "text": "校订一"}}

    def test_changed_cut_range_loses_inheritance(self):
        old = core.compute_occurrences(
            cuts([("A", 0, 2000)]), cues([(500, 2500, "跨剪口")])
        )
        new = core.compute_occurrences(
            cuts([("A", 0, 2400)]), cues([(500, 2500, "跨剪口")])
        )
        old_occ = core.flatten(old)[0]
        new_occ = core.flatten(new)[0]
        previous = {old_occ.signature: {"status": "keep", "text": "旧校订"}}
        assert core.inherited_decisions(new, previous) == {}
        assert new_occ.signature != old_occ.signature
