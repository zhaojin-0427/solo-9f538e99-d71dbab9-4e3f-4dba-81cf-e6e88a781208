"""领域逻辑：WebVTT、剪辑 CSV、时码、出现计算、校订继承、发布存档。

该模块不依赖 Flask，便于单独测试。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# 时码
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?\s*$")


class ParseError(ValueError):
    """上传文件格式错误。"""


class StaleDraft(RuntimeError):
    """草稿页已过期（剪辑表已更换或已发布）。"""


class ValidationError(ValueError):
    """提交的校订内容不合法。"""


class PublishBlocked(RuntimeError):
    """仍有剪口字幕未处理。"""

    def __init__(self, pending: list["Occurrence"]):
        super().__init__(f"还有 {len(pending)} 处剪口字幕未处理")
        self.pending = pending


def parse_timestamp(value: str) -> int:
    """把时码解析为毫秒。接受 HH:MM:SS.mmm、MM:SS.mmm、HH:MM:SS,mmm 与纯毫秒。"""
    if value is None:
        raise ParseError("时码不能为空")
    text = str(value).strip()
    if not text:
        raise ParseError("时码不能为空")
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = _TS_RE.match(text)
    if not match:
        raise ParseError(f"无法识别的时码：{value!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    if minutes > 59 or seconds > 59:
        raise ParseError(f"时码分秒超出范围：{value!r}")
    fraction = (match.group(4) or "0").ljust(3, "0")[:3]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction)


def format_timestamp(ms: int) -> str:
    """毫秒 -> WebVTT 时码 HH:MM:SS.mmm。"""
    if ms < 0:
        raise ParseError("时码不能为负")
    hours, rest = divmod(int(ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


# ---------------------------------------------------------------------------
# WebVTT
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cue:
    index: int
    identifier: str
    start_ms: int
    end_ms: int
    text: str
    settings: str = ""


@dataclass(frozen=True)
class Cut:
    """剪辑表中的一行：片段号 + 原片起止毫秒（左闭右开）。"""

    fragment_no: str
    start_ms: int
    end_ms: int
    line_no: int = 0


def _normalise_newlines(data: str) -> str:
    if data.startswith("﻿"):
        data = data[1:]
    return data.replace("\r\n", "\n").replace("\r", "\n")


def _clean_text_lines(lines: Iterable[str]) -> str:
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def parse_vtt(data: str) -> list[Cue]:
    """解析 WebVTT 原文。字幕正文按行保留，多行以换行连接。"""
    lines = _normalise_newlines(data).split("\n")
    if not lines or not lines[0].startswith("WEBVTT"):
        raise ParseError("不是 WebVTT 文件：缺少 WEBVTT 文件头")

    # 跳过 WEBVTT 行及其后的文件头元数据，直到空行。
    pos = 1
    while pos < len(lines) and lines[pos].strip() != "":
        pos += 1
    if pos < len(lines):
        pos += 1

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines[pos:]:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)

    cues: list[Cue] = []
    for block in blocks:
        first = block[0].strip()
        if first.startswith(("NOTE", "STYLE", "REGION")):
            continue
        cursor = 0
        identifier = ""
        if "-->" not in block[0]:
            identifier = first
            cursor = 1
            if cursor >= len(block):
                raise ParseError(f"字幕块缺少时间行：{identifier!r}")
        timing = block[cursor]
        if "-->" not in timing:
            raise ParseError(f"字幕块缺少时间行：{timing!r}")
        left, right = timing.split("-->", 1)
        right_parts = right.strip().split()
        if not right_parts:
            raise ParseError(f"字幕时间行不完整：{timing!r}")
        try:
            start_ms = parse_timestamp(left)
            end_ms = parse_timestamp(right_parts[0])
        except ParseError as exc:
            raise ParseError(f"字幕时间行无法解析：{timing!r}（{exc}）") from exc
        if end_ms <= start_ms:
            raise ParseError(f"字幕时长必须大于零：{timing!r}")
        cues.append(
            Cue(
                index=len(cues),
                identifier=identifier,
                start_ms=start_ms,
                end_ms=end_ms,
                text=_clean_text_lines(block[cursor + 1 :]),
                settings=" ".join(right_parts[1:]),
            )
        )

    if not cues:
        raise ParseError("WebVTT 中没有字幕")
    return cues


def build_vtt(entries: Iterable[tuple[int, int, list[str]]]) -> str:
    """把 (起, 止, 文本行) 拼成完整 WebVTT 文件。"""
    out = ["WEBVTT", ""]
    for start_ms, end_ms, text_lines in entries:
        out.append(f"{format_timestamp(start_ms)} --> {format_timestamp(end_ms)}")
        out.extend(text_lines)
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 剪辑 CSV
# ---------------------------------------------------------------------------

_FIELD_ALIASES = {
    "fragment_no": ("片段号", "片段编号", "片段", "fragment_no", "fragment", "clip", "id"),
    "start_ms": ("原片起毫秒", "原片起", "起始毫秒", "起点", "start_ms", "start"),
    "end_ms": ("原片止毫秒", "原片止", "结束毫秒", "终点", "end_ms", "end"),
}


def _header_key(name: str) -> str:
    base = re.sub(r"[\s_\-()（）:：]", "", name.strip().lower())
    for key, aliases in _FIELD_ALIASES.items():
        if base in {re.sub(r"[\s_\-()（）:：]", "", a.lower()) for a in aliases}:
            return key
    return base


def _looks_like_data_row(row: list[str]) -> bool:
    """首行不像表头时按无表头数据行处理（第二列可解析为时码即可）。"""
    if len(row) < 3 or not row[0].strip():
        return False
    try:
        parse_timestamp(row[1])
    except ParseError:
        return False
    return True


def parse_cuts_csv(data: str | bytes) -> list[Cut]:
    """解析剪辑表：每行 = 唯一片段号 + 原片起止毫秒，顺序即拼接顺序。

    同一原片时段可被多个片段号重复取用。无表头时按 片段号, 起, 止 读取。
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(_normalise_newlines(data)))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ParseError("剪辑表为空")

    header = [_header_key(cell) for cell in rows[0]]
    needed = ("fragment_no", "start_ms", "end_ms")
    if all(name in header for name in needed):
        columns = {name: header.index(name) for name in needed}
        data_rows = rows[1:]
        first_line_no = 2
    elif _looks_like_data_row(rows[0]):
        columns = {"fragment_no": 0, "start_ms": 1, "end_ms": 2}
        data_rows = rows
        first_line_no = 1
    else:
        missing = [name for name in needed if name not in header]
        raise ParseError(
            "剪辑表缺少列："
            + "、".join(missing)
            + "（需要 片段号、原片起毫秒、原片止毫秒）"
        )

    cuts: list[Cut] = []
    seen: set[str] = set()
    for offset, row in enumerate(data_rows):
        line_no = first_line_no + offset
        cells = [cell.strip() for cell in row]
        if len(cells) <= max(columns.values()):
            raise ParseError(f"第 {line_no} 行列数不足")
        fragment_no = cells[columns["fragment_no"]]
        if not fragment_no:
            raise ParseError(f"第 {line_no} 行缺少片段号")
        if fragment_no in seen:
            raise ParseError(f"第 {line_no} 行片段号重复：{fragment_no}")
        seen.add(fragment_no)
        try:
            start_ms = parse_timestamp(cells[columns["start_ms"]])
            end_ms = parse_timestamp(cells[columns["end_ms"]])
        except ParseError as exc:
            raise ParseError(f"第 {line_no} 行：{exc}") from exc
        if end_ms <= start_ms:
            raise ParseError(f"第 {line_no} 行：原片止毫秒须大于起毫秒")
        cuts.append(Cut(fragment_no, start_ms, end_ms, line_no))

    if not cuts:
        raise ParseError("剪辑表中没有片段")
    if sum(cut.end_ms - cut.start_ms for cut in cuts) <= 0:
        raise ParseError("剪辑总时长为零")
    return cuts


