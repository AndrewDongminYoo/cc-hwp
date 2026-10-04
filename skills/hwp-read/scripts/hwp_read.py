#!/usr/bin/env python3
"""hwp_read — read HWP 5.0 / HWPX documents as Markdown, with no third-party deps.

Subcommands
  info     <file>                      format, flags, counts (JSON)
  extract  <file> [-o out.md] [--images-dir DIR] [--max-chars N]
                                       Markdown on stdout (or -o); stats JSON on stderr
  render   <file> [-o out.pdf] [--page N]
                                       layout-faithful PDF via `rhwp` when installed,
                                       otherwise the embedded first-page preview image

Exit codes: 0 ok, 1 runtime error, 2 usage, 3 unsupported/protected document,
            4 extracted but suspicious (low preview coverage) — output is still written.

When the `rhwp` CLI (github.com/edwardkim/rhwp) is on PATH, prefer it for rendering;
this script's own parser is the dependency-free path that works anywhere Python 3.8+ runs.
"""
from __future__ import annotations

import argparse
import collections
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
import xml.etree.ElementTree as ET
import xml.parsers.expat
from dataclasses import dataclass, field
from typing import List, Optional, Union


class Unsupported(Exception):
    """Document can't be read by this parser (protected, legacy format, ...)."""


# Documents come from the internet: cap every decompressed part (ZIP member or
# HWP5 stream), and the sum over one document, so a small archive cannot
# inflate into an out-of-memory crash.
MAX_PART_BYTES = 128 * 1024 * 1024
MAX_DOC_BYTES = 256 * 1024 * 1024
INFLATE_CHUNK = 1024 * 1024  # inflate in steps so work is charged even if zlib then fails


def _looks_stored(raw: bytes) -> bool:
    """BinData image saved without compression (JPEG, PNG, GIF, BMP)."""
    return raw.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM"))


class _Budget:
    """Decompressed bytes one document may still produce across all of its parts."""

    def __init__(self):
        self.left = MAX_DOC_BYTES

    def limit(self) -> int:
        return min(MAX_PART_BYTES, self.left)

    def spend(self, n: int) -> None:
        self.left -= n


def _too_large(what: str, limit: int) -> Unsupported:
    return Unsupported(f"{what}의 압축 해제 크기가 남은 한도({limit} 바이트)를 넘습니다. "
                       "손상되었거나 압축 폭탄일 수 있는 문서라 읽지 않습니다.")


def _inflate(raw: bytes, what: str, budget: _Budget) -> bytes:
    limit = budget.limit()
    d = zlib.decompressobj(-15)
    out = bytearray()
    data = raw
    # Inflate in chunks and charge each one as it is produced, so a stream that is
    # later rejected (truncated, invalid block, oversized) still pays for its work.
    while not d.eof:
        want = min(INFLATE_CHUNK, limit + 1 - len(out))  # >= 1: zlib reads max_length=0 as "no limit"
        try:
            chunk = d.decompress(data, want)
        except zlib.error:
            budget.spend(want)  # zlib may have produced up to `want` bytes before failing
            raise
        budget.spend(len(chunk))
        out += chunk
        if len(out) > limit:
            raise _too_large(what, limit)
        data = d.unconsumed_tail
        if not data and len(chunk) < want:
            break  # input exhausted and zlib has nothing buffered
    if not d.eof:
        raise ValueError(f"{what}: 압축 스트림이 중간에 끊겼습니다 (손상된 문서).")
    return bytes(out)


def _zip_read(z: zipfile.ZipFile, name: str, budget: _Budget) -> bytes:
    limit = budget.limit()
    with z.open(name) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise _too_large(name, limit)
    budget.spend(len(data))
    return data


def _xml(data: bytes):
    """OWPML never declares a DTD; refusing one closes entity-expansion attacks.
    expat detects the document encoding itself, so this also holds for UTF-16 input."""
    def refuse(*_):
        raise Unsupported("HWPX XML에 DTD/엔티티 선언이 있습니다. 정상 HWPX에는 없는 구조라 읽지 않습니다.")

    p = xml.parsers.expat.ParserCreate()
    p.StartDoctypeDeclHandler = refuse
    p.EntityDeclHandler = refuse
    try:
        p.Parse(data, True)
    except xml.parsers.expat.ExpatError as e:
        raise ET.ParseError(str(e))
    return ET.fromstring(data)


# ───────────────────────────── intermediate representation ─────────────────────────────

@dataclass
class Para:
    text: str
    kind: str = "p"          # p | textbox


@dataclass
class Cell:
    row: int
    col: int
    rowspan: int
    colspan: int
    blocks: List["Block"]


@dataclass
class Table:
    rows: int
    cols: int
    cells: List[Cell]


