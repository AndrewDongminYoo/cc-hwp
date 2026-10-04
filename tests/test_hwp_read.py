"""Regression tests: run with `python3 -m unittest discover tests` (stdlib only)."""
import collections
import html
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
import xml.etree.ElementTree as ET

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "skills", "hwp-read", "scripts", "hwp_read.py")
sys.path.insert(0, os.path.dirname(SCRIPT))
import hwp_read as h  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _chars(s: str) -> collections.Counter:
    return collections.Counter(c for c in s if not c.isspace() and c not in "|<>-")


def _md_chars(md: str) -> collections.Counter:
    md = re.sub(r"</?(table|tr|td|br)[^>]*>", " ", md)
    return _chars(html.unescape(md))


def _raw_hwp5_chars(path):
    r = h.Hwp5Reader(_read(path))
    out = []
    n = 0
    while r.cfb.exists(f"BodyText/Section{n}"):
        for rec in h._records(r._stream(f"BodyText/Section{n}")):
            if rec.tag == h.TAG_PARA_TEXT:
                w = struct.unpack_from(f"<{len(rec.data)//2}H", rec.data)
                i = 0
                while i < len(w):
                    if w[i] >= 32:
                        out.append(chr(w[i])); i += 1
                    elif w[i] in h.EXTENDED_CTRL or w[i] in h.INLINE_CTRL:
                        i += 8
                    else:
                        i += 1
        n += 1
    return _chars("".join(out))


