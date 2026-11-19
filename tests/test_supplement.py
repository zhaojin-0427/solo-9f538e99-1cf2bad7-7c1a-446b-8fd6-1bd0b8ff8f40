"""未闭合引号导致少行冻结，以及补齐版（supplement）的端到端行为。"""

from __future__ import annotations

import io
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from coldchain import facts
from coldchain.app import create_app
from coldchain.db import connect
from coldchain.timeutil import datetime_to_us, now_str

# A month file where OUT-2's note opens a quote that is never closed:
# OUT-3 / OUT-4 are swallowed into that note and vanish from the frozen month.
HEADER = "流水号,SKU,出库时刻,箱数,备注"
FROZEN = (
    ("OUT-1", "SKU-A", "2026-03-01T01:00:00Z", 12),
    ("OUT-2", "SKU-B", "2026-03-05T23:30:00Z", 7),
)


def bad_text():
    return (
        f"{HEADER}\n"
        "OUT-1,SKU-A,2026-03-01T01:00:00Z,12,ok\n"
        'OUT-2,SKU-B,2026-03-05T23:30:00Z,7,"refrigerated\n'
        "OUT-3,SKU-A,2026-03-10T00:00:00Z,5,ok\n"
        "OUT-4,SKU-C,2026-03-31T23:59:59Z,2,ok\n"
    )


def fixed_text():
    return bad_text().replace('"refrigerated\n', '"refrigerated"\n', 1)


def bad_pair(month="2026-03", frozen_prefix="", recovered_prefix=""):
    """Build a (bad, fixed) pair for an arbitrary month.

    The frozen part always uses the same values as ``FROZEN``; only the
    sequence-number prefix and month change so several months can coexist.
    """
    frozen = [
        (
            frozen_prefix + seq,
            sku,
            at.replace("2026-03", month),
            boxes,
        )
        for seq, sku, at, boxes in FROZEN
    ]
    recovered = [
        (recovered_prefix + "OUT-3", "SKU-A", f"{month}-10T00:00:00Z", 5),
        (recovered_prefix + "OUT-4", "SKU-C", f"{month}-20T23:59:59Z", 2),
    ]
    lines = [HEADER]
    lines.extend(f"{seq},{sku},{at},{boxes},ok" for seq, sku, at, boxes in frozen)
    seq, sku, at, boxes = recovered[0]
    lines.append(f'{seq},{sku},{at},{boxes},"refrigerated')
    for seq, sku, at, boxes in recovered[1:]:
        lines.append(f"{seq},{sku},{at},{boxes},ok")
    bad = "\n".join(lines) + "\n"
    return bad, bad.replace('"refrigerated\n', '"refrigerated"\n', 1)


def multiline_ok_text():
    return (
        "流水号,SKU,出库时刻,箱数,备注\n"
        "OUT-1,SKU-A,2026-03-01T01:00:00Z,12,\"line one\nline two\"\n"
        "OUT-2,SKU-B,2026-03-05T23:30:00Z,7,ok\n"
    )


def freeze_legacy_bad(conn, text, filename="bad.csv"):
    """Reproduce the old defect: freeze the file with the quote guard bypassed."""
    with mock.patch.object(facts, "unclosed_quote_line", return_value=None):
        month, rows, sha256 = facts.parse_csv(text)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO months(month, source_filename, source_sha256, row_count, total_boxes, frozen_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                month,
                filename,
                sha256,
                len(rows),
                sum(row["boxes"] for row in rows),
                now_str(),
            ),
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
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return month