Block = Union[Para, Table]


@dataclass
class Doc:
    fmt: str
    blocks: List[Block] = field(default_factory=list)
    notes: List[List[Block]] = field(default_factory=list)   # footnotes/endnotes
    images: List[str] = field(default_factory=list)          # BinData names referenced
    preview_text: str = ""
    meta: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {"equations": 0, "pictures": 0, "textboxes": 0})
    # The reader's decompression budget, so later reads of the same file (images) share it.
    budget: _Budget = field(default_factory=_Budget, repr=False, compare=False)


# ───────────────────────────── minimal CFB (OLE2) reader ─────────────────────────────

class CFB:
    """Just enough of MS-CFB to read named streams. Read-only, no olefile dependency."""

    FREESECT, ENDOFCHAIN = 0xFFFFFFFF, 0xFFFFFFFE

    def __init__(self, data: bytes):
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("not a CFB file")
        self.d = data
        self.ss = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mss = 1 << struct.unpack_from("<H", data, 0x20)[0]
        n_fat, first_dir = struct.unpack_from("<II", data, 0x2C)
        self.mini_cutoff, first_mfat, _n_mfat, first_difat, n_difat = struct.unpack_from("<IIIII", data, 0x38)
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        s = first_difat
        n_secs = len(data) // self.ss - 1
        seen: set = set()
        # The declared count is untrusted: stop at a repeated or out-of-file sector.
        for _ in range(min(n_difat, n_secs)):
            if s >= n_secs or s in seen:
                break
            seen.add(s)
            vals = struct.unpack_from(f"<{self.ss // 4}I", self._sec(s))
            difat.extend(vals[:-1])
            s = vals[-1]
        fat_secs = [x for x in difat if x < self.ENDOFCHAIN][:n_fat]
        self.fat: List[int] = []
        for fs in fat_secs:
            self.fat.extend(struct.unpack_from(f"<{self.ss // 4}I", self._sec(fs)))
        dir_bytes = self._chain(first_dir)
        self.entries = []
        for off in range(0, len(dir_bytes), 128):
            e = dir_bytes[off:off + 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[:max(nlen - 2, 0)].decode("utf-16-le", "replace")
            etype = e[66]
            left, right, child = struct.unpack_from("<III", e, 68)
            start, size = struct.unpack_from("<IQ", e, 116)
            if self.ss == 512:
                size &= 0xFFFFFFFF
            self.entries.append(dict(name=name, type=etype, left=left, right=right,
                                     child=child, start=start, size=size))
        root = self.entries[0]
        self.ministream = self._chain(root["start"])[:root["size"]] if root["start"] < self.ENDOFCHAIN else b""
        self.minifat: List[int] = []
        if first_mfat < self.ENDOFCHAIN:
            mf = self._chain(first_mfat)
            self.minifat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf))
        self.paths = {}
        self._seen: set = set()
        self._walk(root["child"], "")

    def _sec(self, n: int) -> bytes:
        o = (n + 1) * self.ss
        return self.d[o:o + self.ss]

    def _chain(self, start: int) -> bytes:
        out, s, seen = bytearray(), start, set()
        while s < self.ENDOFCHAIN and s not in seen and s < len(self.fat):
            seen.add(s)
            out += self._sec(s)
            s = self.fat[s]
        return bytes(out)

    def _minichain(self, start: int) -> bytes:
        out, s, seen = bytearray(), start, set()
        while s < self.ENDOFCHAIN and s not in seen and s < len(self.minifat):
            seen.add(s)
            out += self.ministream[s * self.mss:(s + 1) * self.mss]
            s = self.minifat[s]
        return bytes(out)

    def _walk(self, idx: int, prefix: str, depth: int = 0):
        stack = [idx]
        while stack:
            i = stack.pop()
            if i >= len(self.entries) or i >= self.ENDOFCHAIN or depth > 32 or i in self._seen:
                continue
            self._seen.add(i)  # malformed sibling/child links can form cycles
            e = self.entries[i]
            path = prefix + e["name"]
            self.paths[path] = e
            stack.extend([e["left"], e["right"]])
            if e["type"] == 1:  # storage
                self._walk(e["child"], path + "/", depth + 1)

    def exists(self, path: str) -> bool:
        return path in self.paths

    def read(self, path: str) -> bytes:
        e = self.paths[path]
        if e["size"] < self.mini_cutoff:
            return self._minichain(e["start"])[:e["size"]]
        return self._chain(e["start"])[:e["size"]]

    def list(self, prefix: str = "") -> List[str]:
        return sorted(p for p, e in self.paths.items() if e["type"] == 2 and p.startswith(prefix))


# ───────────────────────────── HWP 5.0 (binary) ─────────────────────────────

TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_CTRL_HEADER, TAG_LIST_HEADER = 0x42, 0x43, 0x47, 0x48
TAG_TABLE, TAG_SHAPE_PICTURE, TAG_EQEDIT = 0x4D, 0x55, 0x58
EXTENDED_CTRL = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
INLINE_CTRL = {4, 5, 6, 7, 8, 9, 19, 20}


@dataclass
class Rec:
    tag: int
    level: int
    data: bytes
    children: List["Rec"] = field(default_factory=list)


def _records(buf: bytes) -> List[Rec]:
    out, i = [], 0
    while i + 4 <= len(buf):
        h = struct.unpack_from("<I", buf, i)[0]
        i += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, h >> 20
        if size == 0xFFF:
            size = struct.unpack_from("<I", buf, i)[0]
            i += 4
        if i + size > len(buf):
            raise ValueError(f"레코드(tag {tag:#x})가 섹션 끝을 넘습니다 (손상되었거나 잘린 문서).")
        out.append(Rec(tag, level, buf[i:i + size]))
        i += size
    if i != len(buf):
        raise ValueError(f"섹션 끝에 불완전한 레코드 헤더 {len(buf) - i}바이트가 있습니다 (잘린 문서).")
    return out


def _tree(recs: List[Rec]) -> List[Rec]:
    roots: List[Rec] = []
    stack: List[Rec] = []
    for r in recs:
        while stack and stack[-1].level >= r.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(r)
        stack.append(r)
    return roots


def _utf16_char(w, i: int):
    """One character from UTF-16 code units at w[i]: (text, units consumed).
    Surrogate pairs combine into one code point; a lone surrogate becomes U+FFFD."""
    ch = w[i]
    if 0xD800 <= ch <= 0xDBFF and i + 1 < len(w) and 0xDC00 <= w[i + 1] <= 0xDFFF:
        return chr(0x10000 + ((ch - 0xD800) << 10) + (w[i + 1] - 0xDC00)), 2
    if 0xD800 <= ch <= 0xDFFF:
        return "�", 1
    return chr(ch), 1


def _ctrl_id(data: bytes) -> str:
    return data[:4][::-1].decode("latin-1") if len(data) >= 4 else ""


