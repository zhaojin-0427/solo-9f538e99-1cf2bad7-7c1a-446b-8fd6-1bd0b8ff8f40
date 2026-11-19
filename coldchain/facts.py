"""Month fact intake: one CSV, validated strictly, frozen atomically."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3

from .db import transaction
from .errors import ConflictError, NotFoundError, ValidationError
from .timeutil import datetime_to_us, format_utc, month_of, now_str, parse_utc, validate_month

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50_000
MAX_ERRORS = 40

FIELD_ALIASES = {
    "sequence_no": (
        "流水号",
        "出库流水号",
        "sequence_no",
        "sequenceno",
        "sequence",
        "seq",
        "单号",
    ),
    "sku": ("sku", "商品编码", "商品编号", "sku编码", "货号", "物料编码"),
    "occurred_at": (
        "出库时刻",
        "出库时间",
        "时间",
        "occurred_at",
        "occurredat",
        "shipped_at",
        "shipment_time",
        "timestamp",
    ),
    "boxes": ("箱数", "boxes", "boxcount", "box_count", "数量", "件数"),
}

FIELD_LABELS = {
    "sequence_no": "流水号",
    "sku": "SKU",
    "occurred_at": "出库时刻",
    "boxes": "箱数",
}

_SKU_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
_SEQ_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_BOXES_RE = re.compile(r"^[0-9]{1,9}$")


def _norm_header(value):
    text = (value or "").strip().lstrip("﻿").lower()
    text = text.replace(" ", "").replace("_", "").replace("-", "")
    return text


def _decode(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("CSV 无法按 UTF-8 或 GB18030 解码，请另存为 UTF-8 后重试")


def unclosed_quote_line(text):
    """Return the 1-based line where an unterminated quoted field starts.

    ``csv.reader`` in its default mode silently lets an unclosed quote swallow
    every following line into one field, which can make a short month look
    complete. This scanner follows the same quoting rules (a quote only opens
    a field at the very start of that field; ``""`` is an escaped quote) and
    reports the opening line instead. Returns ``None`` when every quoted field
    is closed. Legitimate multi-line quoted fields are not flagged.
    """
    in_quotes = False
    at_field_start = True
    line = 1
    opened_at = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_quotes:
            if char == '"':
                if index + 1 < length and text[index + 1] == '"':
                    index += 2
                    continue
                in_quotes = False
                opened_at = None
            elif char == "\n":
                line += 1
            index += 1
            continue
        if char == '"':
            if at_field_start:
                in_quotes = True
                opened_at = line
            at_field_start = False
            index += 1
        elif char == ",":
            at_field_start = True
            index += 1
        elif char == "\r":
            # Outside quotes csv treats CR, CRLF and LF all as record ends.
            line += 1
            at_field_start = True
            index += 2 if index + 1 < length and text[index + 1] == "\n" else 1
        elif char == "\n":
            line += 1
            at_field_start = True
            index += 1
        else:
            at_field_start = False
            index += 1
    return opened_at if in_quotes else None


def _header_map(fieldnames):
    normalized = {}
    for index, name in enumerate(fieldnames):
        key = _norm_header(name)
        if key in normalized:
            raise ValidationError(f"CSV 表头重复：{name!r}")
        normalized[key] = (index, name)

    mapping = {}
    used = {}
    for field, aliases in FIELD_ALIASES.items():
        alias_set = {_norm_header(alias) for alias in aliases}
        matches = [(idx, name) for key, (idx, name) in normalized.items() if key in alias_set]
        if not matches:
            raise ValidationError(
                f"CSV 缺少必需列「{FIELD_LABELS[field]}」（可用列名：{' / '.join(aliases)}）"
            )
        if len(matches) > 1:
            names = "、".join(str(name) for _, name in matches)
            raise ValidationError(f"CSV 有多列都对应「{FIELD_LABELS[field]}」：{names}")
        idx, name = matches[0]
        if idx in used:
            raise ValidationError(f"表头列「{name}」被重复用于不同字段")
        used[idx] = field
        mapping[field] = idx

    if len(mapping) != 4:
        raise ValidationError("CSV 必须恰好包含流水号、SKU、出库时刻、箱数四列")
    return mapping


def parse_csv(raw, expected_month=None):
    """Parse and validate the entire file. Returns (month, rows, sha256).

    Nothing is written here; any error rejects the whole file so a month can
    never become partially frozen.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        raise ValidationError("上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValidationError("CSV 超过 8 MiB 上限")
    sha256 = hashlib.sha256(raw).hexdigest()
    text = _decode(raw)

    opened_at = unclosed_quote_line(text)
    if opened_at is not None:
        raise ValidationError(
            f"第 {opened_at} 行存在未闭合的引号：其后所有行都会被读成同一个字段，"
            "整份文件未写入。请补齐闭引号后重新上传；合法的跨行引号字段仍可正常上传。",
            details=[
                f"第 {opened_at} 行引号一直延伸到文件末尾仍未闭合",
                "请先修复备注等带引号字段的闭引号，再整份重新上传",
            ],
        )

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("CSV 为空，缺少表头") from None
    if not any(str(cell).strip() for cell in header):
        raise ValidationError("CSV 首行表头为空")

    mapping = _header_map(header)
    width = len(header)
    errors = []
    rows = []
    seen_sequences = {}
    months = set()

    for line_no, cells in enumerate(reader, start=2):
        if not any(str(cell).strip() for cell in cells):
            continue
        if len(cells) > width and any(str(c).strip() for c in cells[width:]):
            errors.append(f"第 {line_no} 行：数据列多于表头")
            if len(errors) >= MAX_ERRORS:
                break
            continue
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))

        def cell(field):
            return str(cells[mapping[field]]).strip()

        row_ok = True
        sequence_no = cell("sequence_no")
        sku = cell("sku")
        occurred_text = cell("occurred_at")
        boxes_text = cell("boxes")

        if not _SEQ_RE.match(sequence_no):
            errors.append(f"第 {line_no} 行：流水号为空或含控制字符或超过 128 字符")
            row_ok = False
        if not _SKU_RE.match(sku):
            errors.append(f"第 {line_no} 行：SKU 为空或含控制字符或超过 64 字符")
            row_ok = False
        if not _BOXES_RE.match(boxes_text):
            errors.append(f"第 {line_no} 行：箱数必须为正整数")
            row_ok = False
        elif int(boxes_text) <= 0:
            errors.append(f"第 {line_no} 行：箱数必须为正整数")
            row_ok = False

        occurred = None
        if not occurred_text:
            errors.append(f"第 {line_no} 行：出库时刻为空")
            row_ok = False
        else:
            try:
                occurred = parse_utc(occurred_text, field="出库时刻")
            except ValueError as exc:
                errors.append(f"第 {line_no} 行：{exc}")
                row_ok = False

        if row_ok:
            if sequence_no in seen_sequences:
                errors.append(
                    f"第 {line_no} 行：流水号 {sequence_no} 与第 {seen_sequences[sequence_no]} 行重复"
                )
                row_ok = False
            else:
                seen_sequences[sequence_no] = line_no
                months.add(month_of(occurred))

        if not row_ok:
            if len(errors) >= MAX_ERRORS:
                break
            continue

        rows.append(
            {
                "sequence_no": sequence_no,
                "sku": sku,
                "occurred_at": format_utc(occurred),
                "occurred_at_dt": occurred,
                "boxes": int(boxes_text),
                "source_row": line_no,
            }
        )
        if len(rows) > MAX_ROWS:
            raise ValidationError(f"单次上传最多 {MAX_ROWS} 行")

    if errors:
        more = "（仅显示前若干条）" if len(errors) >= MAX_ERRORS else ""
        raise ValidationError("CSV 校验未通过，未写入任何数据", details=errors + ([more] if more else []))
    if not rows:
        raise ValidationError("CSV 没有数据行")
    if len(months) != 1:
        raise ValidationError(
            "CSV 含多个 UTC 月份的出库记录：" + "、".join(sorted(months)) + "；请单月上传"
        )
    month = months.pop()
    if expected_month:
        try:
            expected = validate_month(expected_month)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if expected != month:
            raise ValidationError(
                f"指定月份 {expected} 与 CSV 中出库时刻所属 UTC 月份 {month} 不一致"
            )
    return month, rows, sha256


