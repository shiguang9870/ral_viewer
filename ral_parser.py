"""
SystemVerilog / UVM RAL 寄存器模型解析器
========================================

从 ``*_ral_pkg.sv`` 之类的寄存器模型文件里抽出 **block / reg / reg_field** 三级结构。

设计要点——**对自定义继承类保持通用**：

    class uart_ctrl_reg       extends dv_base_reg;        // dv_base_reg       extends uvm_reg
    class uart_reg_block      extends dv_base_reg_block;  // ..._reg_block     extends uvm_reg_block
    class dv_base_reg_field   extends uvm_reg_field;

解析器不硬编码任何厂商前缀，而是三级判定：

1. **继承链**：``class A extends B`` 全局建图，递归解析 B 的类别（本文件没定义也能按名字判）；
2. **命名规则**：``*_reg_block / *_block`` → block，``*_reg_field / *_field`` → field，
   ``*_reg`` → reg，``*_mem`` → mem（覆盖基类定义在别处的常见情况）；
3. **类体特征**：含 ``create_map/add_reg/add_submap/set_hdl_path_root`` → block；
   含 ``configure(this, ...)`` 或 ``super.new(name, <int>, ...)`` → reg；
   都没命中才记为 unknown 并给出告警——绝不因为认不出基类而丢内容。

字段信息由三处互补拼出：成员声明（名字/类型/行号）、``type_id::create``（实例名）、
``configure(parent, size, lsb, access, volatile, reset, has_reset, is_rand, individually_accessible)``
（位宽/位置/权限/复位值）。

命令行自测::

    python ral_parser.py uart_ral_pkg.sv --dump
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field as _field
from pathlib import Path
from typing import Iterable, Sequence

# =============================================================== 类别常量

KIND_BLOCK = "block"
KIND_REG = "reg"
KIND_FIELD = "field"
KIND_MEM = "mem"
KIND_UNKNOWN = "unknown"

KIND_LABEL = {
    KIND_BLOCK: "block",
    KIND_REG: "reg",
    KIND_FIELD: "reg_field",
    KIND_MEM: "mem",
    KIND_UNKNOWN: "unknown",
}

#: UVM 标准基类（无需在文件里定义就能判定类别）
_BUILTIN_KIND = {
    "uvm_reg_block": KIND_BLOCK,
    "uvm_reg": KIND_REG,
    "uvm_reg_field": KIND_FIELD,
    "uvm_vreg": KIND_REG,
    "uvm_vreg_field": KIND_FIELD,
    "uvm_mem": KIND_MEM,
}

#: 命名规则（顺序敏感：block / field 必须先于 reg 判断）
_NAME_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:_reg_block|_blk|_block)$", re.I), KIND_BLOCK),
    (re.compile(r"(?:_reg_field|_field|_fld)$", re.I), KIND_FIELD),
    (re.compile(r"(?:_reg|_regs)$", re.I), KIND_REG),
    (re.compile(r"(?:_mem|_memory)$", re.I), KIND_MEM),
)

_KNOWN_ACCESS = {
    "RO", "RW", "WO", "RC", "WC", "RS", "WS", "WRC", "WRS",
    "W1C", "W0C", "W1S", "W0S", "W1T", "W0T", "W1SRC", "W0SRC", "WOC", "WOS", "NOACCESS",
}


# =============================================================== 数据结构


@dataclass
class FieldDef:
    """一个 uvm_reg_field（或自定义派生字段）。"""

    name: str = ""
    cls: str = "uvm_reg_field"
    width: int = 1
    lsb: int = 0
    access: str = "RW"
    volatile: int = 0
    reset_raw: str = ""
    reset_value: int | None = None
    has_reset: int = 1
    is_rand: int = 0
    individually_accessible: int = 0
    doc: str = ""
    line: int = 0
    file: str = ""
    reg: "RegDef | None" = None

    @property
    def kind(self) -> str:
        return KIND_FIELD

    @property
    def msb(self) -> int:
        return self.lsb + max(self.width, 1) - 1

    @property
    def bit_range(self) -> str:
        """``[8:7]`` / ``[3]`` 形式的位域文本。"""
        return f"[{self.lsb}]" if self.width <= 1 else f"[{self.msb}:{self.lsb}]"

    @property
    def reset_text(self) -> str:
        return self.reset_raw or ("" if self.reset_value is None else str(self.reset_value))

    @property
    def flags(self) -> str:
        bits = []
        if self.is_rand:
            bits.append("rand")
        if self.volatile:
            bits.append("volatile")
        if not self.has_reset:
            bits.append("no_reset")
        if self.individually_accessible:
            bits.append("indiv")
        return " ".join(bits)


@dataclass
class RegDef:
    """一个 uvm_reg（或自定义派生寄存器）在某个 block 中的实例。"""

    cls: str
    inst_name: str = ""
    base: str = ""
    width: int = 32
    fields: list[FieldDef] = _field(default_factory=list)
    offset: int | None = None
    offset_raw: str = ""
    access: str = ""
    hdl_path: str = ""
    doc: str = ""
    line: int = 0
    file: str = ""
    block: "BlockDef | None" = None
    notes: list[str] = _field(default_factory=list)

    @property
    def kind(self) -> str:
        return KIND_REG

    @property
    def display_name(self) -> str:
        return self.inst_name or self.cls

    @property
    def block_name(self) -> str:
        return self.block.cls if self.block else "(unmounted)"

    @property
    def effective_access(self) -> str:
        """add_reg 里没写权限时，由字段权限推断。"""
        if self.access:
            return self.access
        accs = {f.access.upper() for f in self.fields if f.access}
        if not accs:
            return ""
        if accs <= {"RO", "RC"}:
            return "RO"
        if accs <= {"WO", "WOC"}:
            return "WO"
        return "RW"

    @property
    def reset_value(self) -> int | None:
        """按字段复位值拼出整寄存器复位值。"""
        if not self.fields:
            return None
        total = 0
        for f in self.fields:
            if f.reset_value is None:
                return None
            total |= (f.reset_value & ((1 << max(f.width, 1)) - 1)) << f.lsb
        return total

    @property
    def reset_text(self) -> str:
        val = self.reset_value
        if val is None:
            return ""
        digits = max(2, (self.width + 3) // 4)
        return f"0x{val:0{digits}X}"

    @property
    def covered_bits(self) -> int:
        return sum(max(f.width, 1) for f in self.fields)

    @property
    def uncovered_bits(self) -> int:
        """保留位（未被任何字段覆盖的位）数量。"""
        return max(0, self.width - self.covered_bits)


@dataclass
class MemDef:
    """一个 uvm_mem 实例（顺带解析，便于看清 block 全貌）。"""

    name: str
    cls: str = "uvm_mem"
    depth: int | None = None
    width: int | None = None
    access: str = ""
    offset: int | None = None
    hdl_path: str = ""
    line: int = 0
    file: str = ""

    @property
    def kind(self) -> str:
        return KIND_MEM

    @property
    def size_text(self) -> str:
        if self.depth is None and self.width is None:
            return ""
        return f"{self.depth if self.depth is not None else '?'} × {self.width if self.width is not None else '?'} bit"

    @property
    def offset_text(self) -> str:
        return "" if self.offset is None else f"0x{self.offset:X}"


@dataclass
class BlockDef:
    """一个 uvm_reg_block（或自定义派生 block）。"""

    cls: str
    base: str = ""
    regs: list[RegDef] = _field(default_factory=list)
    mems: list[MemDef] = _field(default_factory=list)
    subblocks: list[str] = _field(default_factory=list)
    submaps: list[str] = _field(default_factory=list)
    map_name: str = ""
    map_base: int | None = None
    map_addr_bytes: int | None = None
    map_endian: str = ""
    hdl_path_root: str = ""
    locked: bool = False
    doc: str = ""
    line: int = 0
    file: str = ""
    notes: list[str] = _field(default_factory=list)

    @property
    def kind(self) -> str:
        return KIND_BLOCK

    @property
    def field_count(self) -> int:
        return sum(len(r.fields) for r in self.regs)

    @property
    def is_abstract(self) -> bool:
        """没有任何寄存器实例——通常是被继承的抽象基类。"""
        return not self.regs and not self.mems

    @property
    def map_text(self) -> str:
        if not self.map_name:
            return ""
        parts = [self.map_name]
        if self.map_base is not None:
            parts.append(f"base=0x{self.map_base:X}")
        if self.map_addr_bytes is not None:
            parts.append(f"{self.map_addr_bytes}B")
        if self.map_endian:
            parts.append(self.map_endian)
        return " ".join(parts)


@dataclass
class ParseResult:
    """一次多文件解析的全部产物。"""

    files: list[str] = _field(default_factory=list)
    packages: list[str] = _field(default_factory=list)
    blocks: list[BlockDef] = _field(default_factory=list)
    loose_regs: list[RegDef] = _field(default_factory=list)
    kinds: dict[str, str] = _field(default_factory=dict)
    warnings: list[str] = _field(default_factory=list)
    #: 硬错误：文件读不出来、内容完全没法解析等（界面上会把这些写进 Debug 面板并标红）
    errors: list[str] = _field(default_factory=list)
    #: 实际生效的宏表（`` `define `` + 内置默认值），Debug 面板里会列出来
    macros: dict[str, str] = _field(default_factory=dict)

    @property
    def all_regs(self) -> list[RegDef]:
        out: list[RegDef] = []
        for blk in self.blocks:
            out.extend(blk.regs)
        out.extend(self.loose_regs)
        return out

    @property
    def field_count(self) -> int:
        return sum(len(r.fields) for r in self.all_regs)

    def regs_of(self, block_name: str) -> list[RegDef]:
        if block_name == LOOSE_BLOCK:
            return list(self.loose_regs)
        for blk in self.blocks:
            if blk.cls == block_name:
                return list(blk.regs)
        return []

    def summary(self) -> str:
        return (
            f"{len(self.files)} file(s) / {len(self.blocks)} block(s) / "
            f"{len(self.all_regs)} register(s) / {self.field_count} field(s)"
        )


#: 未被任何 block 实例化的寄存器，在界面上归到这一“伪 block”下
LOOSE_BLOCK = "(Unmounted Registers)"


# =============================================================== 词法工具


def _blank(seq: list[str], start: int, end: int) -> None:
    """把 [start, end) 挖成空格，但保留换行以保证行号不变。"""
    for k in range(start, end):
        if seq[k] != "\n":
            seq[k] = " "


def mask_comments(text: str) -> tuple[str, dict[int, list[tuple[bool, str]]]]:
    """
    把注释挖成空格（保留字符偏移与行号）。

    返回 ``(masked_text, comments)``，其中 ``comments[行号(1基)] = [(是否独占一行, 文本), ...]``。
    """
    out = list(text)
    comments: dict[int, list[tuple[bool, str]]] = {}
    i, n, line = 0, len(text), 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch == '"':  # 跳过字符串字面量，避免里面的 // 被当成注释
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                if i < n and text[i] == "\n":
                    line += 1
                i += 1
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            line_start = text.rfind("\n", 0, i) + 1
            pure = text[line_start:i].strip() == ""
            comments.setdefault(line, []).append((pure, text[i:j].strip()))
            _blank(out, i, j)
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if text[k] == "\n":
                    line += 1
            _blank(out, i, j)
            i = j
            continue
        i += 1
    return "".join(out), comments


_SEP_LINE = re.compile(r"^[\s\-=*/*#]*$")


def doc_above(orig_lines: Sequence[str], line_no: int, max_lines: int = 12) -> str:
    """取第 line_no 行（1 基）上方连续的 ``//`` 注释，拼成一行说明。"""
    buf: list[str] = []
    idx = line_no - 2  # 0 基，从上一行开始
    while idx >= 0 and line_no - idx <= max_lines + 1:
        raw = orig_lines[idx].strip()
        if raw == "":
            idx -= 1
            continue
        if raw.startswith("//"):
            body = raw[2:].strip()
            if body and not _SEP_LINE.match(body):
                buf.append(body)
            idx -= 1
            continue
        break  # 撞到代码行就停，避免把上面别的类/函数注释带进来
    return " / ".join(reversed(buf))