class Hwp5Reader:
    def __init__(self, data: bytes):
        self.cfb = CFB(data)
        self.budget = _Budget()
        hdr = self.cfb.read("FileHeader")
        if not hdr.startswith(b"HWP Document File"):
            raise Unsupported("CFB container but not an HWP 5.0 FileHeader")
        ver = struct.unpack_from("<I", hdr, 32)[0]
        self.flags = struct.unpack_from("<I", hdr, 36)[0]
        self.version = f"{(ver >> 24) & 0xFF}.{(ver >> 16) & 0xFF}.{(ver >> 8) & 0xFF}.{ver & 0xFF}"
        self.compressed = bool(self.flags & 0x01)
        self.doc = Doc("hwp5", budget=self.budget)
        self.doc.meta.update(version=self.version, compressed=self.compressed,
                             password=bool(self.flags & 0x02), distribution=bool(self.flags & 0x04))
        if self.flags & 0x02:
            raise Unsupported("암호가 걸린 HWP 문서입니다. 한컴오피스/HOP에서 암호를 풀어 다시 저장해야 합니다.")
        if self.flags & 0x04:
            raise Unsupported("배포용(복사·인쇄 제한) HWP 문서입니다. 본문(ViewText)이 암호화되어 있어 "
                              "이 파서로는 읽을 수 없습니다. `rhwp convert`로 풀거나 HOP에서 PDF로 내보내세요.")

    def _stream(self, path: str) -> bytes:
        raw = self.cfb.read(path)
        return _inflate(raw, path, self.budget) if self.compressed else raw

    def read(self) -> Doc:
        if self.cfb.exists("PrvText"):
            self.doc.preview_text = self.cfb.read("PrvText").decode("utf-16-le", "replace")
        n = 0
        while self.cfb.exists(f"BodyText/Section{n}"):
            roots = _tree(_records(self._stream(f"BodyText/Section{n}")))
            self.doc.blocks.extend(self._paras(roots))
            n += 1
        if n == 0:
            raise Unsupported("HWP 컨테이너에 본문 섹션(BodyText/Section0)이 없습니다.")
        self.doc.meta["sections"] = n
        self.doc.images = [p.split("/", 1)[1] for p in self.cfb.list("BinData/")]
        return self.doc

    def _paras(self, siblings: List[Rec]) -> List[Block]:
        out: List[Block] = []
        for r in siblings:
            if r.tag == TAG_PARA_HEADER:
                out.extend(self._para(r))
        return out

    def _para(self, ph: Rec) -> List[Block]:
        text_rec = next((c for c in ph.children if c.tag == TAG_PARA_TEXT), None)
        ctrls = [c for c in ph.children if c.tag == TAG_CTRL_HEADER]
        blocks: List[Block] = []
        buf: List[str] = []
        ci = 0
        if text_rec is None:
            return [Para("")]
        w = struct.unpack_from(f"<{len(text_rec.data) // 2}H", text_rec.data)
        i = 0
        while i < len(w):
            ch = w[i]
            if ch >= 32:
                s, n = _utf16_char(w, i)
                buf.append(s)
                i += n
                continue
            if ch in EXTENDED_CTRL:
                if ci < len(ctrls):
                    emitted = self._ctrl(ctrls[ci])
                    ci += 1
                    inline_txt = [b for b in emitted if isinstance(b, str)]
                    buf.extend(inline_txt)
                    real = [b for b in emitted if not isinstance(b, str)]
                    if real:
                        if "".join(buf).strip():
                            blocks.append(Para("".join(buf)))
                        buf = []
                        blocks.extend(real)
                i += 8
            elif ch in INLINE_CTRL:
                if ch == 9:
                    buf.append("\t")
                i += 8
            else:
                if ch == 10:
                    buf.append("\n")
                elif ch == 24:
                    buf.append("-")
                elif ch in (30, 31):
                    buf.append(" ")
                i += 1  # 13 = paragraph end, others ignored
        if buf or not blocks:
            blocks.append(Para("".join(buf)))
        return blocks

    def _ctrl(self, c: Rec) -> list:
        cid = _ctrl_id(c.data)
        if cid == "tbl ":
            return [self._table(c)]
        if cid in ("fn  ", "en  "):
            body = self._paras(c.children)
            self.doc.notes.append(body)
            return [f"[^{len(self.doc.notes)}]"]
        if cid == "eqed":
            self.doc.counters["equations"] += 1
            eq = _find(c, TAG_EQEDIT)
            script = ""
            if eq is not None and len(eq.data) >= 6:
                ln = struct.unpack_from("<H", eq.data, 4)[0]
                script = eq.data[6:6 + ln * 2].decode("utf-16-le", "replace")
            return [f" [수식: {script.strip()}] " if script.strip() else " [수식] "]
        if cid == "gso ":
            out: list = []
            if _find(c, TAG_SHAPE_PICTURE) is not None:
                self.doc.counters["pictures"] += 1
                out.append(" [그림] ")
            for lst in _find_all_lists(c):
                self.doc.counters["textboxes"] += 1
                for b in self._paras(lst):
                    if isinstance(b, Para):
                        b.kind = "textbox"
                    out.append(b)
            return out
        if cid in ("head", "foot"):
            return []  # page furniture: skip
        return []

    def _table(self, c: Rec) -> Table:
        t = _find(c, TAG_TABLE)
        rows, cols = (struct.unpack_from("<HH", t.data, 4) if t is not None and len(t.data) >= 8 else (0, 0))
        cells: List[Cell] = []
        cur: Optional[Cell] = None
        paras: List[Rec] = []
        for ch in c.children:
            if ch.tag == TAG_LIST_HEADER:
                if cur is not None:
                    cur.blocks = self._paras(paras)
                    cells.append(cur)
                col, row, cs, rs = struct.unpack_from("<HHHH", ch.data, 8) if len(ch.data) >= 16 else (0, 0, 1, 1)
                cur, paras = Cell(row, col, max(rs, 1), max(cs, 1), []), []
            elif ch.tag == TAG_PARA_HEADER and cur is not None:
                paras.append(ch)
        if cur is not None:
            cur.blocks = self._paras(paras)
            cells.append(cur)
        return Table(rows, cols, cells)


def _find(r: Rec, tag: int) -> Optional[Rec]:
    for ch in r.children:
        if ch.tag == tag:
            return ch
        f = _find(ch, tag)
        if f is not None:
            return f
    return None


def _find_all_lists(r: Rec) -> List[List[Rec]]:
    """Paragraph runs that follow a LIST_HEADER anywhere under a drawing object (text boxes)."""
    found: List[List[Rec]] = []

    def walk(node: Rec):
        sib = node.children
        idx = 0
        while idx < len(sib):
            if sib[idx].tag == TAG_LIST_HEADER:
                run = []
                idx += 1
                while idx < len(sib) and sib[idx].tag == TAG_PARA_HEADER:
                    run.append(sib[idx])
                    idx += 1
                if run:
                    found.append(run)
                # Don't descend into the run: _paras handles tables nested in it.
            else:
                walk(sib[idx])
                idx += 1

    walk(r)
    return found