class QuoteValidationTests(unittest.TestCase):
    def test_unclosed_quote_rejected_by_parser(self):
        with self.assertRaises(facts.ValidationError) as raised:
            facts.parse_csv(bad_text())
        self.assertIn("未闭合的引号", raised.exception.message)

    def test_unclosed_quote_swallows_rows_without_strict_check(self):
        # Documents why the check exists: the default reader silently merges.
        import csv

        rows = list(csv.reader(io.StringIO(bad_text(), newline="")))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2][4].count("OUT-3"), 1)

    def test_legitimate_multiline_note_accepted(self):
        month, rows, _ = facts.parse_csv(multiline_ok_text())
        self.assertEqual(month, "2026-03")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["boxes"], 12)

    def test_escaped_quotes_accepted(self):
        text = (
            "流水号,SKU,出库时刻,箱数,备注\n"
            'OUT-1,SKU-A,2026-03-01T01:00:00Z,12,"say ""hi"""\n'
        )
        _, rows, _ = facts.parse_csv(text)
        self.assertEqual(len(rows), 1)

    def test_quote_mid_field_is_data(self):
        text = (
            "流水号,SKU,出库时刻,箱数,备注\n"
            'OUT-1,SKU-A,2026-03-01T01:00:00Z,12,x"y\n'
        )
        _, rows, _ = facts.parse_csv(text)
        self.assertEqual(len(rows), 1)

    def test_line_reported_points_at_opening_quote(self):
        self.assertEqual(facts.unclosed_quote_line(bad_text()), 3)
        self.assertIsNone(facts.unclosed_quote_line(multiline_ok_text()))


class UploadRejectsUnclosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "q.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, text, filename="m.csv"):
        return self.client.post(
            "/api/months",
            data={"file": (io.BytesIO(text.encode()), filename)},
            content_type="multipart/form-data",
        )

    def test_api_and_page_reject_unclosed_quote_without_freezing(self):
        response = self.post(bad_text())
        self.assertEqual(response.status_code, 400)
        self.assertIn("未闭合的引号", response.get_json()["message"])
        page = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(bad_text().encode()), "m.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(page.status_code, 400)
        self.assertEqual(self.client.get("/api/months").get_json()["months"], [])
        # The fixed file uploads normally.
        self.assertEqual(self.post(fixed_text()).status_code, 201)

    def test_multiline_file_uploads_normally(self):
        response = self.post(multiline_ok_text())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["row_count"], 2)


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "s.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        self.client = self.app.test_client()
        # Freeze 2026-03 from the bad file by parsing it without the quote
        # guard, exactly reproducing the historical defect: 2 rows / 19 boxes.
        conn = connect(self.db_path)
        try:
            freeze_legacy_bad(conn, bad_text())
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, sku, zone, base, start="2026-03-01T00:00:00Z"):
        return self.client.post(
            "/api/versions",
            json={
                "sku": sku,
                "reason": f"{sku} {zone}",
                "base_version": base,
                "intervals": [{"zone": zone, "valid_from": start}],
            },
        )

    def supplement(self, bad=None, fixed=None, month=None):
        data = {
            "bad_file": (
                io.BytesIO((bad if bad is not None else bad_text()).encode()),
                "bad.csv",
            ),
            "corrected_file": (
                io.BytesIO((fixed if fixed is not None else fixed_text()).encode()),
                "fixed.csv",
            ),
        }
        if month:
            data["month"] = month
        return self.client.post(
            "/api/months", data=data, content_type="multipart/form-data"
        )

    def report(self, supplement=False, version=None):
        query = "/api/report?month=2026-03"
        if version is not None:
            query += f"&version={version}"
        if supplement:
            query += "&supplement=1"
        return self.client.get(query)

    def month_row(self):
        conn = connect(self.db_path)
        try:
            return conn.execute(
                "SELECT * FROM months WHERE month = '2026-03'"
            ).fetchone()
        finally:
            conn.close()

    def test_upload_page_form_supplements_and_redirects_to_supplement_view(self):
        response = self.client.post(
            "/upload",
            data={
                "bad_file": (io.BytesIO(bad_text().encode()), "bad.csv"),
                "corrected_file": (io.BytesIO(fixed_text().encode()), "fixed.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("已补齐", text)
        self.assertIn("补齐版", text)
        self.assertIn("OUT-3", text)

    def test_upload_page_rejects_unclosed_quote_without_writes(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(bad_text().encode()), "bad.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("未闭合的引号", response.get_data(as_text=True))

    def test_bad_file_frozen_with_missing_rows_but_complete(self):
        self.assertEqual(self.month_row()["row_count"], 2)
        report = self.report().get_json()
        self.assertEqual(report["total_shipments"], 2)
        self.assertEqual(report["total_boxes"], 19)

    def test_successful_supplement_adds_rows_and_keeps_originals(self):
        response = self.supplement()
        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json()
        self.assertEqual(body["added_count"], 2)
        self.assertEqual(body["added_boxes"], 7)
        self.assertEqual(body["row_count"], 4)
        self.assertEqual(body["total_boxes"], 26)

        # Original links / API stay on the frozen facts.
        original = self.report()
        self.assertEqual(original.get_json()["total_shipments"], 2)
        self.assertEqual(original.get_json()["total_boxes"], 19)
        self.assertFalse(original.get_json()["supplement"]["supplemented"])

        # Explicit selection reads the repaired view.
        fixed = self.report(supplement=True)
        self.assertEqual(fixed.status_code, 200)
        payload = fixed.get_json()
        self.assertEqual(payload["total_shipments"], 4)
        self.assertEqual(payload["total_boxes"], 26)
        self.assertTrue(payload["supplement"]["supplemented"])
        self.assertEqual(payload["supplement"]["added_count"], 2)
        sequence_nos = {
            item["sequence_no"]
            for group in [payload["zones"], [payload["unclassified"]]]
            for zone in group
            for item in zone["shipments"]
        }
        self.assertEqual(sequence_nos, {"OUT-1", "OUT-2", "OUT-3", "OUT-4"})
        # No knowledge version => everything unclassified, but all 26 boxes show.
        self.assertEqual(payload["zones"], [])
        self.assertEqual(payload["unclassified"]["boxes"], 26)

        # Frozen facts in months/shipments are untouched.
        self.assertEqual(self.month_row()["row_count"], 2)
        conn = connect(self.db_path)
        try:
            frozen = conn.execute(
                "SELECT sequence_no FROM shipments WHERE month='2026-03' ORDER BY sequence_no"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([row["sequence_no"] for row in frozen], ["OUT-1", "OUT-2"])

    def test_supplement_shows_boxes_by_selected_version(self):
        self.register("SKU-A", "冷藏", base=None)
        self.register("SKU-B", "冷藏", base=1)
        self.register("SKU-C", "冷藏", base=2)
        self.assertEqual(self.supplement().status_code, 201)
        fixed = self.report(supplement=True).get_json()
        self.assertEqual({z["zone"]: z["boxes"] for z in fixed["zones"]}, {"冷藏": 26})
        self.assertEqual(fixed["classified_boxes"], 26)
        self.assertEqual(fixed["unclassified"]["boxes"], 0)
        # Original view still only has the two frozen rows.
        original = self.report().get_json()
        self.assertEqual(original["total_boxes"], 19)
        self.assertEqual({z["zone"]: z["boxes"] for z in original["zones"]}, {"冷藏": 19})

    def test_report_page_can_select_supplement(self):
        self.assertEqual(self.supplement().status_code, 201)
        page = self.client.get("/report?month=2026-03&supplement=1")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("补齐版", text)
        self.assertIn("OUT-3", text)
        plain = self.client.get("/report?month=2026-03").get_data(as_text=True)
        self.assertIn("切换到补齐版", plain)
        # The page form also shows the selector once a supplement exists.
        self.assertIn('name="supplement"', plain)

    def test_supplement_is_chosen_by_version(self):
        self.register("SKU-A", "冷藏", base=None)
        self.assertEqual(self.supplement().status_code, 201)
        self.register("SKU-A", "冷冻", base=1)
        v1 = self.report(supplement=True, version=1).get_json()
        v2 = self.report(supplement=True, version=2).get_json()
        # SKU-A boxes: 12 (frozen OUT-1) + 5 (recovered OUT-3)
        self.assertEqual({z["zone"]: z["boxes"] for z in v1["zones"]}, {"冷藏": 17})
        self.assertEqual({z["zone"]: z["boxes"] for z in v2["zones"]}, {"冷冻": 17})
        self.assertEqual(v1["unclassified"]["boxes"], 9)
        self.assertEqual(v2["unclassified"]["boxes"], 9)

    def test_supplement_without_one_rejected(self):
        response = self.report(supplement=True)
        self.assertEqual(response.status_code, 404)

    def test_bad_sha_must_match_frozen_record(self):
        other = bad_text() + "OUT-9,SKU-Z,2026-03-20T00:00:00Z,1,ok\n"
        response = self.supplement(bad=other)
        self.assertEqual(response.status_code, 409)
        conn = connect(self.db_path)
        try:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM month_supplements").fetchone()
            )
        finally:
            conn.close()

    def test_bad_file_must_really_have_unclosed_quote(self):
        response = self.supplement(bad=fixed_text())
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有未闭合的引号", response.get_json()["message"])

    def test_corrected_may_only_add_quote_bytes(self):
        changed = fixed_text().replace("OUT-3", "OUT-9")
        response = self.supplement(fixed=changed)
        self.assertEqual(response.status_code, 400)
        self.assertIn("闭引号", response.get_json()["message"])
        # Submitting the bad file untouched also changes nothing.
        self.assertEqual(self.supplement(fixed=bad_text()).status_code, 400)

    def test_corrected_must_keep_every_frozen_shipment(self):
        dropped = fixed_text().replace(
            "OUT-1,SKU-A,2026-03-01T01:00:00Z,12,ok\n", ""
        )
        response = self.supplement(fixed=dropped)
        self.assertEqual(response.status_code, 400)
        self.assertIn("OUT-1", response.get_json()["message"])

    def test_corrected_must_not_change_frozen_values(self):
        altered = fixed_text().replace(
            "OUT-1,SKU-A,2026-03-01T01:00:00Z,12",
            "OUT-1,SKU-A,2026-03-01T01:00:00Z,99",
        )
        response = self.supplement(fixed=altered)
        self.assertEqual(response.status_code, 400)
        self.assertIn("OUT-1", response.get_json()["message"])

    def test_corrected_must_recover_at_least_one_row(self):
        # A valid file that keeps exactly the frozen rows recovers nothing.
        only_frozen = (
            "流水号,SKU,出库时刻,箱数,备注\n"
            "OUT-1,SKU-A,2026-03-01T01:00:00Z,12,ok\n"
            "OUT-2,SKU-B,2026-03-05T23:30:00Z,7,x\n"
        )
        response = self.supplement(fixed=only_frozen)
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有找回", response.get_json()["message"])

    def test_second_supplement_rejected(self):
        self.assertEqual(self.supplement().status_code, 201)
        response = self.supplement()
        self.assertEqual(response.status_code, 409)
        conn = connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM month_supplements"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_failed_supplement_leaves_nothing(self):
        dropped = fixed_text().replace(
            "OUT-1,SKU-A,2026-03-01T01:00:00Z,12,ok\n", ""
        )
        response = self.supplement(fixed=dropped)
        self.assertEqual(response.status_code, 400)
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM month_supplements"
                ).fetchone()["c"],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM supplement_shipments"
                ).fetchone()["c"],
                0,
            )
        finally:
            conn.close()
        # The correct attempt still works afterwards.
        self.assertEqual(self.supplement().status_code, 201)

    def test_sequence_shared_with_normal_upload(self):
        # OUT-3 already belongs to another month uploaded normally.
        self.client.post(
            "/api/months",
            data={
                "file": (
                    io.BytesIO(
                        "流水号,SKU,出库时刻,箱数\nOUT-3,SKU-X,2026-04-02T00:00:00Z,3\n".encode()
                    ),
                    "apr.csv",
                )
            },
            content_type="multipart/form-data",
        )
        response = self.supplement()
        self.assertEqual(response.status_code, 409)
        self.assertIn("OUT-3", response.get_json()["message"])
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM month_supplements"
                ).fetchone()["c"],
                0,
            )
        finally:
            conn.close()

    def test_supplement_sequence_blocks_later_normal_upload(self):
        self.assertEqual(self.supplement().status_code, 201)
        response = self.client.post(
            "/api/months",
            data={
                "file": (
                    io.BytesIO(
                        "流水号,SKU,出库时刻,箱数\nOUT-4,SKU-X,2026-05-02T00:00:00Z,3\n".encode()
                    ),
                    "may.csv",
                )
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("OUT-4", response.get_json()["message"])

    def test_simultaneous_submission_keeps_sequence_unique(self):
        # Freeze April from a bad file that also lost OUT-3 / OUT-4.
        apr_bad, apr_fixed = bad_pair(
            "2026-04", frozen_prefix="APR-", recovered_prefix="APR-"
        )
        conn = connect(self.db_path)
        try:
            freeze_legacy_bad(conn, apr_bad, "apr-bad.csv")
        finally:
            conn.close()
        march = self.supplement()
        april = self.client.post(
            "/api/months",
            data={
                "bad_file": (io.BytesIO(apr_bad.encode()), "b4.csv"),
                "corrected_file": (io.BytesIO(apr_fixed.encode()), "f4.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(march.status_code, 201)
        self.assertEqual(april.status_code, 201)
        conn = connect(self.db_path)
        try:
            duplicates = conn.execute(
                "SELECT sequence_no, COUNT(*) AS c FROM ("
                " SELECT sequence_no FROM shipments"
                " UNION ALL SELECT sequence_no FROM supplement_shipments"
                ") GROUP BY sequence_no HAVING c > 1"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(duplicates, [])

    def test_simultaneous_supplements_with_shared_sequence_one_wins(self):
        # April's repaired file would add the very OUT-4 that March's
        # supplement now owns: only one of the two may commit, and the loser
        # leaves neither shipments nor a supplement behind.
        apr_bad = (
            f"{HEADER}\n"
            "APR-1,SKU-A,2026-04-01T01:00:00Z,12,ok\n"
            "APR-2,SKU-B,2026-04-05T23:30:00Z,7,ok\n"
            'APR-3,SKU-A,2026-04-10T00:00:00Z,5,"refrigerated\n'
            "OUT-4,SKU-C,2026-04-20T23:59:59Z,2,ok\n"
        )
        apr_fixed = apr_bad.replace('"refrigerated\n', '"refrigerated"\n', 1)
        conn = connect(self.db_path)
        try:
            freeze_legacy_bad(conn, apr_bad, "apr-bad.csv")
        finally:
            conn.close()
        march = self.supplement()
        april = self.client.post(
            "/api/months",
            data={
                "bad_file": (io.BytesIO(apr_bad.encode()), "b4.csv"),
                "corrected_file": (io.BytesIO(apr_fixed.encode()), "f4.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(march.status_code, 201)
        self.assertEqual(april.status_code, 409)
        self.assertIn("OUT-4", april.get_json()["message"])
        conn = connect(self.db_path)
        try:
            supplements = conn.execute(
                "SELECT month FROM month_supplements ORDER BY month"
            ).fetchall()
            april_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM supplement_shipments WHERE month='2026-04'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual([row["month"] for row in supplements], ["2026-03"])
        self.assertEqual(april_rows, 0)

    def test_supplement_survives_restart(self):
        self.assertEqual(self.supplement().status_code, 201)
        before = self.report(supplement=True).get_json()
        original_before = self.report().get_json()

        restarted = create_app({"TESTING": True, "DB_PATH": self.db_path})
        client = restarted.test_client()
        after = client.get("/api/report?month=2026-03&supplement=1").get_json()
        original_after = client.get("/api/report?month=2026-03").get_json()
        self.assertEqual(before, after)
        self.assertEqual(original_before, original_after)
        self.assertEqual(after["total_shipments"], 4)
        self.assertEqual(original_after["total_shipments"], 2)

    def test_supplement_facts_are_immutable(self):
        self.assertEqual(self.supplement().status_code, 201)
        conn = connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE supplement_shipments SET boxes = 1")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM month_supplements")
        finally:
            conn.close()

    def test_unknown_month_supplement_rejected(self):
        feb_bad, feb_fixed = bad_pair("2026-02")
        response = self.client.post(
            "/api/months",
            data={
                "bad_file": (io.BytesIO(feb_bad.encode()), "b.csv"),
                "corrected_file": (io.BytesIO(feb_fixed.encode()), "f.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 404)

    def test_supplement_missing_file_rejected(self):
        response = self.client.post(
            "/api/months",
            data={"bad_file": (io.BytesIO(bad_text().encode()), "b.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)

    def test_month_param_must_match_corrected_file(self):
        response = self.supplement(month="2026-04")
        self.assertEqual(response.status_code, 400)

    def test_months_api_reports_supplement(self):
        self.assertEqual(self.supplement().status_code, 201)
        months = self.client.get("/api/months").get_json()["months"]
        self.assertEqual(len(months), 1)
        self.assertIsNotNone(months[0]["supplement_id"])
        self.assertEqual(months[0]["supplement_row_count"], 4)


class ConcurrentSequenceTests(unittest.TestCase):
    """A normal upload and a supplement racing must not both claim a sequence."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "race.sqlite3")
        self.app = create_app({"TESTING": True, "DB_PATH": self.db_path})
        conn = connect(self.db_path)
        try:
            freeze_legacy_bad(conn, bad_text())
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parallel_upload_and_supplement_keep_sequences_unique(self):
        # Both requests want OUT-3: the supplement recovers it for 2026-03,
        # the normal upload tries to freeze it for 2026-05.
        bad = bad_text().encode()
        fixed = fixed_text().encode()
        normal_csv = (
            "流水号,SKU,出库时刻,箱数\nOUT-3,SKU-X,2026-05-02T00:00:00Z,3\n"
        ).encode()
        barrier = threading.Barrier(2)
        results = []

        def do_supplement():
            client = self.app.test_client()
            barrier.wait()
            response = client.post(
                "/api/months",
                data={
                    "bad_file": (io.BytesIO(bad), "bad.csv"),
                    "corrected_file": (io.BytesIO(fixed), "fixed.csv"),
                },
                content_type="multipart/form-data",
            )
            results.append(response.status_code)

        def do_upload():
            client = self.app.test_client()
            barrier.wait()
            response = client.post(
                "/api/months",
                data={"file": (io.BytesIO(normal_csv), "may.csv")},
                content_type="multipart/form-data",
            )
            results.append(response.status_code)

        threads = [
            threading.Thread(target=do_supplement),
            threading.Thread(target=do_upload),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [201, 409])
        conn = connect(self.db_path)
        try:
            duplicates = conn.execute(
                "SELECT sequence_no, COUNT(*) AS c FROM ("
                " SELECT sequence_no FROM shipments"
                " UNION ALL SELECT sequence_no FROM supplement_shipments"
                ") GROUP BY sequence_no HAVING c > 1"
            ).fetchall()
            months = {
                row["month"]
                for row in conn.execute("SELECT month FROM months").fetchall()
            }
            in_may = conn.execute(
                "SELECT 1 FROM shipments WHERE month='2026-05' AND sequence_no='OUT-3'"
            ).fetchone()
            in_supp = conn.execute(
                "SELECT 1 FROM supplement_shipments WHERE sequence_no='OUT-3'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(duplicates, [])
        self.assertIn("2026-03", months)
        # Exactly one owner of OUT-3: either May froze it (supplement failed)
        # or March's supplement took it (May upload failed).
        self.assertEqual(bool(in_may) + bool(in_supp), 1)
        self.assertEqual("2026-05" in months, bool(in_may))


if __name__ == "__main__":
    unittest.main()