_CONTENT_NOTE = re.compile(r"^[A-Za-z一-鿿]")


def _is_descriptive(text: str) -> bool:
    """过滤掉 "--------" 之类纯装饰性注释行。"""
    body = text.lstrip("/").strip()
    if not body or _SEP_LINE.match(body):
        return False
    return bool(_CONTENT_NOTE.match(body))


def _clean_doc(doc: str) -> str:
    """把 "Register: CTRL @ offset 0x00, RW" 这类注释收敛成纯描述。"""
    if not doc:
        return ""
    chunks = [c.strip() for c in doc.split(" / ") if _is_descriptive(c)]
    if not chunks:
        return ""
    doc = " / ".join(chunks)
    m = re.match(r"^(?:Register|Field|Reg|Block)\s*[::]?\s*(.*)$", doc, re.I)
    if m and m.group(1).strip():
        doc = m.group(1).strip()
    doc = re.sub(r"@\s*offset\s+[0-9A-Fa-fx']+", "", doc)
    doc = re.sub(r"\s*,\s*", ", ", doc)
    return doc.strip(" ,")


def _doc_offset_access(doc: str) -> tuple[str, str]:
    """从 "Register: CTRL @ offset 0x00, RW" 这类注释里取偏移和访问权限。"""
    offset = ""
    access = ""
    m = re.search(r"@\s*offset\s+([0-9A-Fa-fx']+)", doc or "")
    if m:
        offset = m.group(1)
    m = re.search(r"[,·]\s*([A-Za-z0-9_]+)\s*$", (doc or "").strip())
    if m and m.group(1).upper() in _KNOWN_ACCESS:
        access = m.group(1).upper()
    return offset, access