# ---------------------------------------------------------------------------
# 出现计算
# ---------------------------------------------------------------------------

WHOLE = "whole"  # 整条落在片段内：沿用正文
CUT = "cut"      # 被剪口截断：须逐次录入保留文字或标为删除


@dataclass
class Occurrence:
    cue_index: int
    fragment_no: str
    fragment_order: int
    ov_start: int   # 保留的原片起点
    ov_end: int     # 保留的原片终点（不含）
    out_start: int  # 节选（拼接后）起点
    out_end: int
    cut_start: bool
    cut_end: bool
    text: str       # 原字幕正文
    kind: str
    signature: str

    @property
    def original_range(self) -> tuple[int, int]:
        return self.ov_start, self.ov_end

    @property
    def excerpt_range(self) -> tuple[int, int]:
        return self.out_start, self.out_end


@dataclass
class Fragment:
    fragment_no: str
    order: int
    start_ms: int
    end_ms: int
    offset_ms: int
    occurrences: list[Occurrence] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def excerpt_start(self) -> int:
        return self.offset_ms

    @property
    def excerpt_end(self) -> int:
        return self.offset_ms + self.duration_ms


def make_signature(fragment_no: str, cue_text: str, ov_start: int, ov_end: int) -> str:
    """校订沿用键：只有片段号、原字幕、保留的原片范围都相同才一致。"""
    return json.dumps(
        [fragment_no, cue_text, ov_start, ov_end],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_occurrences(cuts: list[Cut], cues: list[Cue]) -> list[Fragment]:
    """按剪辑表行序拼接，展开每一次出现并计算节选时码。

    片段号在表内唯一，因此每个片段恰好出现一次；同一原片时段可被多个片段取用。
    """
    fragments: list[Fragment] = []
    offset = 0
    for order, cut in enumerate(cuts):
        fragments.append(
            Fragment(cut.fragment_no, order, cut.start_ms, cut.end_ms, offset)
        )
        offset += cut.end_ms - cut.start_ms

    ordered_cues = sorted(cues, key=lambda cue: (cue.start_ms, cue.index))
    for fragment in fragments:
        for cue in ordered_cues:
            ov_start = max(cue.start_ms, fragment.start_ms)
            ov_end = min(cue.end_ms, fragment.end_ms)
            if ov_end <= ov_start:
                continue
            cut_start = cue.start_ms < fragment.start_ms
            cut_end = cue.end_ms > fragment.end_ms
            fragment.occurrences.append(
                Occurrence(
                    cue_index=cue.index,
                    fragment_no=fragment.fragment_no,
                    fragment_order=fragment.order,
                    ov_start=ov_start,
                    ov_end=ov_end,
                    out_start=fragment.offset_ms + (ov_start - fragment.start_ms),
                    out_end=fragment.offset_ms + (ov_end - fragment.start_ms),
                    cut_start=cut_start,
                    cut_end=cut_end,
                    text=cue.text,
                    kind=CUT if (cut_start or cut_end) else WHOLE,
                    signature=make_signature(
                        fragment.fragment_no, cue.text, ov_start, ov_end
                    ),
                )
            )
    return fragments


def flatten(fragments: list[Fragment]) -> list[Occurrence]:
    return [occ for fragment in fragments for occ in fragment.occurrences]


def _text_lines(text: str) -> Optional[list[str]]:
    lines = [line.rstrip() for line in _normalise_newlines(text).split("\n")]
    lines = [line for line in lines if line.strip()]
    return lines or None


def decision_text(occ: Occurrence, decisions: dict[str, dict]) -> Optional[list[str]]:
    """某个出现应输出的文本行；None 表示未处理或已删除。"""
    if occ.kind == WHOLE:
        return _text_lines(occ.text)
    decision = decisions.get(occ.signature)
    if not decision or decision["status"] != "keep":
        return None
    return _text_lines(decision["text"])


def is_pending(occ: Occurrence, decisions: dict[str, dict]) -> bool:
    if occ.kind == WHOLE:
        return False
    decision = decisions.get(occ.signature)
    if decision is None:
        return True
    return decision["status"] == "keep" and not decision["text"].strip()


def pending_occurrences(
    fragments: list[Fragment], decisions: dict[str, dict]
) -> list[Occurrence]:
    return [occ for occ in flatten(fragments) if is_pending(occ, decisions)]


def build_output(fragments: list[Fragment], decisions: dict[str, dict]) -> str:
    entries: list[tuple[int, int, list[str]]] = []
    for occ in flatten(fragments):
        lines = decision_text(occ, decisions)
        if not lines:
            continue
        entries.append((occ.out_start, occ.out_end, lines))
    return build_vtt(entries)


def inherited_decisions(
    fragments: list[Fragment], previous: dict[str, dict]
) -> dict[str, dict]:
    """只沿用与当前出现签名（片段号 + 原字幕 + 原片范围）完全一致的校订。"""
    wanted = {occ.signature for occ in flatten(fragments) if occ.kind == CUT}
    return {sig: previous[sig] for sig in wanted if sig in previous}


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    lock_version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO project (id, lock_version) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS draft (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision INTEGER NOT NULL,
    vtt_raw TEXT NOT NULL,
    cuts_json TEXT NOT NULL,
    vtt_filename TEXT NOT NULL DEFAULT '',
    csv_filename TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    published_version INTEGER
);

CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES draft(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('keep', 'delete')),
    text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE (draft_id, signature)
);