def _sequence_clashes(conn, sequences):
    """Return the first sequence number already used anywhere in the ledger."""
    for start in range(0, len(sequences), 400):
        chunk = sequences[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        clash = conn.execute(
            f"SELECT sequence_no FROM shipments WHERE sequence_no IN ({placeholders}) LIMIT 1",
            chunk,
        ).fetchone()
        if clash:
            return clash["sequence_no"]
        clash = conn.execute(
            "SELECT sequence_no FROM supplement_shipments"
            f" WHERE sequence_no IN ({placeholders}) LIMIT 1",
            chunk,
        ).fetchone()
        if clash:
            return clash["sequence_no"]
    return None


def ingest_month(conn, raw, filename, expected_month=None):
    """Validate then freeze one month. All-or-nothing."""
    month, rows, sha256 = parse_csv(raw, expected_month=expected_month)
    safe_name = (filename or "upload.csv").strip()[:255] or "upload.csv"
    total_boxes = sum(row["boxes"] for row in rows)

    with transaction(conn):
        exists = conn.execute(
            "SELECT month, frozen_at FROM months WHERE month = ?", (month,)
        ).fetchone()
        if exists:
            raise ConflictError(
                f"{month} 的事实已于 {exists['frozen_at']} 固定，不能重复上传或改写"
            )
        clash = _sequence_clashes(conn, [row["sequence_no"] for row in rows])
        if clash:
            raise ConflictError(
                f"流水号 {clash} 已存在于其他月份，流水号必须全局唯一"
            )
        frozen_at = now_str()
        try:
            conn.execute(
                "INSERT INTO months(month, source_filename, source_sha256, row_count, total_boxes, frozen_at)"
                " VALUES(?,?,?,?,?,?)",
                (month, safe_name, sha256, len(rows), total_boxes, frozen_at),
            )
            conn.executemany(
                "INSERT INTO shipments(month, sequence_no, sku, occurred_at, occurred_at_us, boxes, source_row)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        month,
                        row["sequence_no"],
                        row["sku"],
                        row["occurred_at"],
                        datetime_to_us(row["occurred_at_dt"]),
                        row["boxes"],
                        row["source_row"],
                    )
                    for row in rows
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"写入被拒绝：{exc}（未产生任何数据）") from exc
        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary) VALUES(?,?,?,?,?)",
            (frozen_at, "freeze_month", month, None, f"固定 {month} 共 {len(rows)} 行 / {total_boxes} 箱"),
        )
    return {
        "month": month,
        "row_count": len(rows),
        "total_boxes": total_boxes,
        "frozen_at": frozen_at,
        "sha256": sha256,
    }


