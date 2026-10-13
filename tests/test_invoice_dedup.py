"""发票批量查重 Python 移植的 golden tests。

用例全量移植自 apps/web/src/lib/invoiceDedup/keys.test.ts + dedup.test.ts
（TS 行为为基准——两版语义必须逐分支一致，升级即全员生效的承诺依赖此护栏）。
"""
from app.services.invoice_dedup import (
    LedgerEntry,
    ParsedTicket,
    add_amounts,
    clean_amount,
    cn_date_from_text,
    date_from_file_name,
    dedup_tickets,
    excel_text,
    is_classic_code,
    is_valid_e_invoice_no,
    make_key,
    normalize_text,
    to_ledger_entries,
)

NO1 = "24312000000012345678"
NO2 = "26112000001182262471"


# ---------------------------------------------------------------------------
# keys.test.ts → normalizeText / cleanAmount / isValidEInvoiceNo /
# isClassicCode / makeKey / dateFromFileName / addAmounts / excelText
# ---------------------------------------------------------------------------

class TestNormalizeText:
    def test_fullwidth_to_halfwidth_and_strip_whitespace(self):
        assert normalize_text("２４３１２０００００ ００１２３４５６７８") == "24312000000012345678"
        assert normalize_text("１２３　４５６") == "123456"


class TestCleanAmount:
    def test_strips_currency_thousands_and_pads_decimals(self):
        assert clean_amount("¥736,792.00") == "736792.00"
        assert clean_amount("￥270") == "270.00"
        assert clean_amount("1,234.5") == "1234.50"

    def test_invalid_input_returns_none(self):
        assert clean_amount("abc") is None
        assert clean_amount("") is None
        assert clean_amount(None) is None


class TestIsValidEInvoiceNo:
    def test_valid_year_and_province(self):
        assert is_valid_e_invoice_no("24312000000012345678") is True
        assert is_valid_e_invoice_no("26112000001182262471") is True

    def test_rejects_bad_length_year_or_province(self):
        assert is_valid_e_invoice_no("2431200000001234567") is False  # 19 位
        assert is_valid_e_invoice_no("20312000000012345678") is False  # 年度 20
        assert is_valid_e_invoice_no("24662000000012345678") is False  # 省份 66


class TestIsClassicCode:
    def test_10_to_12_digits(self):
        assert is_classic_code("011001900111") is True
        assert is_classic_code("1234567890") is True
        assert is_classic_code("123456789") is False


class TestMakeKey:
    def test_e_invoice_number_only(self):
        assert make_key("24312000000012345678") == "24312000000012345678"

    def test_classic_code_dash_number_with_fullwidth_normalized(self):
        assert make_key("１２３４５６７８", "011001900111") == "011001900111-12345678"

    def test_missing_number_returns_empty(self):
        assert make_key("", None) == ""

    def test_pseudo_code_not_joined(self):
        assert make_key("24312000000012345678", "12345") == "24312000000012345678"


class TestDateFromFileName:
    def test_standard_e_invoice_filename_timestamp(self):
        assert (
            date_from_file_name(
                "dzfp_26112000003693537661_永诚财产保险股份有限公司_20260902095513.ofd"
            )
            == "2026-09-02"
        )
        assert (
            date_from_file_name("dzfp_24112000000023929729_凯本_20240416161647.pdf")
            == "2024-04-16"
        )

    def test_no_timestamp_or_illegal_date_returns_none(self):
        assert date_from_file_name("发票.pdf") is None
        assert date_from_file_name("dzfp_202613401234_x.pdf") is None  # 13 月


class TestAddAmounts:
    def test_cent_unit_addition_and_single_side(self):
        assert add_amounts("254.72", "15.28") == "270.00"
        assert add_amounts("270.00", None) == "270.00"
        assert add_amounts(None, "270.00") == "270.00"
        assert add_amounts(None, None) is None


class TestExcelText:
    def test_formula_style_text_wraps_long_number(self):
        assert excel_text("24312000000012345678") == '="24312000000012345678"'

    def test_empty_returns_empty_and_quotes_doubled(self):
        assert excel_text(None) == ""
        assert excel_text('12"3') == '="12""3"'