def _raw_hwpx_chars(path):
    z = zipfile.ZipFile(path)
    txt = []
    for n in z.namelist():
        if re.match(r"Contents/section\d+\.xml$", n):
            for m in re.finditer(r"<hp:t(?:\s[^>]*)?>(.*?)</hp:t>", z.read(n).decode(), re.S):
                txt.append(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))))
    return _chars("".join(txt))


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def check(self, name, raw_fn, fmt, min_tables):
        p = os.path.join(FIX, name)
        doc = h.load(p)
        md = h.to_markdown(doc)
        st = h.stats(doc, md)
        self.assertEqual(st["format"], fmt)
        self.assertEqual(st["preview_coverage"], 1.0)
        self.assertGreaterEqual(st["tables"], min_tables)
        # Conservation: every visible character in the source text records
        # must survive into the Markdown (nothing silently dropped).
        missing = raw_fn(p) - _md_chars(md)
        self.assertFalse(missing, f"characters lost: {dict(missing.most_common(10))}")

    def test_hwp5(self):
        self.check("e-phi-design.hwp", _raw_hwp5_chars, "hwp5", 50)

    def test_hwpx(self):
        self.check("sk-openinno-form.hwpx", _raw_hwpx_chars, "hwpx", 10)

    def test_detect_ignores_extension(self):
        data = _read(os.path.join(FIX, "sk-openinno-form.hwpx"))
        self.assertEqual(h.detect(data), "hwpx")
        self.assertEqual(h.detect(b"not a doc"), "unknown")

    def test_distribution_flag_rejected(self):
        data = bytearray(_read(os.path.join(FIX, "e-phi-design.hwp")))
        # Flip bit 2 (배포용) in the uncompressed FileHeader and expect a clear refusal.
        sig = data.find(b"HWP Document File")
        self.assertGreater(sig, 0)
        flags = struct.unpack_from("<I", data, sig + 36)[0]
        struct.pack_into("<I", data, sig + 36, flags | 0x04)
        with self.assertRaises(h.Unsupported):
            h.Hwp5Reader(bytes(data))

    def test_zip_without_hwpx_markers_rejected(self):
        p = os.path.join(self.tmp, "plain.zip")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("readme.txt", "not a hangul document")
        with self.assertRaises(h.Unsupported):
            h.load(p)

    def test_images_dir_cannot_escape(self):
        src = os.path.join(FIX, "sk-openinno-form.hwpx")
        p = os.path.join(self.tmp, "evil.hwpx")
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(p, "w") as zout:
            for info in zin.infolist():
                zout.writestr(info, zin.read(info.filename))
            zout.writestr("BinData/../../escaped.bin", b"payload")
            absolute = os.path.join(self.tmp, "absolute.bin")
            zout.writestr("BinData/" + absolute, b"payload")  # BinData//tmp/... after the prefix
        outdir = os.path.join(self.tmp, "a", "b", "images")
        written = h.save_images(p, h.load(p), outdir)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "escaped.bin")))
        self.assertFalse(os.path.exists(absolute))
        root = os.path.realpath(outdir)
        for w in written:
            self.assertTrue(os.path.realpath(w).startswith(root + os.sep), w)

    def test_utf16_surrogate_pairs_combined(self):
        units = [0xAC00, 0xD83D, 0xDE00, 0x0041, 0xD800]  # 가, 😀 as a pair, A, lone high surrogate
        out, i = [], 0
        while i < len(units):
            s, n = h._utf16_char(units, i)
            out.append(s)
            i += n
        self.assertEqual("".join(out), "가😀A�")
        "".join(out).encode("utf-8")  # must not raise UnicodeEncodeError

    def test_cfb_directory_cycle_terminates(self):
        data = bytearray(_read(os.path.join(FIX, "e-phi-design.hwp")))
        ss = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        first_dir = struct.unpack_from("<I", data, 0x30)[0]
        entry1 = (first_dir + 1) * ss + 128
        struct.pack_into("<I", data, entry1 + 68, 1)  # entry 1's left sibling -> itself
        p = os.path.join(self.tmp, "cycle.hwp")
        with open(p, "wb") as f:
            f.write(data)
        r = subprocess.run([sys.executable, SCRIPT, "info", p], capture_output=True, text=True, timeout=20)
        self.assertIn(r.returncode, (0, 1, 3), r.stderr)

    def test_cfb_difat_cycle_is_bounded(self):
        data = bytearray(_read(os.path.join(FIX, "e-phi-design.hwp")))
        ss = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        k = len(data) // ss - 2  # last sector index
        struct.pack_into("<II", data, 0x44, k, 10000)  # first DIFAT sector, declared DIFAT count
        struct.pack_into("<I", data, (k + 1) * ss + ss - 4, k)  # its next-DIFAT link points to itself
        calls = []
        orig = h.CFB._sec

        def counting(self, n):
            calls.append(n)
            return orig(self, n)

        h.CFB._sec = counting
        self.addCleanup(setattr, h.CFB, "_sec", orig)
        h.CFB(bytes(data))
        self.assertLess(len(calls), 1000)

    def test_hwpx_textbox_with_table_not_duplicated(self):
        xml = (
            '<hp:p xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:run><hp:rect><hp:drawText>'
            '<hp:subList><hp:p><hp:run><hp:tbl rowCnt="1" colCnt="1"><hp:tr><hp:tc>'
            '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/>'
            '<hp:subList><hp:p><hp:run><hp:t>CELLTEXT</hp:t></hp:run></hp:p></hp:subList>'
            '</hp:tc></hp:tr></hp:tbl></hp:run></hp:p></hp:subList>'
            '</hp:drawText></hp:rect></hp:run></hp:p>'
        )
        r = h.HwpxReader.__new__(h.HwpxReader)
        r.doc = h.Doc("hwpx")
        r.doc.blocks = r._para(h.ET.fromstring(xml))
        self.assertEqual(h.to_markdown(r.doc).count("CELLTEXT"), 1)

    def test_hwp5_textbox_with_table_not_duplicated(self):
        def rec(tag, level, children=()):
            return h.Rec(tag, level, b"", list(children))
        cell_para = rec(h.TAG_PARA_HEADER, 4)
        table = rec(h.TAG_CTRL_HEADER, 3, [rec(h.TAG_LIST_HEADER, 4), cell_para])
        box_para = rec(h.TAG_PARA_HEADER, 2, [table])
        gso = rec(h.TAG_CTRL_HEADER, 1, [rec(h.TAG_LIST_HEADER, 2), box_para])
        lists = h._find_all_lists(gso)
        self.assertEqual(lists, [[box_para]])

    def test_note_with_table_keeps_cells(self):
        t = h.Table(1, 2, [h.Cell(0, 0, 1, 1, [h.Para("NOTECELL")]), h.Cell(0, 1, 1, 1, [h.Para("x")])])
        doc = h.Doc("hwp5", blocks=[h.Para("body[^1]")], notes=[[h.Para("see"), t]])
        self.assertIn("NOTECELL", h.to_markdown(doc))

    def test_html_table_keeps_rows_covered_by_rowspan(self):
        t = h.Table(3, 2, [h.Cell(0, 0, 2, 1, [h.Para("A")]), h.Cell(0, 1, 2, 1, [h.Para("B")]),
                           h.Cell(2, 0, 1, 1, [h.Para("C")]), h.Cell(2, 1, 1, 1, [h.Para("D")])])
        self.assertEqual(h._table_html(t).count("<tr>"), 3)

    def test_preview_coverage_checks_last_line_of_untruncated_preview(self):
        self.assertLess(h.preview_coverage("present text\nmissing text\n", "present text"), 1.0)
        cut = ("x" * 40 + "\n") * 24 + "partial chunk cut he"  # at the ~1K PrvText cap
        self.assertEqual(h.preview_coverage(cut, ("x" * 40 + "\n") * 24), 1.0)

    def _limit(self, n):
        orig = h.MAX_PART_BYTES
        h.MAX_PART_BYTES = n
        self.addCleanup(setattr, h, "MAX_PART_BYTES", orig)

    def test_hwpx_oversized_part_rejected(self):
        self._limit(1000)  # section0.xml is far larger than this
        with self.assertRaises(h.Unsupported):
            h.load(os.path.join(FIX, "sk-openinno-form.hwpx"))

    def test_hwp5_oversized_stream_rejected(self):
        self._limit(1000)  # BodyText/Section0 inflates far beyond this
        with self.assertRaises(h.Unsupported):
            h.load(os.path.join(FIX, "e-phi-design.hwp"))

    def test_hwpx_dtd_rejected(self):
        src = os.path.join(FIX, "sk-openinno-form.hwpx")
        p = os.path.join(self.tmp, "dtd.hwpx")
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(p, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "Contents/section0.xml":
                    data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + data.split(b"?>", 1)[1]
                zout.writestr(info, data)
        with self.assertRaises(h.Unsupported):
            h.load(p)

    def test_utf16_dtd_rejected(self):
        doc = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "aaaa">]><r>&a;</r>'
        with self.assertRaises(h.Unsupported):
            h._xml(doc.encode("utf-16"))

    def test_truncated_deflate_stream_is_a_parse_error(self):
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(b"complete paragraph text " * 20) + c.flush()
        with self.assertRaises(ValueError):
            h._inflate(raw[: len(raw) // 2], "BodyText/Section0", h._Budget())

    def test_document_budget_spans_hwpx_parts(self):
        p = os.path.join(FIX, "sk-openinno-form.hwpx")
        read_by_load = {"mimetype", "META-INF/manifest.xml", "Contents/content.hpf",
                        "Preview/PrvText.txt", "Contents/section0.xml"}
        with zipfile.ZipFile(p) as z:
            largest = max(i.file_size for i in z.infolist() if i.filename in read_by_load)
        orig = h.MAX_DOC_BYTES
        h.MAX_DOC_BYTES = largest + 1  # every single part fits; their sum does not
        self.addCleanup(setattr, h, "MAX_DOC_BYTES", orig)
        with self.assertRaises(h.Unsupported):
            h.load(p)

    def test_document_budget_spans_hwp5_streams(self):
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(b"x" * 100) + c.flush()
        budget = h._Budget()
        budget.left = 150  # room for one 100-byte stream, not two
        self.assertEqual(len(h._inflate(raw, "BodyText/Section0", budget)), 100)
        with self.assertRaises(h.Unsupported):
            h._inflate(raw, "BodyText/Section1", budget)

    def test_exhausted_budget_still_limits(self):
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(b"x" * 100) + c.flush()
        budget = h._Budget()
        budget.left = 100  # the first stream spends it exactly; zlib reads max_length=0 as "no limit"
        h._inflate(raw, "BodyText/Section0", budget)
        with self.assertRaises(h.Unsupported):
            h._inflate(raw, "BodyText/Section1", budget)

    def test_failed_inflation_is_charged(self):
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(b"complete paragraph text " * 20) + c.flush()
        budget = h._Budget()
        before = budget.left
        with self.assertRaises(ValueError):
            h._inflate(raw[: len(raw) // 2], "BinData/BIN0001.bmp", budget)
        self.assertLess(budget.left, before)

    def test_inflation_charged_before_zlib_error(self):
        orig = h.INFLATE_CHUNK
        h.INFLATE_CHUNK = 64
        self.addCleanup(setattr, h, "INFLATE_CHUNK", orig)
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(b"x" * 1000) + c.flush(zlib.Z_SYNC_FLUSH) + b"\x06"  # valid prefix, then a reserved block type
        budget = h._Budget()
        before = budget.left
        with self.assertRaises(zlib.error):
            h._inflate(raw, "BinData/BIN0001.bmp", budget)
        self.assertLessEqual(budget.left, before - (1000 - h.INFLATE_CHUNK))

    def test_sub_chunk_zlib_failure_is_charged(self):
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(b"x" * 100) + c.flush(zlib.Z_SYNC_FLUSH) + b"\x06"  # fails inside the first chunk
        budget = h._Budget()
        before = budget.left
        with self.assertRaises(zlib.error):
            h._inflate(raw, "BinData/BIN0001.bmp", budget)
        self.assertGreaterEqual(before - budget.left, 100)

    def test_overcharged_budget_refuses_further_reads(self):
        c = zlib.compressobj(wbits=-15)
        bad = c.compress(b"x" * 10) + c.flush(zlib.Z_SYNC_FLUSH) + b"\x06"
        c = zlib.compressobj(wbits=-15)
        good = c.compress(b"y" * 1000) + c.flush()
        budget = h._Budget()
        budget.left = 50
        with self.assertRaises(zlib.error):
            h._inflate(bad, "BinData/BIN0001.bmp", budget)  # charges want = 51, leaving -1
        self.assertEqual(budget.left, -1)
        with self.assertRaises(h.Unsupported):
            h._inflate(good, "BinData/BIN0002.bmp", budget)
        self.assertEqual(budget.left, -1)  # refused before inflating anything, not after
        z = zipfile.ZipFile(os.path.join(FIX, "sk-openinno-form.hwpx"))
        with self.assertRaises(h.Unsupported):
            h._zip_read(z, "mimetype", budget)

    def test_small_stored_entry_failure_charges_little(self):
        stored = b"\x01\x00\x09\x00" + b"\x00" * 60  # WMF-like bytes, not a deflate stream
        budget = h._Budget()
        before = budget.left
        with self.assertRaises(zlib.error):
            h._inflate(stored, "BinData/BIN0005.wmf", budget)
        charged = before - budget.left
        self.assertEqual(charged, h._inflate_bound(len(stored)))
        self.assertLess(charged, h.INFLATE_CHUNK // 10)

    def test_stored_image_signatures(self):
        for head in (b"\xff\xd8\xff\xe0", b"\x89PNG\r\n\x1a\n", b"GIF89a", b"BM\x36\x00"):
            self.assertTrue(h._looks_stored(head + b"rest"), head)
        c = zlib.compressobj(wbits=-15)
        self.assertFalse(h._looks_stored(c.compress(b"x" * 100) + c.flush()))

    def test_chunked_inflation_restores_whole_stream(self):
        orig = h.INFLATE_CHUNK
        h.INFLATE_CHUNK = 64
        self.addCleanup(setattr, h, "INFLATE_CHUNK", orig)
        payload = bytes(range(256)) * 40
        c = zlib.compressobj(wbits=-15)
        raw = c.compress(payload) + c.flush()
        budget = h._Budget()
        before = budget.left
        self.assertEqual(h._inflate(raw, "BodyText/Section0", budget), payload)
        self.assertEqual(before - budget.left, len(payload))

    def test_images_share_the_document_budget(self):
        p = os.path.join(FIX, "sk-openinno-form.hwpx")
        doc = h.load(p)
        doc.budget.left = 10  # parsing spent the rest; the BinData images do not fit
        with self.assertRaises(h.Unsupported):
            h.save_images(p, doc, os.path.join(self.tmp, "images"))

    def test_hwpx_package_read_once(self):
        reads = []
        orig = h._zip_read

        def counting(z, name, budget):
            reads.append(name)
            return orig(z, name, budget)

        h._zip_read = counting
        self.addCleanup(setattr, h, "_zip_read", orig)
        h.load(os.path.join(FIX, "sk-openinno-form.hwpx"))
        self.assertEqual(reads.count("Contents/content.hpf"), 1)

    def test_trailing_partial_record_header_is_a_parse_error(self):
        record = struct.pack("<I", h.TAG_PARA_TEXT | (2 << 20)) + b"ab"
        self.assertEqual(len(h._records(record)), 1)
        with self.assertRaises(ValueError):
            h._records(record + b"\x01\x02")

    def test_record_overrunning_section_is_a_parse_error(self):
        header = struct.pack("<I", h.TAG_PARA_TEXT | (100 << 20))  # declares 100 bytes
        with self.assertRaises(ValueError):
            h._records(header + b"x" * 10)

    def test_hwp5_without_body_section_rejected(self):
        data = bytearray(_read(os.path.join(FIX, "e-phi-design.hwp")))
        name = "Section0".encode("utf-16-le")
        i = data.find(name)
        self.assertGreater(i, 0)
        data[i:i + len(name)] = "SectionX".encode("utf-16-le")
        p = os.path.join(self.tmp, "nobody.hwp")
        with open(p, "wb") as f:
            f.write(data)
        with self.assertRaises(h.Unsupported):
            h.load(p)

    def test_preview_coverage_counts_duplicate_lines(self):
        self.assertLess(h.preview_coverage("repeated line\nrepeated line\nother line\n",
                                           "repeated line other line"), 1.0)

    def test_multiline_note_stays_in_definition(self):
        doc = h.Doc("hwp5", blocks=[h.Para("body[^1]")], notes=[[h.Para("first\nsecond")]])
        self.assertIn("[^1]: first second", h.to_markdown(doc))

    def _docx_xml(self, name):
        doc = h.load(os.path.join(FIX, name))
        z = zipfile.ZipFile(io.BytesIO(h.to_docx(doc)))
        for part in z.namelist():
            if part.endswith((".xml", ".rels")):
                ET.fromstring(z.read(part))  # every part is well-formed
        return doc, ET.fromstring(z.read("word/document.xml"))

    def _ir_counts(self, blocks, acc=None):
        acc = acc or {"tables": 0, "colspan": 0, "rowspan": 0}
        for b in blocks:
            if isinstance(b, h.Table):
                acc["tables"] += 1
                for c in b.cells:
                    acc["colspan"] += c.colspan > 1
                    acc["rowspan"] += c.rowspan > 1
                    self._ir_counts(c.blocks, acc)
        return acc

    def test_docx_keeps_every_table_and_merge(self):
        for name in ("e-phi-design.hwp", "sk-openinno-form.hwpx"):
            doc, root = self._docx_xml(name)
            starts = []  # cells that begin a merge region (a vMerge continuation repeats its gridSpan)
            for tc in root.iter(f"{W}tc"):
                vm = tc.find(f"{W}tcPr/{W}vMerge")
                if vm is None or vm.get(f"{W}val") == "restart":
                    starts.append(tc)
            got = {
                "tables": len(root.findall(f".//{W}tbl")),
                "colspan": sum(1 for tc in starts
                               if int(tc.find(f"{W}tcPr/{W}gridSpan").get(f"{W}val", "1")
                                      if tc.find(f"{W}tcPr/{W}gridSpan") is not None else 1) > 1),
                "rowspan": sum(1 for v in root.iter(f"{W}vMerge") if v.get(f"{W}val") == "restart"),
            }
            self.assertEqual(got, self._ir_counts(doc.blocks), name)

    def test_docx_keeps_every_character(self):
        for name in ("e-phi-design.hwp", "sk-openinno-form.hwpx"):
            doc, root = self._docx_xml(name)
            out = _chars("".join(t.text or "" for t in root.iter(f"{W}t")))
            missing = _md_chars(h.to_markdown(doc)) - out
            self.assertFalse(missing, f"{name}: {dict(missing.most_common(10))}")

    def test_docx_cells_end_with_a_paragraph(self):
        _, root = self._docx_xml("e-phi-design.hwp")
        for tc in root.iter(f"{W}tc"):
            self.assertEqual(tc[-1].tag, f"{W}p")  # Word rejects a cell whose last child is not a paragraph

    def test_docx_refuses_oversized_grids(self):
        big = h.MAX_DOCX_GRID_CELLS + 1
        for cell in (h.Cell(0, 0, 1, big, [h.Para("x")]), h.Cell(0, 0, big, 1, [h.Para("x")])):
            doc = h.Doc("hwpx", blocks=[h.Table(1, 1, [cell])])
            with self.assertRaises(h.Unsupported):
                h.to_docx(doc)

    def test_docx_output_is_deterministic(self):
        doc = h.load(os.path.join(FIX, "sk-openinno-form.hwpx"))
        first = h.to_docx(doc)
        time.sleep(2.1)  # a ZIP timestamp has 2-second resolution; always cross a tick boundary
        self.assertEqual(first, h.to_docx(doc))

    def test_cli_convert_reports_low_coverage(self):
        src = os.path.join(FIX, "sk-openinno-form.hwpx")
        p = os.path.join(self.tmp, "stale-preview.hwpx")
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(p, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename == "Preview/PrvText.txt":
                    data = "\r\n".join(f"<본문에 없는 미리보기 줄 {i}>" for i in range(20)).encode("utf-8")
                zout.writestr(info, data)
        r = subprocess.run([sys.executable, SCRIPT, "convert", p, "--to", "docx", "-o",
                            os.path.join(self.tmp, "out.docx")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("preview_coverage", r.stdout)

    def test_cli_convert_docx(self):
        out = os.path.join(self.tmp, "form.docx")
        r = subprocess.run([sys.executable, SCRIPT, "convert", os.path.join(FIX, "sk-openinno-form.hwpx"),
                            "--to", "docx", "-o", out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        if shutil.which("textutil"):  # macOS: an independent DOCX reader
            txt = subprocess.run(["textutil", "-convert", "txt", "-stdout", out], capture_output=True, text=True)
            self.assertIn("참가신청서", txt.stdout)

    def test_cli_exit_codes(self):
        r = subprocess.run([sys.executable, SCRIPT, "extract", os.path.join(FIX, "e-phi-design.hwp")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("예측경보시스템", r.stdout)
        r = subprocess.run([sys.executable, SCRIPT, "extract", __file__], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main()