def list_months(conn):
    return conn.execute(
        "SELECT m.month, m.row_count, m.total_boxes, m.frozen_at, m.source_filename,"
        "       s.id AS supplement_id, s.row_count AS supplement_row_count,"
        "       s.total_boxes AS supplement_total_boxes, s.created_at AS supplement_created_at"
        " FROM months m LEFT JOIN month_supplements s ON s.month = m.month"
        " ORDER BY m.month DESC"
    ).fetchall()


def get_supplement(conn, month):
    """Return the (at most one) supplement record for ``month`` or ``None``."""
    return conn.execute(
        "SELECT * FROM month_supplements WHERE month = ?", (month,)
    ).fetchone()


def _insert_only_quotes(bad_raw, corrected_raw):
    """Check that ``corrected_raw`` only adds closing quote bytes.

    Walks the corrected bytes alongside the bad file: every byte that is not
    carried over verbatim must be a ``"`` inserted while a quoted field is
    open, i.e. a closing quote. Returns the number of inserted quotes (0 means
    the files are byte-identical, which repairs nothing).
    """
    if len(corrected_raw) < len(bad_raw):
        return 0
    position = 0
    inserted = 0
    in_quotes = False
    at_field_start = True
    for byte in corrected_raw:
        if position < len(bad_raw) and byte == bad_raw[position]:
            match = byte
            position += 1
        elif byte == 0x22 and in_quotes:
            # A closing quote is the only byte the repair is allowed to add.
            inserted += 1
            in_quotes = False
            continue
        else:
            return 0
        if in_quotes:
            if match == 0x22:
                in_quotes = False
            continue
        if match == 0x22:
            if at_field_start:
                in_quotes = True
            at_field_start = False
        elif match == 0x2C:  # ','
            at_field_start = True
        elif match in (0x0A, 0x0D):
            at_field_start = True
        else:
            at_field_start = False
    if position != len(bad_raw):
        return 0
    return inserted