class TestCnDateFromText:
    def test_scans_first_legal_cn_date(self):
        assert cn_date_from_text("开票日期：2026年08月25日 价税合计") == "2026-08-25"

    def test_illegal_date_skipped_then_next_candidate(self):
        # 13 月非法 → 继续扫描，命中后续合法日期（TS 版 continue 语义）
        assert cn_date_from_text("2026年13月01日 后面有 2026年08月25日") == "2026-08-25"

    def test_no_date_returns_none(self):
        assert cn_date_from_text("无日期文本") is None


# ---------------------------------------------------------------------------
# dedup.test.ts → dedupTickets / toLedgerEntries
# ---------------------------------------------------------------------------


def t(**over) -> ParsedTicket:
    file_name = over.pop("file_name", "a.xml")
    base = dict(
        file_name=file_name,
        file_ref=file_name,
        code=None,
        number=NO1,
        amount="270.00",
        date=None,
    )
    base.update(over)
    file_ref = base.pop("file_ref", file_name)
    return ParsedTicket(file_ref=file_ref, **base)


def ledger_entry(**over) -> LedgerEntry:
    entry = LedgerEntry(
        key=NO2,
        number=NO2,
        code=None,
        amount="270.00",
        first_name="旧票.xml",
        first_day="2026-08-15",
    )
    for k, v in over.items():
        setattr(entry, k, v)
    return entry


class TestDedupTickets:
    def test_first_ok_batch_second_duplicate_with_row_number(self):
        rows = dedup_tickets(
            [t(file_name="1.xml"), t(file_name="2.xml"), t(file_name="3.xml", number=NO2)],
            {},
        )
        assert [r.status for r in rows] == ["ok", "duplicate", "ok"]
        assert "本批第 1 行" in rows[1].note

    def test_ledger_hit_duplicate_with_source(self):
        rows = dedup_tickets([t(number=NO2)], {NO2: ledger_entry()})
        assert rows[0].status == "duplicate"
        assert "历史台账" in rows[0].note
        assert "2026-08-15" in rows[0].note

    def test_same_number_different_amount_suspicious(self):
        rows = dedup_tickets(
            [t(file_name="1.xml", amount="270.00"), t(file_name="2.xml", amount="271.00")],
            {},
        )
        assert rows[1].status == "suspicious"
        assert "270.00" in rows[1].note
        assert "271.00" in rows[1].note

    def test_same_file_multiple_rows_same_key_infile_duplicate(self):
        rows = dedup_tickets(
            [
                t(file_name="dup.pdf", file_ref="dup.pdf"),
                t(file_name="dup.pdf#2", file_ref="dup.pdf"),
            ],
            {},
        )
        assert rows[1].status == "duplicate"
        assert "同一文件内" in rows[1].note

    def test_classic_ticket_keyed_by_code_dash_number(self):
        a = t(code="011001900111", number="12345678", file_name="a.pdf")
        b = t(code="011001900111", number="12345678", file_name="b.pdf")
        c = t(code="022002000222", number="12345678", file_name="c.pdf")
        rows = dedup_tickets([a, b, c], {})
        assert [r.status for r in rows] == ["ok", "duplicate", "ok"]

    def test_unrecognized_not_participating(self):
        rows = dedup_tickets([t(number=None), t()], {})
        assert rows[0].status == "unrecognized"
        assert rows[1].status == "ok"

    def test_amount_single_side_missing_is_plain_duplicate(self):
        rows = dedup_tickets(
            [t(file_name="1.xml", amount="270.00"), t(file_name="2.ofd", amount=None)],
            {},
        )
        assert rows[1].status == "duplicate"


class TestToLedgerEntries:
    def test_only_recognized_deduped_first_per_key(self):
        rows = dedup_tickets(
            [t(file_name="1.xml"), t(file_name="2.xml"), t(file_name="3.xml", number=None)],
            {},
        )
        entries = to_ledger_entries(rows, today="2026-10-13")
        assert len(entries) == 1
        assert entries[0].number == NO1
        assert entries[0].first_name == "1.xml"
        assert entries[0].first_day == "2026-10-13"