# ───────────────────────────── HWPX (OWPML) ─────────────────────────────

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HC = "{http://www.hancom.co.kr/hwpml/2011/core}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _outer_sublists(el) -> list:
    """hp:subList elements under el that are not nested inside another subList;
    nested ones (table cells, inner drawings) are reached when the outer list is parsed."""
    out = []
    for ch in el:
        if ch.tag == HP + "subList":
            out.append(ch)
        else:
            out.extend(_outer_sublists(ch))
    return out


class HwpxReader:
    def __init__(self, data: bytes, path: str):
        import io
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.budget = _Budget()
        names = set(self.z.namelist())
        mime = _zip_read(self.z, "mimetype", self.budget).decode(errors="replace").strip() if "mimetype" in names else ""
        if mime and "hwp" not in mime:
            raise Unsupported(f"ZIP 컨테이너지만 HWPX가 아닙니다 (mimetype={mime})")
        if not mime and "Contents/content.hpf" not in names:
            raise Unsupported("ZIP 컨테이너지만 HWPX 패키지 표지(mimetype, Contents/content.hpf)가 없습니다.")
        manifest = _zip_read(self.z, "META-INF/manifest.xml", self.budget).decode(errors="replace") if "META-INF/manifest.xml" in names else ""
        if "encryption-data" in manifest:
            raise Unsupported("암호화된 HWPX 문서입니다.")
        self.doc = Doc("hwpx", budget=self.budget)
        self.doc.meta["mimetype"] = mime

    def _package(self):
        """content.hpf parsed once (it is charged to the budget), or None if absent or unreadable."""
        try:
            return _xml(_zip_read(self.z, "Contents/content.hpf", self.budget))
        except Unsupported:
            raise
        except Exception:
            return None

    def _sections(self, hpf) -> List[str]:
        names = self.z.namelist()
        if hpf is not None:
            items = {i.get("id"): i.get("href") for i in hpf.iter() if _local(i.tag) == "item"}
            spine = [items.get(r.get("idref")) for r in hpf.iter() if _local(r.tag) == "itemref"]
            secs = [s for s in spine if s and re.search(r"section\d+\.xml$", s)]
            if secs:
                return [s if s in names else "Contents/" + s.split("/")[-1] for s in secs]
        return sorted((n for n in names if re.match(r"Contents/section\d+\.xml$", n)),
                      key=lambda s: int(re.findall(r"\d+", s)[-1]))

    def read(self) -> Doc:
        names = set(self.z.namelist())
        if "Preview/PrvText.txt" in names:
            raw = _zip_read(self.z, "Preview/PrvText.txt", self.budget)
            for enc in ("utf-8", "utf-16"):
                try:
                    self.doc.preview_text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        hpf = self._package()
        for m in (hpf.iter() if hpf is not None else ()):
            if _local(m.tag) == "title" and (m.text or "").strip():
                self.doc.meta["title"] = m.text.strip()
        secs = self._sections(hpf)
        if not secs:
            raise Unsupported("HWPX 패키지에 본문 섹션(Contents/sectionN.xml)이 없습니다.")
        for s in secs:
            root = _xml(_zip_read(self.z, s, self.budget))
            self.doc.blocks.extend(self._paras(root))
        self.doc.meta["sections"] = len(secs)
        self.doc.images = sorted(n.split("/", 1)[1] for n in names if n.startswith("BinData/"))
        return self.doc

    def _paras(self, container) -> List[Block]:
        out: List[Block] = []
        for p in container:
            if p.tag == HP + "p":
                out.extend(self._para(p))
        return out

    def _para(self, p) -> List[Block]:
        blocks: List[Block] = []
        buf: List[str] = []

        def flush():
            nonlocal buf
            if "".join(buf).strip():
                blocks.append(Para("".join(buf)))
            buf = []

        for run in p:
            if run.tag != HP + "run":
                continue
            for el in run:
                name = _local(el.tag)
                if name == "t":
                    buf.append(self._t(el))
                elif name == "tbl":
                    flush()
                    blocks.append(self._table(el))
                elif name == "equation":
                    self.doc.counters["equations"] += 1
                    sc = el.find(HP + "script")
                    s = (sc.text or "").strip() if sc is not None else ""
                    buf.append(f" [수식: {s}] " if s else " [수식] ")
                elif name == "pic":
                    self.doc.counters["pictures"] += 1
                    buf.append(" [그림] ")
                elif name == "ctrl":
                    for note in el:
                        if _local(note.tag) in ("footNote", "endNote"):
                            sub = note.find(HP + "subList")
                            body = self._paras(sub) if sub is not None else []
                            self.doc.notes.append(body)
                            buf.append(f"[^{len(self.doc.notes)}]")
                        # header/footer, fields, bookmarks: skip
                elif name in ("secPr", "colPr", "linesegarray"):
                    continue
                else:
                    # drawing objects (rect, ellipse, container, ...) may hold text boxes
                    subs = _outer_sublists(el)
                    if subs:
                        flush()
                        for sub in subs:
                            self.doc.counters["textboxes"] += 1
                            for b in self._paras(sub):
                                if isinstance(b, Para):
                                    b.kind = "textbox"
                                blocks.append(b)
        if buf or not blocks:
            blocks.append(Para("".join(buf)))
        return blocks

    @staticmethod
    def _t(t) -> str:
        parts = [t.text or ""]
        for ch in t:
            n = _local(ch.tag)
            if n == "tab":
                parts.append("\t")
            elif n == "lineBreak":
                parts.append("\n")
            elif n in ("nbSpace", "fwSpace"):
                parts.append(" ")
            elif n == "hyphen":
                parts.append("-")
            parts.append(ch.tail or "")
        return "".join(parts)

    def _table(self, tbl) -> Table:
        rows = int(tbl.get("rowCnt", "0") or 0)
        cols = int(tbl.get("colCnt", "0") or 0)
        cells: List[Cell] = []
        for tr in tbl.findall(HP + "tr"):
            for tc in tr.findall(HP + "tc"):
                addr = tc.find(HP + "cellAddr")
                span = tc.find(HP + "cellSpan")
                sub = tc.find(HP + "subList")
                cells.append(Cell(
                    int(addr.get("rowAddr", 0)) if addr is not None else 0,
                    int(addr.get("colAddr", 0)) if addr is not None else 0,
                    max(int(span.get("rowSpan", 1)), 1) if span is not None else 1,
                    max(int(span.get("colSpan", 1)), 1) if span is not None else 1,
                    self._paras(sub) if sub is not None else [],
                ))
        return Table(rows, cols, cells)