_INT_LIT = re.compile(r"^(?:(\d+)\s*)?'\s*([hHbBoOdD])\s*([0-9A-Fa-f_xXzZ]+)$")


def parse_int_literal(raw: str) -> tuple[int | None, int | None]:
    """解析 SV 整型字面量 → ``(值, 位宽)``；解析不了返回 ``(None, None)``。"""
    s = (raw or "").strip().rstrip(";").strip()
    if not s:
        return None, None
    m = _INT_LIT.match(s)
    if m:
        size = int(m.group(1)) if m.group(1) else None
        digits = m.group(3).replace("_", "")
        if re.search(r"[xXzZ]", digits):
            return None, size
        base = {"h": 16, "b": 2, "o": 8, "d": 10}[m.group(2).lower()]
        try:
            return int(digits, base), size
        except ValueError:
            return None, size
    if s.lower().startswith("0x"):
        try:
            return int(s, 16), None
        except ValueError:
            return None, None
    if re.fullmatch(r"-?\d+", s):
        return int(s), None
    return None, None


_EXPR_OP_RE = re.compile(r"\s*([+\-|])\s*")


def _balanced(text: str) -> bool:
    """圆括号是否配对（只数括号，不关心字符串里的括号 —— 偏移表达式够用了）。"""
    depth = 0
    for ch in text:
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if depth < 0:
            return False
    return depth == 0


def parse_const_expr(raw: str) -> int | None:
    """
    解析整型字面量，或**由字面量拼出来的简单常量表达式**。

    模型里的偏移经常不是单个字面量：``'h4000 + 'h10``、``(32'h4000)``、
    ``BASE | 8``。只认单个字面量的话，整块的 offset 会一起变空 ——
    而这正是"一个 offset 都解析不出来"的典型原因之一。
    """
    s = (raw or "").strip().rstrip(";").strip()
    for _ in range(4):                       # 剥掉外层多余的括号
        if s.startswith("(") and s.endswith(")") and _balanced(s[1:-1]):
            s = s[1:-1].strip()
        else:
            break
    value, _width = parse_int_literal(s)
    if value is not None:
        return value

    parts = _EXPR_OP_RE.split(s)             # ["a", "+", "b", "-", "c"]
    if len(parts) < 3 or len(parts) % 2 == 0:
        return None
    total, first = 0, True
    for idx in range(0, len(parts), 2):
        piece, _width = parse_int_literal(parts[idx])
        if piece is None:                    # 有一段不是常量（变量/未知宏）→ 放弃
            return None
        if first:
            total, first = piece, False
        elif parts[idx - 1] == "+":
            total += piece
        elif parts[idx - 1] == "-":
            total -= piece
        else:                                # "|"
            total |= piece
    return total


# =============================================================== 宏（`define）

#: UVM 自带的寄存器位宽宏（uvm_reg_defines.svh）。模型里自己 `define 了就用模型的值。
#: ``UVM_REG_ADDR_WIDTH`` 是**地址**位宽，业界模型大量写成
#: ``create_map("m", `UVM_REG_ADDR_WIDTH'h4000, 4, UVM_LITTLE_ENDIAN)`` ——
#: 不解析这个宏，map 的基地址就取不到（整行只剩 "4B UVM_LITTLE_ENDIAN"）。
BUILTIN_MACROS: dict[str, str] = {
    "UVM_REG_ADDR_WIDTH": "32",
    "UVM_REG_DATA_WIDTH": "64",
}

_DEFINE_RE = re.compile(r"^[ \t]*`define[ \t]+([A-Za-z_]\w*)([ \t]*\([^)]*\))?[ \t]*(.*)$")
_MACRO_REF_RE = re.compile(r"`([A-Za-z_]\w*)")


def collect_macros(code: str) -> dict[str, str]:
    """
    收集 `` `define NAME 值 ``（对象宏），返回 ``{名字: 值}``。

    * 带参数的宏（`` `define FOO(x) ...``）跳过 —— 它不是数值；
    * ``\\`` 续行的值拼成一行（**绝不能带换行**，否则会把行号搞乱）；
    * 空值的宏也跳过，免得把引用处替换成空串、悄悄吃掉一段代码。
    """
    out: dict[str, str] = {}
    lines = code.split("\n")
    for i, line in enumerate(lines):
        m = _DEFINE_RE.match(line)
        if not m or m.group(2):
            continue
        value = m.group(3).strip()
        while value.endswith("\\") and i + 1 < len(lines):
            i += 1
            value = value[:-1].strip() + " " + lines[i].strip()
        value = value.strip()
        if value:
            out[m.group(1)] = value
    return out


