"""Regression tests: run with `python3 -m unittest discover tests` (stdlib only)."""
import collections
import html
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "skills", "hwp-read", "scripts", "hwp_read.py")
sys.path.insert(0, os.path.dirname(SCRIPT))
import hwp_read as h  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


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

    def test_cli_exit_codes(self):
        r = subprocess.run([sys.executable, SCRIPT, "extract", os.path.join(FIX, "e-phi-design.hwp")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("예측경보시스템", r.stdout)
        r = subprocess.run([sys.executable, SCRIPT, "extract", __file__], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)


if __name__ == "__main__":
    unittest.main()