# ───────────────────────────── Markdown rendering ─────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[ \t 　]+", " ", s).strip()


def _cell_text(blocks: List[Block], html_mode: bool) -> str:
    parts = []
    for b in blocks:
        if isinstance(b, Para):
            t = _norm(b.text.replace("\n", " "))
            if t:
                parts.append(html.escape(t) if html_mode else t.replace("|", "\\|"))
        else:
            parts.append(_table_html(b))
    return ("<br>" if html_mode else " <br> ").join(parts)


def _simple(t: Table) -> bool:
    return all(c.rowspan == 1 and c.colspan == 1 and not any(isinstance(b, Table) for b in c.blocks)
               for c in t.cells)


def _is_layout(t: Table) -> bool:
    """One-row or one-column tables without nesting are almost always layout boxes
    (title banners, chapter headers, notice boxes) rather than data."""
    if not t.cells or any(isinstance(b, Table) for c in t.cells for b in c.blocks):
        return False
    nrows = len({c.row for c in t.cells})
    ncols = len({c.col for c in t.cells})
    return nrows == 1 or ncols == 1


def _flatten(t: Table) -> str:
    texts = [_cell_text(c.blocks, False).replace(" <br> ", "\n")
             for c in sorted(t.cells, key=lambda c: (c.row, c.col))]
    texts = [x for x in texts if x.strip()]
    one_row = len({c.row for c in t.cells}) == 1
    return (" | " if one_row else "\n").join(texts)


def _table_md(t: Table) -> str:
    if not t.cells:
        return ""
    if _is_layout(t):
        return _flatten(t)
    if not _simple(t) or t.rows < 2:
        return _table_html(t)
    nrows = max(c.row for c in t.cells) + 1
    ncols = max(c.col for c in t.cells) + 1
    grid = [["" for _ in range(ncols)] for _ in range(nrows)]
    for c in t.cells:
        grid[c.row][c.col] = _cell_text(c.blocks, False)
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * ncols]
    lines += ["| " + " | ".join(r) + " |" for r in grid[1:]]
    return "\n".join(lines)