def supplement_month(
    conn,
    *,
    bad_raw,
    bad_filename,
    corrected_raw,
    corrected_filename,
    expected_month=None,
):
    """Repair a month that was frozen from a file with an unclosed quote.

    The bad file must be the exact file frozen for that month (same SHA-256)
    and must really contain an unclosed quote. The corrected file may only add
    closing quote bytes. Every frozen shipment's SKU / UTC instant / boxes must
    survive, and at least one previously swallowed shipment from the same
    month must be recovered. The frozen facts themselves are never touched:
    the repaired view lives in ``month_supplements`` and
    ``supplement_shipments`` and is selected explicitly when reporting.
    """
    if isinstance(bad_raw, str):
        bad_raw = bad_raw.encode("utf-8")
    if isinstance(corrected_raw, str):
        corrected_raw = corrected_raw.encode("utf-8")
    if not bad_raw or not corrected_raw:
        raise ValidationError("请同时提交坏文件和修正版")
    if len(bad_raw) > MAX_UPLOAD_BYTES or len(corrected_raw) > MAX_UPLOAD_BYTES:
        raise ValidationError("CSV 超过 8 MiB 上限")

    bad_text = _decode(bad_raw)
    opened_at = unclosed_quote_line(bad_text)
    if opened_at is None:
        raise ValidationError(
            "所提交的坏文件并没有未闭合的引号；普通上传已可正常接收，无需补齐"
        )

    month, rows, corrected_sha256 = parse_csv(corrected_raw, expected_month=expected_month)
    bad_sha256 = hashlib.sha256(bad_raw).hexdigest()

    with transaction(conn):
        month_row = conn.execute(
            "SELECT * FROM months WHERE month = ?", (month,)
        ).fetchone()
        if month_row is None:
            raise NotFoundError(f"月份 {month} 尚未冻结，不能补齐")
        if month_row["source_sha256"] != bad_sha256:
            raise ConflictError(
                "坏文件的 SHA-256 与该月冻结记录不一致；只有当时被冻结的原文件才能补齐"
            )
        existing = get_supplement(conn, month)
        if existing is not None:
            raise ConflictError(
                f"{month} 已于 {existing['created_at']} 成功补齐过一次，补齐版不可重复生成"
            )

        frozen_rows = conn.execute(
            "SELECT sequence_no, sku, occurred_at, boxes FROM shipments"
            " WHERE month = ? ORDER BY occurred_at_us, id",
            (month,),
        ).fetchall()
        if not frozen_rows:
            raise ConflictError(f"{month} 没有已冻结流水，无法核对修正版")

        corrected_by_seq = {row["sequence_no"]: row for row in rows}
        for frozen in frozen_rows:
            fixed = corrected_by_seq.get(frozen["sequence_no"])
            if fixed is None:
                raise ValidationError(
                    f"修正版缺少已冻结流水 {frozen['sequence_no']}；不得丢弃任何已冻结流水"
                )
            if (
                fixed["sku"] != frozen["sku"]
                or fixed["occurred_at"] != frozen["occurred_at"]
                or fixed["boxes"] != frozen["boxes"]
            ):
                raise ValidationError(
                    f"修正版改变了已冻结流水 {frozen['sequence_no']} 的 SKU、出库时刻或箱数"
                )

        frozen_sequences = {row["sequence_no"] for row in frozen_rows}
        added_rows = [row for row in rows if row["sequence_no"] not in frozen_sequences]
        if not added_rows:
            raise ValidationError(
                "修正版没有找回任何新的同月流水；若原文件本不缺少流水，则无需补齐"
            )

        if not _insert_only_quotes(bad_raw, corrected_raw):
            raise ValidationError(
                "修正版与坏文件不是同一份原始字节：修正版只能多出补上的闭引号，"
                "不得改写、增删或重新编码其他内容"
            )

        added_sequences = [row["sequence_no"] for row in added_rows]
        for start in range(0, len(added_sequences), 400):
            chunk = added_sequences[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            clash = conn.execute(
                "SELECT sequence_no FROM shipments"
                f" WHERE month != ? AND sequence_no IN ({placeholders}) LIMIT 1",
                [month, *chunk],
            ).fetchone()
            if clash:
                raise ConflictError(
                    f"流水号 {clash['sequence_no']} 已存在于其他月份，流水号必须全局唯一"
                )
            clash = conn.execute(
                "SELECT sequence_no FROM supplement_shipments"
                f" WHERE sequence_no IN ({placeholders}) LIMIT 1",
                chunk,
            ).fetchone()
            if clash:
                raise ConflictError(
                    f"流水号 {clash['sequence_no']} 已存在于其他补齐版，流水号必须全局唯一"
                )

        added_boxes = sum(row["boxes"] for row in added_rows)
        row_count = len(frozen_rows) + len(added_rows)
        total_boxes = month_row["total_boxes"] + added_boxes
        created_at = now_str()
        safe_bad = (bad_filename or "bad.csv").strip()[:255] or "bad.csv"
        safe_fixed = (corrected_filename or "corrected.csv").strip()[:255] or "corrected.csv"
        try:
            cursor = conn.execute(
                "INSERT INTO month_supplements("
                " month, bad_filename, bad_sha256, corrected_filename, corrected_sha256,"
                " row_count, total_boxes, added_count, added_boxes, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    month,
                    safe_bad,
                    bad_sha256,
                    safe_fixed,
                    corrected_sha256,
                    row_count,
                    total_boxes,
                    len(added_rows),
                    added_boxes,
                    created_at,
                ),
            )
            supplement_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO supplement_shipments("
                " supplement_id, month, sequence_no, sku, occurred_at, occurred_at_us,"
                " boxes, source_row) VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        supplement_id,
                        month,
                        row["sequence_no"],
                        row["sku"],
                        row["occurred_at"],
                        datetime_to_us(row["occurred_at_dt"]),
                        row["boxes"],
                        row["source_row"],
                    )
                    for row in added_rows
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"补齐写入被拒绝：{exc}（未产生任何新增流水）") from exc
        conn.execute(
            "INSERT INTO audit_log(at, action, month, version_no, summary) VALUES(?,?,?,?,?)",
            (
                created_at,
                "supplement_month",
                month,
                None,
                f"补齐 {month}：找回 {len(added_rows)} 行 / {added_boxes} 箱，"
                f"补齐后 {row_count} 行 / {total_boxes} 箱",
            ),
        )
    return {
        "month": month,
        "supplement_id": supplement_id,
        "frozen_row_count": len(frozen_rows),
        "added_count": len(added_rows),
        "added_boxes": added_boxes,
        "row_count": row_count,
        "total_boxes": total_boxes,
        "bad_sha256": bad_sha256,
        "corrected_sha256": corrected_sha256,
        "created_at": created_at,
    }