CREATE TABLE IF NOT EXISTS edition (
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


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def lock_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT lock_version FROM project WHERE id = 1").fetchone()[0]


def _parse_cuts(cuts_json: str) -> list[Cut]:
    return [Cut(**item) for item in json.loads(cuts_json)]


def decision_rows(conn: sqlite3.Connection, draft_id: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT signature, status, text FROM decision WHERE draft_id = ?", (draft_id,)
    ):
        out[row["signature"]] = {"status": row["status"], "text": row["text"]}
    return out


def _insert_decisions(
    conn: sqlite3.Connection, draft_id: int, decisions: dict[str, dict]
) -> None:
    now = _utc_now()
    for signature, decision in decisions.items():
        conn.execute(
            """
            INSERT INTO decision (draft_id, signature, status, text, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (draft_id, signature) DO UPDATE SET
                status = excluded.status,
                text = excluded.text,
                updated_at = excluded.updated_at
            """,
            (draft_id, signature, decision["status"], decision.get("text", ""), now),
        )


def create_draft(
    conn: sqlite3.Connection,
    vtt_raw: str,
    cuts: list[Cut],
    cues: list[Cue],
    *,
    vtt_filename: str = "",
    csv_filename: str = "",
) -> int:
    """建立新草稿：锁版本递增（旧草稿页随之过期），并沿用上一版可匹配的校订。"""
    fragments = compute_occurrences(cuts, cues)
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = conn.execute(
            "SELECT id FROM draft ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous_decisions = (
            decision_rows(conn, previous["id"]) if previous is not None else {}
        )
        conn.execute("UPDATE project SET lock_version = lock_version + 1 WHERE id = 1")
        revision = lock_version(conn)
        cursor = conn.execute(
            """
            INSERT INTO draft
                (revision, vtt_raw, cuts_json, vtt_filename, csv_filename, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision,
                vtt_raw,
                json.dumps([cut.__dict__ for cut in cuts], ensure_ascii=False),
                vtt_filename,
                csv_filename,
                _utc_now(),
            ),
        )
        draft_id = cursor.lastrowid
        _insert_decisions(
            conn, draft_id, inherited_decisions(fragments, previous_decisions)
        )
        conn.execute("COMMIT")
        return draft_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def load_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM draft WHERE id = ?", (draft_id,)).fetchone()


def load_bundle(conn: sqlite3.Connection, draft_id: int) -> Optional[dict]:
    row = load_draft(conn, draft_id)
    if row is None:
        return None
    cues = parse_vtt(row["vtt_raw"])
    cuts = _parse_cuts(row["cuts_json"])
    fragments = compute_occurrences(cuts, cues)
    current_lock = lock_version(conn)
    return {
        "row": row,
        "cues": cues,
        "cuts": cuts,
        "fragments": fragments,
        "occurrences": flatten(fragments),
        "decisions": decision_rows(conn, draft_id),
        "lock_version": current_lock,
        "stale": row["published_version"] is not None
        or row["revision"] != current_lock,    }


def _reject_if_stale(
    conn: sqlite3.Connection, draft_id: int, expected_revision: int
) -> sqlite3.Row:
    revision = lock_version(conn)
    row = load_draft(conn, draft_id)
    if (
        revision != expected_revision
        or row is None
        or row["revision"] != expected_revision
        or row["published_version"] is not None
    ):
        raise StaleDraft("校订页已过期，请刷新后重新提交")
    return row


def save_decisions(
    conn: sqlite3.Connection,
    draft_id: int,
    expected_revision: int,
    changes: list[dict],
) -> None:
    """保存校订；草稿页过期时拒绝。changes: [{signature, status, text}]。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _reject_if_stale(conn, draft_id, expected_revision)
        fragments = compute_occurrences(
            _parse_cuts(row["cuts_json"]), parse_vtt(row["vtt_raw"])
        )
        valid = {occ.signature for occ in flatten(fragments) if occ.kind == CUT}
        now = _utc_now()
        for change in changes:
            signature = change.get("signature", "")
            if signature not in valid:
                raise ValidationError("提交的校订不属于当前剪辑表")
            status = change.get("status")
            if status not in ("keep", "delete"):
                raise ValidationError("请选择保留文字或标为删除")
            text = _normalise_newlines(change.get("text") or "").strip("\n")
            if status == "keep" and not text.strip():
                # 空保留文字按尚未处理处理。
                conn.execute(
                    "DELETE FROM decision WHERE draft_id = ? AND signature = ?",
                    (draft_id, signature),
                )
                continue
            conn.execute(
                """
                INSERT INTO decision (draft_id, signature, status, text, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (draft_id, signature) DO UPDATE SET
                    status = excluded.status,
                    text = excluded.text,
                    updated_at = excluded.updated_at
                """,
                (
                    draft_id,
                    signature,
                    status,
                    text if status == "keep" else "",
                    now,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def active_draft(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    row = conn.execute("SELECT * FROM draft ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row["published_version"] is not None:
        return None
    return row


def list_editions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT version_no, vtt_sha256, published_at, total_ms, cue_count "
            "FROM edition ORDER BY version_no DESC"
        )
    )


def get_edition(conn: sqlite3.Connection, version_no: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM edition WHERE version_no = ?", (version_no,)
    ).fetchone()


def publish(conn: sqlite3.Connection, draft_id: int, expected_revision: int) -> int:
    """发布：所有剪口处理完毕才写入新版；任何失败都不留新版本。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _reject_if_stale(conn, draft_id, expected_revision)
        fragments = compute_occurrences(
            _parse_cuts(row["cuts_json"]), parse_vtt(row["vtt_raw"])
        )
        decisions = decision_rows(conn, draft_id)
        pending = pending_occurrences(fragments, decisions)
        if pending:
            raise PublishBlocked(pending)
        vtt_text = build_output(fragments, decisions)
        blob = vtt_text.encode("utf-8")
        latest = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS v FROM edition"
        ).fetchone()["v"]
        version_no = latest + 1
        total_ms = sum(fragment.duration_ms for fragment in fragments)
        cue_count = sum(1 for line in vtt_text.splitlines() if "-->" in line)
        conn.execute(
            """
            INSERT INTO edition
                (version_no, vtt_sha256, published_at, total_ms, cue_count,
                 source_draft_id, vtt_blob)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_no,
                hashlib.sha256(blob).hexdigest(),
                _utc_now(),
                total_ms,
                cue_count,
                draft_id,
                blob,
            ),
        )
        conn.execute(
            "UPDATE draft SET published_version = ? WHERE id = ?",
            (version_no, draft_id),
        )
        # 发布后旧页面立即过期，避免重复发布。
        conn.execute("UPDATE project SET lock_version = lock_version + 1 WHERE id = 1")
        conn.execute("COMMIT")
        return version_no
    except Exception:
        conn.execute("ROLLBACK")
        raise