def _table_html(t: Table) -> str:
    rows = {}
    for c in sorted(t.cells, key=lambda c: (c.row, c.col)):
        rows.setdefault(c.row, []).append(c)
    out = ["<table>"]
    last = max(c.row + c.rowspan - 1 for c in t.cells) if t.cells else -1
    # A row fully covered by rowspans has no starting cell but still needs its <tr>,
    # or the cells of later rows shift into the merged area.
    for r in range(last + 1):
        tds = []
        for c in rows.get(r, []):
            attrs = (f' rowspan="{c.rowspan}"' if c.rowspan > 1 else "") + \
                    (f' colspan="{c.colspan}"' if c.colspan > 1 else "")
            tds.append(f"<td{attrs}>{_cell_text(c.blocks, True)}</td>")
        out.append("<tr>" + "".join(tds) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def to_markdown(doc: Doc) -> str:
    out: List[str] = []
    blank = 0
    for b in doc.blocks:
        if isinstance(b, Table):
            md_t = _table_md(b)
            if md_t.strip():
                out.append(md_t)
                out.append("")
                blank = 1
            continue
        lines = [_norm(ln) for ln in b.text.split("\n")]
        txt = "\n".join(lines).strip("\n")
        if not txt.strip():
            blank += 1
            if blank == 1:
                out.append("")
            continue
        blank = 0
        if b.kind == "textbox":
            txt = "\n".join("> " + ln for ln in txt.split("\n"))
        out.append(txt)
    if doc.notes:
        out.append("")
        for i, body in enumerate(doc.notes, 1):
            parts = [_norm(x.text.replace("\n", " ")) if isinstance(x, Para) else _table_html(x).replace("\n", "")
                     for x in body]
            note = " ".join(p for p in parts if p)
            out.append(f"[^{i}]: {note}")
    md = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


# ───────────────────────────── verification ─────────────────────────────

def _squash(s: str) -> str:
    return re.sub(r"[\s 　<>|`*#>\-]+", "", html.unescape(re.sub(r"<[^>]+>", "", s)))


PRVTEXT_CAP = 1000  # Hancom truncates PrvText at about 1K characters (1,023 in both fixtures)


def preview_coverage(preview: str, md: str) -> Optional[float]:
    """Share of the embedded preview text (PrvText, written by the authoring app)
    that also appears in our extraction. The preview is a truncated, independent
    rendering of the first ~1-2K chars, so it's a free ground truth for the head."""
    if not preview.strip():
        return None
    body = _squash(md)
    chunks = [c for c in (_squash(x) for x in re.split(r"[<>\r\n]+", preview)) if len(c) >= 4]
    if not chunks:
        return None
    if len(preview) >= PRVTEXT_CAP:
        chunks = chunks[:-1] or chunks  # cut mid-chunk at the size limit; a shorter preview is complete
    # A line repeated in the preview must appear that many times in the output.
    need = collections.Counter(chunks)
    hit = sum(min(k, body.count(c)) for c, k in need.items())
    return round(hit / len(chunks), 3)


def stats(doc: Doc, md: str) -> dict:
    def walk(blocks, acc):
        for b in blocks:
            if isinstance(b, Table):
                acc["tables"] += 1
                if not _simple(b):
                    acc["merged_tables"] += 1
                for c in b.cells:
                    walk(c.blocks, acc)
            elif b.text.strip():
                acc["paragraphs"] += 1
        return acc

    acc = walk(doc.blocks, {"paragraphs": 0, "tables": 0, "merged_tables": 0})
    cov = preview_coverage(doc.preview_text, md)
    s = {"format": doc.fmt, **doc.meta, **acc, **doc.counters,
         "notes": len(doc.notes), "embedded_files": len(doc.images),
         "chars": len(md), "preview_coverage": cov}
    warn = []
    if cov is not None and cov < 0.9:
        warn.append(f"preview_coverage {cov} < 0.9 — 앞부분 텍스트 일부가 누락됐을 수 있음. render 경로로 교차 확인 권장")
    if acc["paragraphs"] == 0 and acc["tables"] == 0:
        warn.append("본문이 비어 있음 — 이미지/스캔 문서이거나 파서가 지원하지 않는 구조")
    if doc.counters["equations"]:
        warn.append("수식은 원문 스크립트(한컴 수식 문법)로만 표기됨")
    s["warnings"] = warn
    return s


# ───────────────────────────── entry points ─────────────────────────────

def detect(data: bytes) -> str:
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "hwp5"
    if data[:4] == b"PK\x03\x04":
        return "hwpx"
    if data[:17] == b"HWP Document File":
        return "hwp3"
    if data.lstrip()[:5] in (b"<?xml", b"<HWPM"):
        return "hml"
    return "unknown"


def load(path: str) -> Doc:
    with open(path, "rb") as f:
        data = f.read()
    kind = detect(data)
    if kind == "hwp5":
        return Hwp5Reader(data).read()
    if kind == "hwpx":
        return HwpxReader(data, path).read()
    if kind == "hwp3":
        raise Unsupported("HWP 3.x(1990년대) 형식입니다. rhwp CLI(`rhwp export-markdown`)를 사용하세요.")
    if kind == "hml":
        raise Unsupported("HML(XML) 형식입니다. rhwp CLI를 사용하세요.")
    raise Unsupported("HWP/HWPX 시그니처가 아닙니다 (확장자와 무관하게 내용으로 판별함).")


def _safe_dest(outdir: str, member: str) -> Optional[str]:
    """Destination for an embedded file, or None if its name would leave outdir.
    Container entry names come from the document and must not be trusted as paths."""
    base = member.replace("\\", "/").rsplit("/", 1)[-1]
    if base in ("", ".", ".."):
        return None
    dst = os.path.join(outdir, base)
    root = os.path.realpath(outdir)
    if not os.path.realpath(dst).startswith(root + os.sep):
        return None
    return dst


def save_images(path: str, doc: Doc, outdir: str) -> List[str]:
    os.makedirs(outdir, exist_ok=True)
    with open(path, "rb") as f:
        data = f.read()
    written = []
    if doc.fmt == "hwp5":
        r = Hwp5Reader(data)
        for p in r.cfb.list("BinData/"):
            raw = r.cfb.read(p)
            try:
                if r.compressed and not _looks_stored(raw):
                    raw = _inflate(raw, p, doc.budget)
            except (zlib.error, ValueError):
                pass  # not a valid deflate stream: keep the stored bytes (the failed attempt was charged)
            dst = _safe_dest(outdir, p)
            if dst is None:
                continue
            with open(dst, "wb") as f:
                f.write(raw)
            written.append(dst)
    else:
        z = zipfile.ZipFile(path)
        for n in z.namelist():
            if n.startswith("BinData/"):
                dst = _safe_dest(outdir, n)
                if dst is None:
                    continue
                with open(dst, "wb") as f:
                    f.write(_zip_read(z, n, doc.budget))
                written.append(dst)
    return written


def cmd_info(a) -> int:
    doc = load(a.file)
    md = to_markdown(doc)
    print(json.dumps(stats(doc, md), ensure_ascii=False, indent=2))
    return 0


def cmd_extract(a) -> int:
    doc = load(a.file)
    md = to_markdown(doc)
    st = stats(doc, md)
    if a.max_chars and len(md) > a.max_chars:
        omitted = len(md) - a.max_chars
        md = md[:a.max_chars] + f"\n\n<!-- truncated: {omitted} chars omitted; rerun without --max-chars -->\n"
        st["truncated"], st["omitted_chars"] = True, omitted
    if a.images_dir:
        st["images_written"] = save_images(a.file, doc, a.images_dir)
    if a.output:
        with open(a.output, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        sys.stdout.write(md)
    sys.stderr.write(json.dumps(st, ensure_ascii=False) + "\n")
    return 4 if st["warnings"] and st.get("preview_coverage") is not None and st["preview_coverage"] < 0.9 else 0


def cmd_render(a) -> int:
    rhwp = shutil.which("rhwp")
    out = a.output or os.path.splitext(os.path.basename(a.file))[0] + ".pdf"
    if rhwp:
        cmd = [rhwp, "export-pdf", a.file, "-o", out, "--json"]
        if a.page is not None:
            cmd += ["-p", str(a.page)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode
    # Fallback: the authoring app's own first-page thumbnail.
    with open(a.file, "rb") as f:
        data = f.read()
    kind = detect(data)
    img = None
    if kind == "hwp5":
        c = CFB(data)
        img = c.read("PrvImage") if c.exists("PrvImage") else None
    elif kind == "hwpx":
        z = zipfile.ZipFile(a.file)
        n = next((n for n in z.namelist() if n.startswith("Preview/PrvImage")), None)
        img = _zip_read(z, n, _Budget()) if n else None
    if not img:
        sys.stderr.write("rhwp가 없고 문서에 미리보기 이미지도 없습니다.\n")
        return 3
    ext = ".png" if img[:4] == b"\x89PNG" else ".gif" if img[:3] == b"GIF" else ".bmp" if img[:2] == b"BM" else ".img"
    dst = os.path.splitext(out)[0] + "_preview" + ext
    with open(dst, "wb") as f:
        f.write(img)
    print(json.dumps({"backend": "embedded-preview", "output": dst, "pages": [0],
                      "note": "첫 쪽 썸네일만 있음. 전체 쪽 렌더링은 rhwp 설치 필요"}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hwp_read", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("file"); p.set_defaults(fn=cmd_info)
    p = sub.add_parser("extract"); p.add_argument("file"); p.add_argument("-o", "--output")
    p.add_argument("--images-dir"); p.add_argument("--max-chars", type=int)
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("render"); p.add_argument("file"); p.add_argument("-o", "--output")
    p.add_argument("--page", type=int); p.set_defaults(fn=cmd_render)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except Unsupported as e:
        sys.stderr.write(json.dumps({"error": "unsupported", "message": str(e)}, ensure_ascii=False) + "\n")
        return 3
    except (zipfile.BadZipFile, zlib.error, struct.error, ET.ParseError, KeyError, ValueError) as e:
        sys.stderr.write(json.dumps({"error": "parse", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