def expand_macros(code: str, macros: dict[str, str]) -> str:
    """
    把代码里的 `` `NAME `` 换成宏表中的值，**只认表里有的名字**，别的反引号原样留着
    （`` `uvm_object_utils`` 这类 UVM 宏不受影响）。

    两个刻意的约束：

    * **不动字符串内部**：`` "..." `` 里的反引号不替换；
    * **不产生换行**：替换文本都来自单行的 define，因此行号一一对应，
      后面所有基于 ``line_of()`` 的行号、源码定位都不会错位。
    """
    if not macros:
        return code
    for _ in range(8):                      # 宏套宏，最多展开 8 层
        out: list[str] = []
        i, n, quote, changed = 0, len(code), False, False
        while i < n:
            ch = code[i]
            if quote:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(code[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    quote = False
                i += 1
                continue
            if ch == '"':
                quote = True
            elif ch == "`":
                m = _MACRO_REF_RE.match(code, i)
                if m and m.group(1) in macros:
                    out.append(macros[m.group(1)])
                    i = m.end()
                    changed = True
                    continue
            out.append(ch)
            i += 1
        code = "".join(out)
        if not changed:
            break
    return code


def split_top_args(text: str) -> list[str]:
    """按顶层逗号切分实参列表（忽略括号/方括号/字符串里的逗号）。"""
    args: list[str] = []
    cur: list[str] = []
    depth = 0
    quote = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < n:
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = False
            i += 1
            continue
        if ch == '"':
            quote = True
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        args.append("".join(cur).strip())
    return args  # 保留中间的空白实参，避免位置错位


def find_matching(text: str, open_idx: int) -> int:
    """
    给定 ``(`` 的下标，返回配对 ``)`` 的下标；找不到返回 -1。

    注意：SV 里的单引号是进制前缀（``1'b0``/``8'h00``），**不是**字符串定界符，
    所以这里只把 ``"`` 当引号，否则 ``1'b0, 1, 0, 0);`` 会被吞掉一大段。
    """
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    quote = False
    i, n = open_idx, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i
        i += 1
    return -1


def call_args(text: str, paren_idx: int) -> str:
    """取出一对括号内的文本（含嵌套）。"""
    end = find_matching(text, paren_idx)
    return text[paren_idx + 1:end] if end > 0 else ""


def line_of(text: str, pos: int, first_line: int = 1) -> int:
    return first_line + text.count("\n", 0, pos)


# =============================================================== 类体解析


_CREATE_RE = re.compile(r"(?:(?P<lhs>\w+)\s*=\s*)?(?P<type>\w+(?:::\w+)*)::type_id::create\s*\(")
_NEW_CALL_RE = re.compile(r"(?P<lhs>\w+)\s*=\s*new\s*\(")
_SUPER_NEW_RE = re.compile(r"\bsuper\s*\.\s*new\s*\(")
_METHOD_RE = re.compile(r"(?P<obj>\w+)\s*\.\s*(?P<method>\w+)\s*\(")
#: 不带对象前缀的调用，如 ``create_map(...)`` / ``lock_model()``
_BARE_CALL_RE = re.compile(r"(?<![\w.`$:])(?P<method>[a-z_]\w*)\s*\(")
_BARE_SKIP = {
    "if", "for", "while", "do", "repeat", "forever", "foreach", "case", "casex", "casez",
    "else", "begin", "end", "initial", "always", "assign", "typedef", "struct", "union",
    "new", "string", "int", "bit", "logic", "void", "byte", "return", "function", "task",
    "import", "export", "assert", "assume", "cover", "wait", "with", "unique", "priority",
    "posedge", "negedge", "this", "super", "and", "or", "not", "sens", "solve", "before",
}
_MEMBER_RE = re.compile(
    r"^\s*(?:(?:rand|randc)\s+)?(?:(?:local|protected|public|static|virtual|automatic)\s+)*"
    r"(?P<type>[A-Za-z_]\w*(?:::\w+)*)\s+(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;\s*$"
)
_MEMBER_SKIP = {"return", "import", "export", "typedef"}


@dataclass
class ClassInfo:
    """源码里一个 class 的原始提取结果。"""

    name: str
    base: str
    file: str
    line: int
    body: str
    body_line: int
    kind: str = KIND_UNKNOWN
    doc: str = ""
    members: list[tuple[str, str, int]] = _field(default_factory=list)  # (类型, 名字, 行号)
    creates: list[tuple[str, str, str, int]] = _field(default_factory=list)  # (变量, 类型, 名字, 行号)
    news: list[tuple[str, str, int]] = _field(default_factory=list)  # X = new(...) -> (变量, 实参, 行号)
    calls: list[tuple[str, str, str, int]] = _field(default_factory=list)  # (对象, 方法, 实参, 行号)
    width: int | None = None
    orig_lines: Sequence[str] = ()
    comments: dict[int, list[tuple[bool, str]]] = _field(default_factory=dict)

    def calls_of(self, method: str) -> list[tuple[str, str, str, int]]:
        return [c for c in self.calls if c[1] == method]

    def lead_doc(self, line_no: int, max_lines: int = 4) -> str:
        return doc_above(self.orig_lines, line_no, max_lines)

    def trail_doc(self, line_no: int) -> str:
        return " / ".join(t for pure, t in self.comments.get(line_no, []) if not pure)


def _name_kind(name: str) -> str:
    for pattern, kind in _NAME_RULES:
        if pattern.search(name):
            return kind
    return KIND_UNKNOWN


def _strip_params(base: str) -> str:
    return re.sub(r"#\s*\(.*\)\s*$", "", base or "").strip()


def _body_kind(info: "ClassInfo") -> str:
    """类体特征兜底判定（基类不认识、名字也不合规则时用）。"""
    body = info.body
    if re.search(r"\bcreate_map\s*\(", body) or any(
        c[1] in {"add_reg", "add_submap", "add_mem"} for c in info.calls
    ):
        return KIND_BLOCK
    if re.search(r"\bset_hdl_path_root\s*\(|\block_model\s*\(", body):
        return KIND_BLOCK
    if re.search(r"\bconfigure\s*\(\s*this\b", body):
        return KIND_REG
    if re.search(r"::type_id::create", body) and re.search(
        r"\bsuper\s*\.\s*new\s*\([^)]*,\s*\d+\s*,", body
    ):
        return KIND_REG
    return KIND_UNKNOWN


def resolve_kind(name: str, infos: dict[str, "ClassInfo"], seen: set[str] | None = None) -> str:
    """递归判定类类别：继承链 → 命名规则 → 类体特征。"""
    name = _strip_params(name)
    if not name:
        return KIND_UNKNOWN
    if name in _BUILTIN_KIND:
        return _BUILTIN_KIND[name]
    info = infos.get(name)
    if info is None:
        return _name_kind(name)  # 定义在别处（未解析）的外部类，只按名字猜
    if info.kind != KIND_UNKNOWN:
        return info.kind
    seen = seen if seen is not None else set()
    if name in seen:
        return KIND_UNKNOWN
    seen.add(name)
    if info.base:
        kind = resolve_kind(info.base, infos, seen)
        if kind != KIND_UNKNOWN:
            info.kind = kind
            return kind
    kind = _name_kind(name)
    if kind == KIND_UNKNOWN:
        kind = _body_kind(info)
    info.kind = kind
    return kind


def _read_class_header(code: str, pos: int) -> tuple[str, str, int]:
    """
    从 ``class`` 关键字位置解析出 ``(类名, 基类, 类体起始偏移)``。

    类体起始 = 声明语句（含 ``#(参数)`` 列表）末尾分号之后。
    """
    m = re.match(r"\s*class\s+(?P<name>\w+)", code[pos:])
    if not m:
        return "", "", pos
    name = m.group("name")
    i, n, depth, end = pos + m.end(), len(code), 0, len(code)
    while i < n:
        ch = code[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == '"':
            i += 1
            while i < n and code[i] != '"':
                i += 2 if code[i] == "\\" else 1
        elif ch == ";" and depth <= 0:
            end = i + 1
            break
        i += 1
    header = code[pos:end]
    bm = re.search(r"\bextends\s+([\w:]+)\s*(?:#\s*\(.*?\))?", header, re.S)
    return name, (bm.group(1) if bm else ""), end


def _extract_body(info: ClassInfo) -> None:
    """提取类体里的成员声明与 create / configure / add_reg 等调用。"""
    body, first = info.body, info.body_line

    # --- 顶层成员声明（跳过 function/task 内部；注意 \bfunction\b 不会匹配 endfunction）
    fn_depth = 0
    for offset, raw in enumerate(body.split("\n")):
        stripped = raw.strip()
        if not stripped:
            continue
        head = stripped.split("(", 1)[0].split(";")[0].strip()
        if re.search(r"\b(function|task)\b", head):
            fn_depth += 1
            continue
        if re.search(r"\bendfunction\b|\bendtask\b", stripped):
            fn_depth = max(0, fn_depth - 1)
            continue
        if fn_depth:
            continue
        m = _MEMBER_RE.match(raw)
        if not m:
            continue
        mtype, mname = m.group("type"), m.group("name")
        if mtype in _MEMBER_SKIP or mname in {"begin", "end"}:
            continue
        info.members.append((mtype, mname, first + offset))

    # --- 调用
    for m in _METHOD_RE.finditer(body):
        info.calls.append(
            (
                m.group("obj"),
                m.group("method"),
                call_args(body, m.end() - 1),
                line_of(body, m.start(), first),
            )
        )
    for m in _BARE_CALL_RE.finditer(body):  # create_map(...) / lock_model() 这类无前缀调用
        method = m.group("method")
        if method in _BARE_SKIP:
            continue
        lhs = re.search(r"(\w+)\s*=\s*$", body[max(0, m.start() - 64):m.start()].rstrip())
        info.calls.append(
            (
                lhs.group(1) if lhs else "",
                method,
                call_args(body, m.end() - 1),
                line_of(body, m.start(), first),
            )
        )

    for m in _SUPER_NEW_RE.finditer(body):
        args = call_args(body, m.end() - 1)
        info.calls.append(("super", "new", args, line_of(body, m.start(), first)))
        for arg in split_top_args(args)[1:]:
            value, _w = parse_int_literal(arg)
            if value is not None:
                info.width = value  # super.new(name, 32, UVM_NO_COVERAGE) 里的 32
                break

    # --- create / 显式 new
    for m in _CREATE_RE.finditer(body):
        info.creates.append(
            (
                m.group("lhs") or "",
                m.group("type"),
                _first_string(body, m.end()),
                line_of(body, m.start(), first),
            )
        )
    for m in _NEW_CALL_RE.finditer(body):
        info.news.append(
            (m.group("lhs"), call_args(body, m.end() - 1), line_of(body, m.start(), first))
        )


def _first_string(text: str, pos: int) -> str:
    m = re.search(r'"([^"]*)"', text[pos:pos + 240])
    return m.group(1) if m else ""


def parse_source(path: Path, text: str, result: ParseResult,
                 macros: dict[str, str] | None = None) -> list[ClassInfo]:
    """解析单个文件，返回其中所有 class 的信息。``macros`` 用来展开 `` `NAME ``。"""
    code, comments = mask_comments(text)
    if macros:
        code = expand_macros(code, macros)
    orig_lines = text.split("\n")     # 注释原文取自**未展开**的文本，文档提取照旧
    infos: list[ClassInfo] = []

    for pm in re.finditer(r"\bpackage\s+(\w+)\s*;", code):
        if pm.group(1) not in result.packages:
            result.packages.append(pm.group(1))

    for cm in re.finditer(r"\bclass\s+\w+", code):
        name, base, body_start = _read_class_header(code, cm.start())
        if not name:
            continue

        # 用 endclass 配对出类体范围（支持嵌套类）
        depth, pos, body_end = 1, body_start, len(code)
        while depth > 0:
            nxt_cls = re.search(r"\bclass\s+\w+", code[pos:])
            nxt_end = re.search(r"\bendclass\b", code[pos:])
            if nxt_end is None:
                break
            if nxt_cls is not None and nxt_cls.start() < nxt_end.start():
                depth += 1
                pos += nxt_cls.start() + len(nxt_cls.group(0))
                continue
            depth -= 1
            body_end = pos + nxt_end.start()
            pos = body_end + len("endclass")
        if depth != 0:
            result.warnings.append(
                f"{path.name}:{line_of(code, cm.start())}: class {name} has no matching endclass"
            )

        info = ClassInfo(
            name=name,
            base=base,
            file=path.name,
            line=line_of(code, cm.start()),
            body=code[body_start:body_end],
            body_line=line_of(code, body_start),
            orig_lines=orig_lines,
            comments=comments,
        )
        info.doc = doc_above(orig_lines, info.line)
        _extract_body(info)
        infos.append(info)

    return infos


# =============================================================== 组装模型


def _field_from_configure(args: list[str], line: int) -> FieldDef | None:
    """configure(parent, size, lsb, access, volatile, reset, has_reset, is_rand, indiv)。"""
    if len(args) < 4:
        return None
    width, _ = parse_int_literal(args[1])
    lsb, _ = parse_int_literal(args[2])
    fld = FieldDef(
        width=width if width else 1,
        lsb=lsb or 0,
        access=args[3].strip().strip('"').upper() or "RW",
        line=line,
    )
    if len(args) > 4:
        fld.volatile = parse_int_literal(args[4])[0] or 0
    if len(args) > 5:
        fld.reset_raw = args[5].strip()
        fld.reset_value = parse_int_literal(args[5])[0]
    if len(args) > 6:
        fld.has_reset = 0 if parse_int_literal(args[6])[0] == 0 else 1
    if len(args) > 7:
        fld.is_rand = parse_int_literal(args[7])[0] or 0
    if len(args) > 8:
        fld.individually_accessible = parse_int_literal(args[8])[0] or 0
    return fld


def _looks_like_field_var(info: ClassInfo, var: str, infos: dict[str, ClassInfo]) -> bool:
    if var in {"this", "super"}:
        return False
    for mtype, mname, _line in info.members:
        if mname == var:
            return resolve_kind(mtype, infos) == KIND_FIELD
    return any(lhs == var or lit == var for lhs, _t, lit, _l in info.creates)


def build_fields(info: ClassInfo, infos: dict[str, ClassInfo]) -> list[FieldDef]:
    """
    从一个寄存器类里抽出所有字段。

    成员声明 / ``type_id::create`` / ``configure`` 三处互补：
    前两者给名字、类型、行号，configure 给位宽、位置、权限、复位值。
    """
    order: list[str] = []
    meta: dict[str, dict[str, str | int]] = {}

    def touch(var: str, cls: str, line: int) -> None:
        if var not in meta:
            order.append(var)
            meta[var] = {"cls": cls, "line": line}
        elif cls and not meta[var]["cls"]:
            meta[var]["cls"] = cls

    for mtype, mname, mline in info.members:
        if resolve_kind(mtype, infos) == KIND_FIELD:
            touch(mname, mtype, mline)
    for lhs, ctype, lit, cline in info.creates:
        if resolve_kind(ctype, infos) == KIND_FIELD:
            touch(lhs or lit, ctype, cline)

    cfg_line: dict[str, int] = {}
    for obj, method, args, line in info.calls:
        if method != "configure":
            continue
        argl = split_top_args(args)
        if len(argl) < 4 or not _looks_like_field_var(info, obj, infos):
            continue
        touch(obj, "", line)
        cfg_line[obj] = line

    fields: list[FieldDef] = []
    for var in order:
        decl_line = int(meta[var]["line"])
        fld = FieldDef(
            name=var,
            cls=str(meta[var]["cls"]) or "uvm_reg_field",
            line=decl_line,
            file=info.file,
        )
        for obj, method, args, cline in info.calls:
            if method == "configure" and obj == var:
                parsed = _field_from_configure(split_top_args(args), cline)
                if parsed:
                    parsed.name, parsed.cls, parsed.file = var, fld.cls, info.file
                    fld = parsed
                break
        # 说明：configure 上方注释 → create 上方注释 → 行尾注释
        fld.doc = (
            _clean_doc(info.lead_doc(fld.line, 3))
            or _clean_doc(info.lead_doc(decl_line, 3))
            or info.trail_doc(fld.line)
            or info.trail_doc(decl_line)
        )
        fields.append(fld)

    fields.sort(key=lambda f: (f.lsb, f.name))
    return fields


def make_reg(info: ClassInfo, inst_name: str, infos: dict[str, ClassInfo]) -> RegDef:
    """由寄存器类信息生成一个 RegDef（每个 block 内一份实例）。"""
    reg = RegDef(
        cls=info.name,
        inst_name=inst_name,
        base=info.base,
        width=info.width or 32,
        doc=_clean_doc(info.doc),
        line=info.line,
        file=info.file,
    )
    reg.fields = build_fields(info, infos)
    for fld in reg.fields:
        fld.reg = reg
    doc_off, doc_acc = _doc_offset_access(info.doc)
    if doc_off:
        reg.offset_raw = doc_off
        reg.offset = parse_const_expr(doc_off)
    if doc_acc:
        reg.access = doc_acc
    if doc_acc and reg.doc:  # 注释里的 "CTRL, RW" 去掉权限，权限单独成列
        stripped = re.sub(r"[,，]\s*" + doc_acc + r"\s*$", "", reg.doc, flags=re.I).strip()
        reg.doc = stripped or reg.doc
    if not reg.fields:
        reg.notes.append("no fields parsed")
    return reg


#: ``BlockDef.notes`` 里带上这个前缀的条目会被升级成 ``ParseResult.warnings``。
#: 分水岭在于：GUI 的 Debug 面板只显示 warnings / errors —— 不带前缀的 note
#: （比如"这是个抽象基类"）是正常现象，不该刷屏；而"offset 一个都没解析出来"
#: 必须让用户看见。
NOTE_WARN_PREFIX = "⚠ "


def resolve_var_ref(ref: str, names: Iterable[str],
                    aliases: dict[str, str] | None = None) -> str | None:
    """
    把 ``add_reg()`` / ``add_mem()`` 的第一个实参归一到真正的实例名。

    真实模型里这个位置出现过各种写法：``TOP_CRG``（成员名）、``this.TOP_CRG``、
    ``m_TOP_CRG``、``blk.TOP_CRG``、``TOP_CRG.get_name()``，甚至直接写类名
    ``ral_reg_rubx_TOP_CRG`` 或 ``create()`` 里的字符串字面量。只按成员名精确
    匹配的话，一旦对不上，**整个 block 的 offset 会一起变空**（界面上就是所有
    寄存器都显示 "——"），所以这里逐个策略往下试。
    """
    text = (ref or "").strip().strip('"').strip()
    if not text:
        return None
    text = re.sub(r"\(\s*\)\s*$", "", text)      # TOP_CRG.get_name() → TOP_CRG.get_name
    pool = set(names)
    for cand in reversed(text.split(".")):       # this.TOP_CRG → 先试最后一段
        cand = cand.strip()
        if cand in pool:
            return cand
        if aliases and cand in aliases:
            return aliases[cand]
    tail = text.split(".")[-1].strip()           # ral_reg_rubx_TOP_CRG ↔ TOP_CRG
    hits = [n for n in pool if n.endswith("_" + tail) or tail.endswith("_" + n)]
    return hits[0] if len(hits) == 1 else None   # 只认唯一解，模棱两可就别猜


def build_block(info: ClassInfo, infos: dict[str, ClassInfo]) -> BlockDef:
    """把一个 block 类组装成 BlockDef，含寄存器实例与地址映射。"""
    blk = BlockDef(
        cls=info.name,
        base=info.base,
        doc=_clean_doc(info.doc),
        line=info.line,
        file=info.file,
    )

    for obj, method, args, _line in info.calls:
        if method == "create_map":
            argl = split_top_args(args)
            blk.map_name = obj
            if len(argl) > 1:
                blk.map_base = parse_int_literal(argl[1])[0]
            if len(argl) > 2:
                blk.map_addr_bytes = parse_int_literal(argl[2])[0]
            if len(argl) > 3:
                blk.map_endian = argl[3].strip()
            break
    for _obj, method, args, _line in info.calls:
        if method == "set_hdl_path_root":
            argl = split_top_args(args)
            if argl:
                blk.hdl_path_root = argl[0].strip().strip('"')
        elif method == "lock_model":
            blk.locked = True
        elif method == "add_submap":
            argl = split_top_args(args)
            off = parse_int_literal(argl[1])[0] if len(argl) > 1 else None
            text = argl[0].strip() if argl else args.strip()
            blk.submaps.append(text + (f" @ 0x{off:X}" if off is not None else ""))

    # --- 寄存器 / 内存 / 子 block 实例：成员声明 + create 调用 + 显式 new
    reg_vars: list[str] = []
    reg_types: dict[str, str] = {}
    mem_vars: list[str] = []
    mem_types: dict[str, str] = {}
    sub_vars: dict[str, str] = {}
    for mtype, mname, _line in info.members:
        kind = resolve_kind(mtype, infos)
        if kind == KIND_REG:
            if mname not in reg_types:
                reg_vars.append(mname)
            reg_types[mname] = mtype
        elif kind == KIND_MEM:
            if mname not in mem_types:
                mem_vars.append(mname)
            mem_types[mname] = mtype
        elif kind == KIND_BLOCK and mtype != info.name:
            sub_vars.setdefault(mname, mtype)
    handle_aliases: dict[str, str] = {}
    for lhs, ctype, lit, _line in info.creates:
        var = lhs or lit
        kind = resolve_kind(ctype, infos)
        # 有些生成器在 build() 里用的句柄名和成员名**不是同一个**：
        #     rand ral_reg_TOP TOP;              // 成员叫 TOP
        #     RAL_HANDLE = ral_reg_TOP::create("TOP");   // 句柄却叫 RAL_HANDLE
        # 若照字面当成两个实例，界面上会出现两条一模一样的寄存器，而真正带
        # offset 的那条（按句柄名匹配 add_reg）和"没 offset"的那条并存。
        # 这里认出来：create 的实例名才是成员，句柄名退化成 add_reg 的别名。
        if (lhs and lit and kind == KIND_REG and lhs not in reg_types
                and reg_types.get(lit) == ctype):
            handle_aliases.setdefault(lhs, lit)
            continue
        if kind == KIND_REG and var not in reg_types:
            reg_vars.append(var)
            reg_types[var] = ctype
        elif kind == KIND_MEM and var not in mem_types:
            mem_vars.append(var)
            mem_types[var] = ctype

    regs: dict[str, RegDef] = {}
    for var in reg_vars:
        rinfo = infos.get(reg_types[var])
        if rinfo is None:
            blk.notes.append(
                f"register class {reg_types[var]} is not defined in this batch "
                f"(instance {var})"
            )
            continue
        regs[var] = make_reg(rinfo, var, infos)

    # --- 内存模型（uvm_mem / *_mem）
    mems: dict[str, MemDef] = {}
    for var in mem_vars:
        mems[var] = MemDef(name=var, cls=mem_types[var], line=info.line, file=info.file)
    for lhs, args, _line in info.news:  # new("fifo", 256, 8, "RW", UVM_NO_COVERAGE)
        mem = mems.get(lhs)
        if mem is None:
            continue
        argl = split_top_args(args)
        if len(argl) > 1:
            mem.depth = parse_int_literal(argl[1])[0]
        if len(argl) > 2:
            mem.width = parse_int_literal(argl[2])[0]
        if len(argl) > 3:
            mem.access = argl[3].strip().strip('"').upper()
    for obj, method, args, _line in info.calls:
        if method != "add_mem":
            continue
        argl = split_top_args(args)
        mem_name = resolve_var_ref(argl[0], mems) if argl else None
        mem = mems.get(mem_name) if mem_name else None
        if mem is None:
            continue
        if len(argl) > 1:
            mem.offset = parse_const_expr(argl[1])
        if len(argl) > 2 and not mem.access:
            mem.access = argl[2].strip().strip('"').upper()
    blk.mems = list(mems.values())

    # --- add_reg 补 offset / 权限；set_hdl_path 补 HDL 路径
    # 别名表：类名 / create() 的字面量 → 实例名（add_reg 里这几种写法都见过）。
    # **只有唯一解才收**：同一个类被实例化多次时按类名反查是有歧义的，
    # 宁可报 "对不上" 也不能把 offset 安到错的寄存器上。
    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()

    def _alias(key: str, var: str) -> None:
        key = (key or "").strip().strip('"')
        if not key or key in ambiguous:
            return
        other = aliases.get(key)
        if other is None:
            aliases[key] = var
        elif other != var:
            aliases.pop(key, None)
            ambiguous.add(key)

    for var, reg in regs.items():
        _alias(reg.cls, var)
    for lhs, ctype, lit, _line in info.creates:
        var = lhs or lit
        if var in regs and lit:
            _alias(lit, var)
    for handle, var in handle_aliases.items():     # 句柄名 → 成员名
        if var in regs:
            _alias(handle, var)

    mapped: set[str] = set()
    unmatched: list[str] = []
    unparsed: list[str] = []
    for obj, method, args, line in info.calls:
        if method == "add_reg":
            argl = split_top_args(args)
            if not argl:
                continue
            raw_ref = argl[0].strip()
            var = resolve_var_ref(raw_ref, regs, aliases)
            if var is None:
                unmatched.append(f"add_reg({raw_ref}, …) on line {line} "
                                 f"does not match any register instance")
                continue
            reg = regs[var]
            if len(argl) > 1:
                reg.offset_raw = argl[1].strip()
                reg.offset = parse_const_expr(argl[1])
                if reg.offset is None:      # 字面量/宏没解析出来 —— 以前是**静默**的
                    unparsed.append(f"{reg.inst_name} ← {reg.offset_raw!r} (line {line})")
            if len(argl) > 2:
                reg.access = argl[2].strip().strip('"').upper()
            mapped.add(var)
        elif method == "set_hdl_path":
            argl = split_top_args(args)
            var = resolve_var_ref(obj, regs, aliases)
            reg = regs.get(var) if var else None
            if reg is not None and argl:
                reg.hdl_path = argl[0].strip().strip('"')

    # --- offset 解析失败的诊断。这一类问题以前**完全静默**：界面只是把 offset
    # 显示成 "——"，用户无从知道是"没写 add_reg"还是"写了但对不上/解析不了"。
    # 带 NOTE_WARN_PREFIX 的条目会被 parse_files 升级成 warning，直接进 Debug 面板。
    for text in unmatched[:6]:
        blk.notes.append(NOTE_WARN_PREFIX + text)
    for text in unparsed[:6]:
        blk.notes.append(NOTE_WARN_PREFIX + f"offset not resolved: {text}")
    if (unmatched or unparsed) and regs:
        blk.notes.append(
            NOTE_WARN_PREFIX
            + f"only {len(mapped) - len(unparsed)}/{len(regs)} register offset(s) resolved "
              f"in block {info.name} — unmatched add_reg() argument or unresolvable "
              f"offset literal/macro"
        )
    elif regs and blk.map_name and not any(r.offset is not None for r in regs.values()):
        # 有地址映射（create_map）却一个 offset 都没落上：说明这个 block 的寄存器
        # 不是靠 add_reg 放进 map 的（或者 add_reg 写在别处）。不加提示的话，
        # 界面上就是一片 "——"，用户完全不知道从哪查起。
        blk.notes.append(
            NOTE_WARN_PREFIX
            + f"block {info.name} has an address map ({blk.map_name}) but no register "
              f"offset was resolved from add_reg() — are the registers added to the map "
              f"in another file/function?"
        )

    # --- 排序：有偏移按地址，其余保持源码顺序
    have_off = sorted((r for r in regs.values() if r.offset is not None), key=lambda r: r.offset)
    no_off = sorted(
        (r for r in regs.values() if r.offset is None),
        key=lambda r: reg_vars.index(r.inst_name) if r.inst_name in reg_vars else 0,
    )
    blk.regs = have_off + no_off
    for reg in blk.regs:
        reg.block = blk
        if reg.inst_name not in mapped:
            reg.notes.append("not added to the address map")
    blk.subblocks = [f"{cls} (instance {var})" for var, cls in sub_vars.items()]
    if blk.is_abstract:
        blk.notes.append("no register / memory instantiated "
                         "(probably an abstract base class)")
    return blk


#: 读文件时清掉的控制字符：``\t`` / ``\n`` 保留（词法分析要用），其余 C0 控制符和
#: DEL 全去掉，``\r`` 顺带一起（CRLF 变成 LF，行号不受影响）。
#: 为什么要在这里清：``errors="replace"`` 只把**非法字节**换成 U+FFFD，**NUL 是原样保留**
#: 的；而 ``\x00`` 一旦漏进解析结果（offset 原文、字段名、注释……）就会顺着 GUI 流到
#: Tk 的 Treeview / Text / Canvas 上，在 X11 上是段错误的经典诱因，界面上还什么都看不到。
_FILE_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def parse_files(paths: Sequence[str | Path],
                macros: dict[str, str] | None = None) -> ParseResult:
    """
    解析一批 SV 文件，返回统一的寄存器模型视图（跨文件解析继承与实例化）。

    宏走两遍：先把所有文件的 `` `define `` 收齐（跨文件共享 —— 模型常把
    ``*_defines.svh`` 里的位宽定义单独放一个文件），再带宏表解析。
    内置默认值见 :data:`BUILTIN_MACROS`（``UVM_REG_ADDR_WIDTH`` 默认 32bit），
    文件里自己定义的同名宏优先。``macros`` 参数可以再从外部覆盖一层。
    """
    result = ParseResult()
    infos: dict[str, ClassInfo] = {}
    order: list[str] = []

    merged: dict[str, str] = dict(BUILTIN_MACROS)
    texts: list[tuple[Path, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        try:
            text = _FILE_CTRL_RE.sub("", path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            result.errors.append(f"{path}: cannot read file ({exc.__class__.__name__}: {exc})")
            continue
        texts.append((path, text))
        merged.update(collect_macros(mask_comments(text)[0]))
    if macros:
        merged.update(macros)
    result.macros = merged

    for path, text in texts:
        if str(path) not in result.files:
            result.files.append(str(path))
        for info in parse_source(path, text, result, merged):
            if info.name in infos:
                result.warnings.append(
                    f"{path.name}:{info.line}: class {info.name} is defined more than once; "
                    f"using the later definition"
                )
            else:
                order.append(info.name)
            infos[info.name] = info

    for name in order:
        infos[name].kind = resolve_kind(name, infos)
        result.kinds[name] = infos[name].kind

    used_reg_classes: set[str] = set()
    for name in order:
        if infos[name].kind != KIND_BLOCK:
            continue
        blk = build_block(infos[name], infos)
        used_reg_classes.update(r.cls for r in blk.regs)
        result.blocks.append(blk)
        for note in blk.notes:          # 标记过的 note 升级成 warning（GUI 才看得见）
            if note.startswith(NOTE_WARN_PREFIX):
                result.warnings.append(f"{blk.cls}: {note[len(NOTE_WARN_PREFIX):]}")

    for name in order:  # 没被任何 block 引用的寄存器类单独列出，避免丢数据
        if infos[name].kind != KIND_REG or name in used_reg_classes:
            continue
        reg = make_reg(infos[name], name, infos)
        reg.notes.append("not instantiated by any block")
        result.loose_regs.append(reg)

    if not result.blocks and not result.loose_regs:
        result.warnings.append("no register model found (is this really a RAL model file?)")
    return result


# =============================================================== 文本输出


def dump(result: ParseResult) -> None:
    """命令行文本视图，便于不开 GUI 时核对解析结果。"""
    print(f"== {result.summary()}")
    if result.packages:
        print(f"== package: {', '.join(result.packages)}")
    if result.macros:
        print(f"== macros: {', '.join(f'{k}={v}' for k, v in sorted(result.macros.items()))}")
    for path in result.files:
        print(f"== file: {path}")

    for blk in result.blocks:
        print()
        print(f"[BLOCK] {blk.cls} extends {blk.base or '-'}   ({blk.file}:{blk.line})")
        if blk.doc:
            print(f"        doc       : {blk.doc}")
        if blk.map_text:
            print(f"        addr map  : {blk.map_text}")
        if blk.hdl_path_root:
            print(f"        HDL root  : {blk.hdl_path_root}")
        if blk.subblocks:
            print(f"        sub-blocks: {', '.join(blk.subblocks)}")
        if blk.submaps:
            print(f"        sub-maps  : {', '.join(blk.submaps)}")
        print(f"        lock_model: {'yes' if blk.locked else 'no'}")
        for mem in blk.mems:
            print(
                f"  {mem.offset_text or '  --':>6}  {mem.access or '-':<4} {mem.size_text:<12} "
                f"{mem.cls:<24} inst={mem.name:<12} (memory)"
            )
        for reg in blk.regs:
            off = "  --  " if reg.offset is None else f"0x{reg.offset:02X}"
            print(
                f"  {off:>6}  {reg.effective_access or '-':<4} {reg.width:>2}bit  "
                f"{reg.cls:<24} inst={reg.display_name:<12} reset={reg.reset_text or '-':<8} "
                f"fields={len(reg.fields)}  ({reg.file}:{reg.line})"
            )
            if reg.doc:
                print(f"          doc: {reg.doc}")
            for fld in reg.fields:
                extra = f"  {' '.join(filter(None, [fld.flags, fld.doc]))}"
                print(
                    f"            {fld.bit_range:>8}  {fld.name:<18} {fld.width:>2}bit "
                    f"{fld.access:<4} reset={fld.reset_raw or '-':<8}{extra}"
                )
            for note in reg.notes:
                print(f"          ! {note}")
        for note in blk.notes:
            # 前缀只用来标记"要升级成 warning"，命令行里已经有 ! 了，不必再打一遍
            print(f"        ! {note.removeprefix(NOTE_WARN_PREFIX)}")

    if result.loose_regs:
        print()
        print(f"[{LOOSE_BLOCK}]")
        for reg in result.loose_regs:
            print(f"  {reg.cls:<28} fields={len(reg.fields)}  ({reg.file}:{reg.line})")
            for fld in reg.fields:
                print(f"            {fld.bit_range:>8}  {fld.name:<18} {fld.width:>2}bit {fld.access}")

    unknown = [n for n, k in result.kinds.items() if k == KIND_UNKNOWN]
    if unknown:
        print()
        print(f"[unclassified classes] {', '.join(unknown)}")

    if result.errors:
        print()
        print("[errors]")
        for err in result.errors:
            print(f"  ! {err}")

    if result.warnings:
        print()
        print("[warnings]")
        for warn in result.warnings:
            print(f"  - {warn}")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    paths = [a for a in argv if not a.startswith("-")]
    if not paths:
        here = Path(__file__).with_name("uart_ral_pkg.sv")
        if not here.exists():
            print(__doc__)
            return 2
        paths = [str(here)]
    missing = [p for p in paths if not Path(p).exists()]
    for path in missing:
        print(f"file not found: {path}")
    result = parse_files(paths)
    dump(result)
    return 0 if result.all_regs else 1


if __name__ == "__main__":
    sys.exit(main())
