# RAL 寄存器模型浏览器

用 CustomTkinter 写的一个小工具：导入一个或多个 SystemVerilog **RAL 寄存器模型**
文件（`*_ral_pkg.sv`），解析出 **block / reg / reg_field** 三级结构，以表格和位图
两种形式呈现每个层级的特征与名字。

> **界面文案一律英文**（按钮、表头、状态栏、位图标注、Debug 面板全都如此），
> 代码里的注释和文档字符串保持中文。解析器抛出的告警/错误信息同样是英文，
> 因为它们会直接显示在界面上。

本目录下的其它内容（`customtkinter_demo.py` / `pyside6_demo.py`）是框架对照示例，
与本工具无关。

| 文件 | 作用 |
|---|---|
| `ral_parser.py` | 纯解析器，不依赖 GUI，可单独命令行运行 |
| `ral_viewer.py` | CustomTkinter 界面 |
| `uart_ral_pkg.sv` | 随附示例模型（7 个寄存器 / 32 个字段） |
| `samples/` | 通用性测试样本（故意写成不规范风格） |

## 运行

```powershell
python ral_viewer.py                         # 直接打开（自动载入随附的 uart_ral_pkg.sv）
python ral_viewer.py a_ral_pkg.sv b_ral_pkg.sv
python ral_viewer.py .\rtl\*.sv              # 多文件一起解析，跨文件继承也能认
python ral_viewer.py --light                 # 浅色启动
python ral_viewer.py --selftest              # 自测：渲染 → 断言 → 截图 → 退出
```

只想看解析结果、不要界面：

```powershell
python ral_parser.py uart_ral_pkg.sv --dump
```

## 界面

| 区域 | 说明 |
|---|---|
| 顶部 | Open files / Add files / Load sample / Clear / Export CSV / 深浅色切换 |
| 搜索栏 | 多关键词（空格分隔）AND 过滤，作用于当前视图所有列；`Hide tree` 可收起左侧树把宽度让给表格 |
| 左侧树 | 文件 → block → 寄存器 / 内存；点任意节点即把右侧表格限定到该范围 |
| 右侧视图 | **Registers** / **Fields** / **Blocks** / **Bitmap** / **All Bitmaps** / **Source**，表格视图点击表头可排序 |
| 底部 Debug 面板 | 解析全过程 + 告警 + 错误 + 失败堆栈；出错自动展开 |

### 各视图的列

* **Registers**：Block、Instance、Register Class、Offset、Access、Width、Reset、
  Fields、Reserved Bits（保留位）、Description、Base Class、HDL Path、File:Line
* **Fields**：Block、Register、Field、Bit Range `[8:7]`、Width、LSB、Access、Reset、
  Attributes（rand / volatile / no_reset / indiv）、Description、File:Line
* **Blocks**：Block、Base Class、Registers、Memories、Fields、
  Address Map（`reg_map base=0x0 4B UVM_LITTLE_ENDIAN`）、HDL Root、
  Locked（是否 `lock_model()`）、Description、Sub-blocks / Sub-maps、File:Line
* **Bitmap**：按位画出寄存器，字段按访问权限着色，下方是字段图例，
  鼠标悬停显示字段详情。**位带和图例都是从高位到低位**（左端标 `MSB bit31`、
  右端标 `LSB bit0`），两个方向一致；窄字段在格子里画不下名字，由图例补全
* **All Bitmaps**：所有寄存器一次画完、纵向排列在同一个可滚动画布上。
  左侧一栏是「偏移 + 实例名 + 访问/位宽/字段数」，右侧是所有寄存器**共用同一个格宽**
  的位带，因此上下几行的同一个 bit 号是竖直对齐的，方便一眼比对谁占了哪些位。
  每行都带自己的高低位标注；字段名塞不进格子时，该行下方补一行字段图例
  （同样是高位在前）。
  悬停任意位格显示字段详情，点击某行即聚焦该寄存器（状态栏确认，
  切回 Bitmap 就是它的放大图）。搜索框和左侧模型树的作用域照样生效，
  比如搜 `intr` 就只剩三个中断寄存器。

### 位图横向拉长（字段名看不全时）

窄字段（1bit 的 `en`、`tx`，2bit 的 `rxblvl`）在自适应格宽下只有二三十像素，
名字**画不进格子**，被降级到下方图例里 —— 想直接读格子里的名字就得拉长。
Bitmap / All Bitmaps 两个视图各有一条缩放条：

```
-   ×1 · 26px/bit (fit)   +   Fit
```

* `+` / `-` 在档位表 `×1 → 1.25 → 1.5 → 2 → 2.5 → 3 → 4 → 6 → 8` 上前后挪一格，
  实际格宽 = 自适应值 × 档位（夹在 6 ~ 400 px/bit）；
* `Fit` 回到 1×（自适应铺满视口宽度），这也是打开时的默认状态；
* 标签实时显示当前档位和真实的 px/bit，1× 时补一个 `(fit)`；
* 内容比视口宽之后底部出现**横向滚动条**，另外 **Shift + 滚轮**可以直接左右滚
  （没得滚的时候忽略，免得误触发）；
* 档位是两个视图**共享**的：在 Bitmap 里拉到 4×，切到 All Bitmaps 也是 4×，
  两边的「一个 bit 多宽」始终一致；
* All Bitmaps 里所有寄存器共用同一个格宽，拉长后整列一起变宽，
  上下对齐的关系不变。

「Fit」是**真的 fit**：自适应格宽向下取整，保证 `图宽 + 边距 ≤ 视口宽`，
所以 1× 下不会冒出一条几乎没用、却和标签上 `(fit)` 打架的横向滚动条。
挤不下的时候（比如 240px 宽的画布要画 64bit 寄存器）格宽退回 8px/bit 的下限、
允许横向溢出 —— 总比把格子压成 1~2px、只剩几条看不清的竖线强。

### 渲染性能

位图这两块最早是按「每个 bit 一个矩形 + 一个位号」画的，几十个寄存器就要
三千多个画布图元，重绘上百毫秒、滚轮一拖就顿。现在：**每段字段只画一个矩形**、
文字宽度**带缓存**、裁剪用**二分**、`<Configure>` 重绘**防抖**、
排版尺寸**全部由字体的真实行高推导**（高 DPI 下字形是磅不是像素，
写死像素会把两行字叠在一起，看起来像字体发虚，详见踩坑 12~16）。

54 个寄存器 / 216 个字段的合成模型实测（Python 3.12.10，200% 缩放）：

| 项目 | 优化前 | 优化后 |
|---|---|---|
| 「全部位图」画布图元 | 3888 | 702 |
| 整幅重绘（首次 / 之后） | ~110 ms | 54 ms / **22 ms** |
| 滚轮每格 | 9.0 ms | **2.9 ms** |
| 单寄存器位图 | 5.0 ms | 4.3 ms |

首次那次多出来的 ~30ms 是在给每个名字量宽度（之后的渲染全部命中缓存）。
* **Source**：显示选中 block / 寄存器 / 字段对应的原始 SV 代码行（`>` 标出范围；
  原生 `Text` 控件，行号标记/分隔线一律 ASCII，原因见踩坑 22）

导出 CSV 带 UTF-8 BOM，Excel 直接双击不乱码。

### 跨平台（Windows / Linux）

字体族**不写死**，启动时用 `tkfont.families()` 挑第一个真实存在的：

```python
UI_FONT_CANDIDATES   = ("Microsoft YaHei UI", "Segoe UI",
                        "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
                        "Noto Sans", "DejaVu Sans", "Liberation Sans", "Ubuntu", …)
MONO_FONT_CANDIDATES = ("Consolas", "Noto Sans Mono CJK SC", "DejaVu Sans Mono",
                        "Liberation Mono", "Ubuntu Mono", …)
```

Windows 上的 `Microsoft YaHei UI` / `Consolas` 在 Linux 上并不存在，
Tk 不会报错而是**静默替换**：换出来的字形粗细、字面宽都和请求的不是一回事，
中文还可能落到点阵回退族上 —— 界面上就是"字发虚"。候选全都不在时退回
`TkDefaultFont` / `TkFixedFont` 的 `actual('family')`，保证族名一定是本机真实的。

所有画布/ttk 文字还统一走 `_pen()` 把**磅换算成整数像素**，再用 Tk 的
「负 size = 像素」写法（`(族, -13)`）交给渲染器，见踩坑 18。

窄窗口（Linux 小屏很常见）下，搜索/视图那一行放不下时
视图分段按钮会自动换到第二行（见踩坑 19），窗口最小尺寸也不再硬写死。
Debug 面板启动时会打印一行 `platform … · fonts ui='…' mono='…' · 1.333px/pt`，
字体出问题时先看这行。

另外两处只在 Linux 上埋雷的地方：

* **滚轮事件**。`<MouseWheel>` 是 Windows / macOS 专有的虚拟事件，X11 上根本
  没有它 —— 滚轮是按键事件 `<Button-4>`（上）/ `<Button-5>`（下），而且
  `event.delta` 这个属性在 X11 上不存在。绑定时三个一起绑，换算交给
  `_wheel_units()`（见踩坑 21）。**Shift + 滚轮横向滚**也走同一条路：
  X11 的按键事件里 `state` 的 bit0 同样是 Shift，两个平台一起覆盖。
* **进程级崩溃没法在 Python 里抓**。Tk 在 X11 上的崩溃多是段错误，`try/except`
  拦不住，Debug 面板也来不及刷新。所以关键路径（目前是源码视图）每走一步就往
  `%TEMP%/ral_viewer_trace.log`（Linux 即 `/tmp/ral_viewer_trace.log`）追加一行
  并立刻落盘，启动时清空、超过 64KB 自动截断。**崩溃后先打开这个文件** ——
  最后一行就是死在哪一步。同时 `report_callback_exception` 被重写，
  Tk 回调里抛出的 Python 异常会带完整 `traceback` 进 Debug 面板，
  而不是只往 stderr 打一行（见踩坑 22）。

### Debug 面板（界面最下方）

状态栏右侧的 `Hide debug log` / `Show debug log` 按钮控制展开收起，
默认展开。面板里的内容：

* 启动信息（Python / CustomTkinter 版本、DPI 缩放、工作目录）；
* 每次加载：解析了哪几个文件、replace 还是 append、耗时、解析摘要；
* 解析器的每一条 warning / error（文件读不出来、类没有 endclass、
  类重复定义、`add_reg` 对不上实例……）；
* 本次生效的**宏表**（`macros: UVM_REG_ADDR_WIDTH=32, ...`，最多列 12 条）——
  地址/偏移看着不对时，先确认宏有没有按预期展开；
* **解析抛异常时**：异常类型 + 完整 `traceback`，同时状态栏变红并提示
  `see the Debug log`，面板自动展开顶到眼前，不用再去翻控制台。

日志按级别着色（DEBUG 灰 / INFO 常规 / WARN 琥珀 / ERROR 红），
最多保留 600 行，标题右侧实时显示 warning / error 计数，
`Copy` 一键复制全文、`Clear` 清空。

有一个容易忽略的边界：**追加一个读不出来的文件不会把已加载的模型弄丢**。
`_load()` 只在解析成功后才赋值 `self.result`；如果一个文件都没读进来，
还会保留上一份结果，只把错误写进 Debug 面板。

## 解析器怎么做到"通用"

这是本工具的核心。`ral_parser.py` **不硬编码任何厂商前缀**，
`uart_ctrl_reg extends dv_base_reg` 这种自定义继承（`dv_base_reg` 本身只是
`extends uvm_reg`，而且定义通常不在这份文件里）要能照常认出来，靠的是三级判定：

1. **继承链** — 全局建 `class A extends B` 图，递归解析 B 的类别，跨文件有效。
   这是最可靠的一级，`scratch extends vendor_base extends uvm_reg` 这样的两级
   间接继承也能一路追到底。
2. **命名规则** — 基类完全找不到时兜底：`*_reg_block` / `*_block` → block，
   `*_reg_field` / `*_field` → field，`*_reg` → reg，`*_mem` → mem。
3. **类体特征** — 前面都没命中时看类里写了什么：
   含 `create_map` / `add_reg` / `add_submap` / `set_hdl_path_root` → block；
   含 `configure(this, ...)` 或 `super.new(name, <int>, ...)` → reg。

三级都没命中才记 `unknown` 并给出告警——**不会因为认不出基类就把内容丢掉**，
这是"不影响解析"的底线。

字段信息由三处互补拼出：

| 来源 | 提供 |
|---|---|
| 成员声明 `rand uart_ctrl_reg_field rxblvl;` | 字段名、类型、行号 |
| `type_id::create("rxblvl",, get_full_name())` | 实例名 |
| `configure(parent, size, lsb, access, volatile, reset, has_reset, is_rand, individually_accessible)` | 位宽、位置、权限、复位值、属性 |

解析结果分两类反馈：`ParseResult.warnings`（能继续但值得提醒，比如类重复定义）
和 `ParseResult.errors`（硬错误，比如文件根本读不出来），界面上分别以
WARN / ERROR 两级写进 Debug 面板。

寄存器位宽从 `super.new(name, 32, UVM_NO_COVERAGE)` 取；
寄存器整体复位值由各字段的复位值按位拼回来。

### offset 从哪里来（解析不出来时怎么查）

偏移和访问权限优先取 `reg_map.add_reg(ctrl, 'h00, "RW")`，取不到再回退到
`// Register: CTRL @ offset 0x04, RO` 这类注释。`add_reg()` 的**第一个实参
不一定是成员变量名**，生成器各种写法都出现过，所以这里逐级往下试：

| 实参写法 | 举例 | 靠什么认出来 |
|---|---|---|
| 成员名 | `add_reg(ctrl, …)` | 精确匹配 |
| 句柄式 | `add_reg(this.ctrl, …)` / `add_reg(blk.ctrl, …)` | 按 `.` 逐段取尾 |
| 取值式 | `add_reg(ctrl.get_name(), …)` | 剥掉末尾的 `()` |
| 类名 | `add_reg(uart_ctrl_reg, …)` | 别名表（类名 → 实例） |
| create 字面量 | `create("ctrl")` | 别名表（实例名 → 实例） |
| 前缀不一 | 成员 `TOP_CRG` / 句柄 `ral_reg_TOP_CRG` | 唯一后缀匹配 |
| 句柄≠成员 | 成员 `TOP`、`HANDLE = ral_reg::create("TOP")` | 认成**同一个**实例，不产生重复行 |

偏移本身也可以是常量表达式：`add_reg(r, (32'h30 + 4), "RW")` 里的 `+` / `-` / `|`
会算出来（`parse_const_expr()`），外面套几层括号也无所谓。

**别名只有唯一解才生效**：同一个类被实例化多次时按类名反查是有歧义的，
宁可报"对不上"也不会把 offset 安到错的寄存器上。

解析不出来的情况**一律可见**，不会再静默变成一片 `——`：

* 界面上该寄存器显示 `?原文`（比如 ``?`NOT_DEFINED``、`?0x40+`），
  一眼就能区分"模型没写 add_reg"和"写了但没解析出来"；
* Debug 面板出 WARN：`add_reg(…) on line N does not match any register instance`、
  `offset not resolved: <名字> ← <原文> (line N)`、
  `only k/n register offset(s) resolved in block …`；
* 有 `create_map` 但一个 offset 都没落上时额外提示
  `block … has an address map but no register offset was resolved`。

### 宏（`` `define ``）

模型里的数值经常写成宏，最典型的是**地址位宽**：

```systemverilog
csr_map = create_map("csr_map", `UVM_REG_ADDR_WIDTH'h4000, 4, UVM_BIG_ENDIAN, 1);
```

`` `UVM_REG_ADDR_WIDTH'h4000 `` 不展开就解析不出基地址，整条 map 只剩
`4B UVM_BIG_ENDIAN`（`` `h4000 `` 前面挂着个宏，任何整数字面量的正则都匹配不上）。
所以解析前先做一遍**预处理**：

1. **收宏**：扫出所有 `` `define NAME 值 ``（对象宏）。带参数的宏
   （`` `define FOO(x)``）和空值的宏都跳过 —— 前者不是数值，后者替换掉会凭空吃掉一段代码；
   `` \ `` 续行的值拼成一行。
2. **合并**：先铺一层内置默认值，再用**所有文件**里的 `` `define `` 覆盖
   （模型常把位宽定义单独放一个 `*_defines.svh`，跨文件必须能认）。
3. **展开**：只替换表里有的名字，`` `uvm_object_utils `` 这类 UVM 宏原样不动；
   **字符串里不替换**；替换文本不含换行，所以**行号完全不变**，
   后面所有源码定位照旧。

| 宏 | 默认值 | 说明 |
|---|---|---|
| `UVM_REG_ADDR_WIDTH` | **32** | 地址位宽。模型里自己 `define` 了就用模型的值（比如 64） |
| `UVM_REG_DATA_WIDTH` | 64 | 数据位宽 |

实际生效的宏表会写进 Debug 面板（`DEBUG macros: UVM_REG_ADDR_WIDTH=32, ...`），
`--dump` 也会打一行 `== macros:` —— 数值对不上时先看这里。

`samples/generic_base_pkg.sv` 里就埋了一处 `` `UVM_REG_ADDR_WIDTH'h4000 ``，
自测会断言 map 基地址解析成 `0x4000`。

### 通用性测试样本

`samples/` 下两份文件**故意写得不规范**，专门用来打上面这套判定：

* `vendor_base` / `vendor_blk_base` —— 基类名里没有 `uvm_`，也没有 `_reg`；
* `scratch extends vendor_base` —— 只能靠继承链认出是寄存器；
* `vendor_top extends vendor_blk_base`，而 `vendor_blk_base` 定义在**另一个文件**里；
* `side_block` 里有 `uvm_mem`、`add_mem`、`add_submap`、`lock_model()`；
* 偏移写成 `'h1C` 这种省略位宽的形式，map 是 BIG_ENDIAN。

自测会逐条断言这些。

## 自测

```powershell
python ral_viewer.py --selftest
```

覆盖内容：

* 解析正确性 —— 7 个寄存器 / 32 个字段 / 7 个偏移；`uart_ctrl_reg` 的类与基类、
  32 位宽、8 个字段、`rxblvl` 的 `[8:7] RW` 2bit、`{ctrl}` HDL 路径；
  block 的 `base=0x0 / 4B / tb.dut.uart / lock_model`；
* 通用性 —— 上面那 5 条自定义继承与跨文件实例化；
* 宏 —— `` `UVM_REG_ADDR_WIDTH `` 默认 32；样本里
  `` create_map("csr_map", `UVM_REG_ADDR_WIDTH'h4000, ...) `` 的基地址必须解析成
  `0x4000`、两个寄存器偏移为 `0x4000 / 0x4004`；字符串里的反引号不替换、
  带参数/空值的宏不收、续行的宏要拼成一行；再临时生成一个把
  `` `UVM_REG_ADDR_WIDTH `` 定义成 64 的模型，断言**文件里的定义优先于默认值**；
* offset 解析 —— 临时生成一份模型，让 `add_reg()` 分别写成
  `this.X` / `X.get_name()` / 类名 / create 字面量 / 常量表达式 / 未知宏，
  断言前几种都拿到正确偏移、未知宏的那条保留原文并出 WARN；
  同名不同实例的类名反查必须**拒绝猜**；有 `create_map` 却完全没有 `add_reg`
  的 block 必须出 WARN；句柄名与成员名不一致时只能有**一条**寄存器记录；
* 字体 —— 解析出来的族名必须是本机 `tkfont.families()` 里真实存在的、
  画布字号必须是负的整数像素、量宽字体与画布字体同族同像素、
  没解析出来的 offset 显示成 `?原文`；
* 控制字符 —— 临时生成一份内嵌 `\x00` / `\x01` 的模型，断言能正常解析出寄存器、
  解析结果（类名 / 实例名 / 复位值 / offset 原文 / 注释 / 告警）里**一个控制字符都不剩**；
* 滚轮 —— `_wheel_units()` 对 X11（`num=4/5`，无 `delta`）、Windows（`±120`）、
  macOS（`±1`）三种事件都要换算出一致的上/下方向；「全部位图」和「位图」两个画布
  都必须同时绑着 `<MouseWheel>` / `<Button-4>` / `<Button-5>`，
  且分别指向 `on_wall_wheel` / `on_bitmap_wheel`；
  内容装得下视口时还会真滚一格验证 `yview` 确实动了；
* 界面 —— 模型树节点联动、搜索过滤、结构树收起/展开、五个视图的行数、
  位图元素数、源码定位、CSV 导出内容；
* 位图 —— 高低位标注的**位置**必须与标注内容一致（左端 `MSB  bit31`、
  右端 `LSB  bit0`），字段覆盖的位号越大格子 x 越小（低位在右）；
  字段图例按阅读顺序（先上后下、先左后右）取出的位号必须**单调不增**，
  即图例也是高位在前；
* 全部位图 —— 画出的位带数等于范围内寄存器数、过滤后数量随之变化、
  每行都有高低位标注、每一行的字段图例同样是高位在前、
  悬停能显示字段详情、点击能聚焦到对应寄存器；
* 横向拉长 —— 两个视图各拉一遍：格宽必须真的变大、画进位带里的字段名**只增不减**，
  拉到最高档时 `uart_ctrl_reg` 的 8 个字段名**一个不剩地全部进格子**
  （不留图例）、画布变成可横向滚动、Shift + 滚轮（X11 / Windows 两种事件）
  真的能左右滚、缩放条文字跟着档位走，最后 `Fit` 必须回到 1× 且格宽复原；
  「Fit 真的 fit」这条用 `_fit_cell()` 在 680 / 560 / 360 / 240px 宽、
  32 / 64bit 的组合上单独验一遍（窄窗口 = Linux 上最常见的场景）；
* 源码视图 —— 必须定位到 `uart_ctrl_reg` 的代码、选中行要有 `>` 标记、
  分隔线是纯 ASCII（`-` × 76）、插进文本框的串里**不能有控制字符**、
  最后再临时生成一个带 NUL 的文件验证 `_read_lines()` 能安全读回；
* Debug 面板 —— 追加一个不存在的文件后：面板里必须出现该文件名和一条
  `ERROR`、面板自动展开、ERROR 计数 +1、**已加载的模型不能被清空**、
  状态栏切到错误色。

截图输出到 `_selftest/ral_*.png`（深色 7 张 + 浅色 1 张：`ral_debug_error_dark.png`
是解析失败时的 Debug 面板，`ral_bitmap_zoom_dark.png` 是位图拉长到 8×、
字段名全部进格子、底部出现横向滚动条的样子），
全部通过退出码 `0`，有断言失败为 `1`。

实测（Python 3.12.10 / CustomTkinter 6.0.0）：`[selftest] OK - all assertions passed`，
退出码 0。

## 踩坑记录

**1. SystemVerilog 里 `'` 不是字符串定界符**

`configure(this, 1, 0, "RW", 0, 1'b0, 1, 0, 0)` —— 如果按 C 的习惯把 `'` 也当成
引号起始，`1'b0` 的 `'` 会一路吞掉后面所有文本，复位值变成跨行的垃圾。
`find_matching()` / `split_top_args()` 里只认双引号为字符串定界符，
`'` 一律当基值前缀处理。

**2. 空实参位不能丢**

`create("f0",, get_full_name())` 中间那个空位是有意义的，早期版本
`split_top_args()` 过滤掉空串后参数整体左移，`configure` 的位置全错。
现在按原样返回，空位保留。

**3. 无形参的调用也要匹配**

`create_map(...)` / `lock_model()` 没有 `obj.` 前缀，只匹配 `\w+\.\w+\(` 会全漏。
补了一条带负向后顾 `(?<![\w.\`$:])` 的裸调用规则，再用关键字白名单
（`if` / `for` / `case` / `new` / `this` …）排除语法结构。

**4. 注释要先遮掉，但不能动行号**

`//` 会出现在字符串里，注释里也可能带 `"`。做法是把注释整段替换成等长空格，
`\n` 保留，这样所有偏移和行号仍然对得上，`// Register: XXX @ 0x04, RO`
这类信息则在遮蔽前单独存表备用。

**5. 高 DPI 下 CTk 与 ttk 的口径不一致**

本机缩放 200%。CustomTkinter 会把 `geometry()` **和**控件尺寸/字体都乘上缩放
系数，但 `ttk.Treeview` 是原生控件，列宽、行高、字体都不跟着走——结果 CTk 的
字大了一倍、表头挤成一团。办法是用 `ScalingTracker.get_window_dpi_scaling()`
拿到系数存进 `self._ui_scale`，在 `_configure_columns()` 里把列宽
（`width=round(width*scale)`）和行高（`23*scale`）手动乘上去。

**6. `CTkSegmentedButton.set()` 不触发 `command`**

程序里切视图（比如点寄存器跳到源码）用 `.set()` 是静默的，界面不动。
所以另外写了 `goto_view()`：`view_var.set()` + `segment.set()` + 手动 `refresh()`。

**7. 1bit 字段在位图里"看起来没填色"**

窄字段的画法本来是「放不下就只画标签」，1bit 字段宽度不足 `span_px`，
结果整格空着像没数据。改成**格子永远填字段颜色**，窄字段的名字挪到下方
按实际像素宽度换行的图例里，用 `tkfont.Font.measure()` 精确量宽。

**8. 一行文字里靠空格排版，位置会跑到对面去**

位图的位序标注最早写成一个左对齐的字符串：

```python
cv.create_text(left, y, anchor="nw",
               text=f"bit31 <- high" + " " * 8 + "low -> bit0")
```

后半句是用 8 个空格顶过去的，而空格的宽度取决于字体回退后的实际字体 ——
结果「low -> bit0」并没有落在位带右端，而是飘到了位带中段（也就是**高位那一侧**）
的正下方。看上去就像高低位标反了。正确做法是让两端各自锚在自己的端点上，
位置和内容就不可能对不上：

```python
cv.create_text(left, y,  anchor="nw", text=f"MSB  bit{bits-1}")   # 左端
cv.create_text(right, y, anchor="ne", text="LSB  bit0")           # 右端
```

顺带把字段名在色块里的居中算法也修了：原来把「字段最低位格子的左边缘」写成了
`left + (bits - lsb) * cell`（其实是最高位格子的**右**边缘），名字整体右偏半格。

同一个坑的另一半是**图例的顺序**：`reg.fields` 是按 lsb 升序排的（「字段」表格
也按这个顺序列，没问题），但直接拿去排图例，就成了「图例第一个 chip 是最低位，
而图里最左边是最高位」——读图例和看位带是反着的。位图里的图例改成
`reversed(reg.fields)`，两个方向就统一成 MSB → LSB 了。

**9. `Canvas` 没有 `yscrollincrement` 时滚轮几乎不动**

「全部位图」用 `yview_scroll(step, "units")` 滚轮翻页，而 canvas 的默认
`yscrollincrement` 是 0，此时一个 unit 只有窗口高度的 1/10，滚起来一顿一顿。
显式设成 `yscrollincrement=24`，一格滚轮 = 72px。

**10. `CTkSegmentedButton` 内嵌按钮的文字颜色不跟着换肤**

切到浅色后，分段按钮里没选中的几个视图名仍是创建时那套浅灰字，
压在白底上几乎看不见。查下来是 `CTkSegmentedButton` 没有把
`_set_appearance_mode` 传给它内部那几个 `CTkButton`，而 `text_color`
在探针里一直是 `['#DCE4EE', '#DCE4EE']` —— 深浅两档被解析成了同一个值，
所以无论怎么切都不会变。办法是在 `_apply_theme()` 里显式刷一遍：

```python
self.segment.configure(
    fg_color=pal["head"], selected_color=pal["sel"], selected_hover_color=pal["sel"],
    unselected_color=pal["head"], unselected_hover_color=pal["grid"],
    text_color=pal["fg"], text_color_disabled=pal["muted"],
)
```

顺带好处是分段按钮的配色和表格、树保持同一套。

**11. 英文比中文长，原来"刚好放得下"的地方全溢出了**

界面文案换成英文之后连着踩了三个地方，都是同一类问题：

* 顶部工具栏按钮互相压叠 —— 5 个按钮 + 标题 + 外观菜单的总宽超过了
  按屏幕算出来的窗口宽度（1440×900 屏上只有 700 逻辑像素），
  `weight=1` 的那一列被挤到比按钮请求宽度还小，网格不会自动让位，
  按钮就直接盖到邻居身上。把各按钮宽度按文字重新收紧就好了；
* 位图/全部位图的标题行被右边裁掉 —— 用 `wraplength` 让标签自动换行，
  注意 CTkLabel 的 `wraplength` 是**逻辑像素**，要拿画布物理宽度除以缩放系数；
* 换行之后图例被挤没了 —— `tk.Canvas` 的默认请求高度是 265px，
  `pack` 空间不够时先从最后装的控件开始裁，被裁的就是图例。
  把 canvas 的 `height/width` 请求值设小（反正 `expand=True` 会撑满）就稳定了。

同一条线索下还有个小设计：图例固定两行，鼠标悬停时也补一个 `\n` 凑满两行，
否则图例一会一行一会两行，会顶得上面的画布上下跳。

**12. 画布上的字号是"磅"，不是"像素"——写死的行距高 DPI 下全叠在一起**

这是「字体发虚」的真正原因，看上去像字体渲染问题，其实是**排版问题**：
`create_text(..., font=(FONT, 10))` 里的 `10` 是磅，Tk 按 `tk scaling` 换算成像素，
本机 200% 缩放时 1 磅 ≈ 2.67px，**10 磅字的行高是 27px 而不是 10px**。
而位图里的格高写死 30px、图例 chip 高写死 20px、左栏两行文字的行距写死 14px ——
这些数字在 100% 缩放下刚好，200% 下第二行字直接压在上一行上，
两行文字的灰度互相叠印，看起来就是"糊成一团/有重影"。

办法是**量出来再排**，一个字面量都不留：

```python
def _lh(self, size, family=FONT) -> int:      # 该字号在当前缩放下的真实行高
    return int(self._tk_font(size, family).metrics("linespace"))

cell_h   = self._lh(9) + 8                    # 格高 = 一行字段名 + 内边距
axis_h   = self._lh(8) + 4
chip_h   = self._lh(size) + 8                 # 图例 chip 跟着字高走
gutter_h = self._lh(10) + self._lh(8)         # 左栏是上下两行
```

`font=(FONT, size)` 和 `tkfont.Font(family=FONT, size=size)` 在同一个
`tk scaling` 下像素度量完全一致，所以量出来的行高可以直接拿去排版。
**推论**：紧凑模式下格子里只放得下一行字，位号就**不能再画**（原来画在格底，
正好和居中的字段名叠满整格）；位号改到只有单寄存器大图才画，而且要用
`measure("00")` 试出塞得进格宽的字号，塞不进就不画。

同一条线索的另一半是**坐标不取整**：`cell = (width-24)/bits` 常常是 21.03 这种小数，
相邻格子落在半像素上，ClearType 会给文字加一圈灰边，也表现为"发虚"。
现在 `cell`、`left`、`top` 全部 `round()` 成整数。

**13. 每个 bit 画一个矩形，代价是 3888 个图元**

「全部位图」原来对每条位带按位画矩形 + 位号：54 个 32bit 寄存器就是
`54 × (32 矩形 + 32 文字) ≈ 3800` 个图元，重绘要 110ms 以上，
滚轮每格 9ms（一次滚轮的绘制量都在这里）。

实际上**字段是整段连续的**，所以改成：

1. 先铺**一层**底色矩形（代表没字段覆盖的保留位）；
2. 再按字段**一段一个**矩形盖上去（宽度 = `span * cell`）。

同一张图从 3888 个图元降到 702 个（54 寄存器 × 13），重绘 22ms、滚轮每格 2.9ms。
`owner`（bit → 字段）那张表**照旧逐位建**，悬停/点击/自检的几何判断都不受影响 ——
它只是不再参与画图。大图里为了保留"保留位也有格子线"的观感，额外补了
几根竖直分隔线，但只在「相邻两个 bit 都没有字段」处补。

**14. `Font.measure()` 是一次 Tcl 往返，比建图元还贵**

改完图元数量后重绘仍有 70ms，cProfile 一跑就露馅了：
`_tkinter.tkapp.call` 吃掉 0.33s / 5 次，其中大头是 `font.measure` ——
**一次 measure 约 0.08ms，一轮渲染要调 648 次**（每个字段名、每条图例、
每个位序标注都要按像素宽裁剪）。两处修：

* `_tw()` 按 `(family, size, text)` 缓存量过的宽度（跨重绘复用）；
* `_fit_text()` 从「从尾到头逐个试」改成**二分** —— 真实模型里
  `TOP_CTRL_STATUS` 这种长名字一次裁剪就要量二十来次。

改完 cProfile 里 measure 的累计时间从 50ms/轮 掉到 3ms/轮。

**15. `<Configure>` 是每个像素都会触发的**

位图和全部位图的画布都绑了 `<Configure>` → 整幅重画。拖动窗口边缘或者
分栏拉伸时这个事件会连着来几十次，每次都重画就是幻灯片。
用 `_debounce()` 挂 `after(90ms)`，只有停下来了才真正画一次：

```python
job = self._after_id
if job: self.after_cancel(job)
self._after_id = self.after(90, redraw)
```

**16. `` `UVM_REG_ADDR_WIDTH'h4000 `` 这类"宏当位宽"的字面量**

位图/表格里所有整数字面量都走同一个 `parse_int_literal()`，它认的是
`64'h10`、`'h10`、`0x10`、`123` 这几种。而真实模型里大量写成

```systemverilog
csr_map = create_map("csr_map", `UVM_REG_ADDR_WIDTH'h4000, 4, UVM_BIG_ENDIAN, 1);
```

`'` 前面挂的是**宏**而不是数字，正则匹配不上，`parse_int_literal` 返回 `None` ——
**不报错、不告警，基地址就静悄悄地没了**（界面上只显示 `4B UVM_BIG_ENDIAN`，
看起来像是"这个模型没写基地址"）。这类静默失败最难查。

修法是在**解析前**做一遍轻量预处理（收宏 → 合并 → 展开，见上文"宏"一节），
而不是去改 `parse_int_literal`：展开后所有下游（`create_map` / `add_reg` /
`configure` / `super.new`）一行都不用动，`parse_int_literal` 看到的还是标准字面量 `64'h4000`。

两个必须守住的约束：

* **展开不能引入换行**。`line_of()`、文档提取、源码定位全靠行号，
  所以续行宏的值要拼成一行，替换文本里不能有 `\n`。
* **字符串里不能替换**。`"..."` 里的反引号不碰，否则形如
  `` `uvm_info("x", `UVM_REG_ADDR_WIDTH) `` 的调用会把字符串也改掉。

**17. 悬停时不要让 `CTkLabel` 反复 `configure()`**

鼠标在位图上移动时每个 `<Motion>` 事件都会调一次 `_set_legend()`，
即使算出来的文字和当前显示的**一模一样**也会触发 CTkLabel 重设文本 + 重绘，
拖动滚动条时这个开销很可观。按 `id(label)` 缓存当前文本，只在不相等时才写。

**18. Linux 上字体还是发虚：族名不存在 + 字号是小数**

踩坑 12 解决了"排版把两行字叠在一起"造成的发虚，但到 Linux 上仍然发虚，
是两个新的原因叠在一起：

1. **族名不存在**。`Microsoft YaHei UI` / `Consolas` 是 Windows 专有，
   Tk 查不到时不报错，而是交给 fontconfig **静默替换**。替换出来的字形
   字面宽、粗细都和请求的不是同一个，中文还可能落到点阵回退族上 ——
   一个字里半截是点阵半截是轮廓，看着就是"糊"。现在启动时按候选表挑
   真实存在的族（Linux 落到 Noto / DejaVu / WenQuanYi），见上文跨平台一节。
2. **字号是小数**。Tk 的字号单位是磅，`tk scaling` 在 Linux 上常见
   1.3333（96dpi）或 1.3889（100dpi），10 磅 = **13.33px**。小数像素交给
   Xft/FreeType 栅格化，一个字模会落在半格上 —— 这就是 X11 上典型的"发虚"。
   Windows 的 GDI 会先取整，所以同一个界面在 Windows 上看着还行。

修法是把磅**取整成像素**再交给 Tk（Tk 的约定：**负字号就是像素**）：

```python
self._px_per_pt = float(self.winfo_fpixels("1p"))    # 1 磅 = 多少像素

def _pen(self, size):  return max(6, int(round(size * self._px_per_pt)))
def _cf(self, size, family=""):  return (family or FONT, -self._pen(size))   # 画布用
def _tk_font(self, size, family=FONT):                                        # 量宽用
    return tkfont.Font(family=family, size=-self._pen(size))
```

两个附带好处：`_tk_font().measure()` 和画布实际绘制**必然**同口径
（以前一个是磅、一个是 Canvas 内部换算，边缘上会差一两个字符）；
`_lh()` 量出来的行高也变成整数，`round()` 之后坐标不会再出现半像素。

ttk / `tk.Text` 同样这么处理（`font=(FONT, -body_px, "bold")`），
它们不受 CustomTkinter 的缩放影响，得自己乘 DPI —— 但注意乘完要取整成像素，
别把 CTk 的缩放系数直接当磅值用。

**19. `grid()` 在空间不够时是"裁剪"，不是"换行"**

Linux 小屏（逻辑宽度只有 ~680px）上，搜索栏那一行的
`输入框 + Clear filter + 6 个视图分段按钮 + Hide tree` 加起来远比窗口宽 ——
Tk 的 `grid` **不会自动换行**：它只把 `weight>0` 的列一路压缩，
压无可压就把右边的列**直接推到窗口外**，于是视图分段按钮整块消失。
（`sticky` 和 `wraplength` 都救不了，这是 grid 的算法决定的。）

按"除输入框外的固定控件宽度 + 输入框最小 140px"判断放不放得下，
放不下就把分段按钮 `grid_configure(row=1, columnspan=4, sticky="ew")`
换到第二行，窗口拉宽了再换回来。窗口的 `minsize` 也不能写死逻辑像素值，
否则屏幕本来就窄时它比屏幕还宽，窗口再也缩不小。

**20. `add_reg()` 的第一个实参不一定是成员变量名 —— 对不上就全块丢 offset**

最隐蔽的一处静默失败：`build_block()` 里原来是
`var = argl[0].strip(); reg = regs.get(var)`，**精确匹配成员变量名**。
只要模型不这么写，整块的 offset 全部变空（界面上就是所有寄存器一排 `——`），
而且**没有任何告警** —— 因为不匹配只是往 `blk.notes` 里塞了一句话，
而 `notes` 从来没进过界面，只在命令行 `--dump` 里才看得见。

真实模型里出现过的写法：`add_reg(this.TOP_CRG, …)`、`TOP_CRG.get_name()`、
`add_reg(ral_reg_rubx_TOP_CRG, …)`（类名）、`create("TOP_CRG")` 的字面量、
成员叫 `TOP_CRG` 而 build 里句柄叫 `ral_reg_rubx_TOP_CRG`……
现在这些全都能认（`resolve_var_ref()` + 别名表 + 唯一后缀匹配），
并且：

* 偏移支持常量表达式（`(32'h30 + 4)`、`'h10 | 4`）；
* **别名只有唯一解才用**，同一个类实例化多次时按类名反查有歧义，
  宁可报"对不上"也不猜（猜错等于给用户一个错的地址，比空着更糟）；
* 带 `⚠ ` 前缀的 block note 会被 `parse_files()` 升级成
  `ParseResult.warnings`，直接进 Debug 面板 / 状态栏；
* 界面上没解析出来的 offset 显示成 `?原文`，和"模型真的没写"（`——`）区分开。

**21. X11 上没有 `<MouseWheel>` —— "全部位图"在 Linux 上滚不动**

Windows 上滚得好好的，Linux 上滚轮完全没反应（只能拖右边的滚动条）。
原因很简单但很容易漏：`<MouseWheel>` 是 Windows / macOS 的虚拟事件，
**X11 上不存在**；X11 的滚轮是按键事件 `<Button-4>`（上滚）/ `<Button-5>`（下滚），
而且 `event.delta` 在 X11 上**根本没有这个属性**：

```python
def on_wall_wheel(self, event):          # 旧写法：Linux 上永远不会被触发，
    self.wall_canvas.yview_scroll(        #     就算触发也会 AttributeError
        -1 if event.delta > 0 else 1, "units")
```

改成三个事件都绑、再把两层平台差异收进一个换算函数：

```python
widget.bind("<MouseWheel>", handler)     # Windows / macOS
widget.bind("<Button-4>", handler)       # X11 上滚
widget.bind("<Button-5>", handler)       # X11 下滚

def _wheel_units(event):                 # 上滚 -1、下滚 +1，无事件信息返回 0
    if getattr(event, "num", None) == 4: return -1
    if getattr(event, "num", None) == 5: return 1
    delta = getattr(event, "delta", 0) or 0
    return 0 if not delta else (-1 if delta > 0 else 1)
```

绑定要覆盖鼠标可能停住的**每一个**控件：画布、包着画布的 frame、提示文字、
图例、甚至滚动条本身 —— Tk 的事件只发给指针正下方那个控件，不会往父控件冒泡。
另外画布必须有 `yscrollincrement` 和 `scrollregion`，否则 `yview_scroll` 是空操作
（`scrollregion` 已经在 `render_all_bitmaps()` 里按内容高度设好了）。

原生 `Text`（源码视图 / Debug 面板）则是**另一套事实**：实测
`bind_class('Text', '<Button-4>')` 在 Windows 上是空的，只有 `<MouseWheel>`；
X11 那两头挂不挂取决于 Tk 内部按窗口系统的判断。与其赌 Tk 的版本/平台分支，
不如自己绑上并 `return "break"` —— `break` 会掐掉后面的类绑定，所以
无论 Tk 那边有没有，一次滚动都只滚一格（不会滚双份）：

```python
box.bind("<Button-4>", handler); box.bind("<Button-5>", handler)
def handler(event):
    box.yview_scroll(self._wheel_units(event), "units")
    return "break"           # tkinter 会把 "break" 返回给 Tcl 的绑定脚本
```

**22. 点 Source 就段错误退出：控制字符 + 逐字符字体回退**

Linux 上点 `Source` 直接 segmentation fault（Windows 上同样操作没事）。
段错误是 C 层崩溃，Python 侧 `try/except` 抓不到，只能从"往 Tk 控件里塞了什么、
塞给了谁"反推。源码视图当时踩了四处：

1. **内嵌 NUL**。文件是用 `read_text(errors="replace")` 读的，非法字节会变成
   U+FFFD，但 **`\x00` 是原样保留的** —— Tk 8.6 的 `Text` 处理内嵌 NUL 是历史
   遗留崩溃点，X11 上尤其明显。现在在读文件这一步就清掉所有 C0 控制符
   （保留 `\t` / `\n`，`\r` 一起清掉，CRLF 变 LF 不影响行号）：
   `_FILE_CTRL_RE` 在 `ral_parser.parse_files()` 里，`ral_viewer` 侧还有一道
   `tk_safe()` 兜底（Debug 面板的日志文本同样要过）。
   **这是根部的修法** —— 这类字符一旦漏进解析结果，就会顺着 offset 原文 /
   字段名 / 注释流到 Treeview、Text、Canvas 上，逐个控件去堵是堵不完的。
2. **控件选型**。整个程序里把"文件内容整片贴进控件"的只有这一处，
   而它偏偏也是唯一用到 `CTkScrollbar`（`CTkTextbox` 内部自带一个）的地方。
   对照组：Debug 面板贴的文本量一样大，用原生 `Text` + `ttk.Scrollbar`，
   在 Linux 上从没出过问题。所以源码视图改成和它**同一套实现**
   （原生 `Text` + `ttk.Scrollbar`，颜色在 `_apply_theme()` 里自己刷），
   把 CTk 的绘制/滚动条路径从这条链上摘掉；字号口径保持不变
   （`mono_px`：CTk 内部给字号乘的是 DPI 缩放系数，这里按同口径换算成像素）。
3. **非 ASCII 字形**。行号标记 `▸` / 分隔线 `─` / 竖线 `│` 在等宽字体里未必存在，
   X11 下要**逐字符**走 fontconfig 回退去找字形 —— 换成纯 ASCII 的 `>` `-` `|`
   就不存在这个问题（ASCII 任何字体都有）。
4. **逐行插入**。原来每行一次 `source_box.insert()`，一个 block 就是几千次 Tcl
   往返；现在先在 Python 里拼好整段、过一次 `tk_safe()`、**一次**插入，
   顺带把行数上限压到 3000 行。

还补了两件"下次再崩也不抓瞎"的事：渲染过程包在 `try` 里（异常进 Debug 面板
而不是冒泡），以及关键步骤写面包屑到 `%TEMP%/ral_viewer_trace.log`：

```
19:50:03 source: show begin
19:50:03 source: textbox gridded
19:50:03 source: render begin
19:50:03 source: reading uart_ral_pkg.sv
19:50:03 source: inserting 2757 char(s)
19:50:03 source: render done
```

**最后一行停在哪儿，就死在哪儿。** 另外 `report_callback_exception` 也重写了：
Tk 回调里的 Python 异常现在会带完整 traceback 进 Debug 面板（以前只在终端打一行，
从桌面图标启动时等于什么都看不见）。

> 顺带回答一个容易走偏的方向：这个工具**不会**调用 `xdg-open` / `os.startfile` /
> 子进程去"打开"文件（全文没有 `subprocess` / `os.system`），源码视图只是
> `Path.read_text()` + 往文本框里插字符串。所以问题不在"Linux 下打开文件的方式"，
> 而在**读回来的内容**交给了谁。

**23. 自适应格宽永远差几像素 —— 于是 1× 下就有一条要滚不滚的横向滚动条**

位图最早只有纵向滚动，格宽一算出来就完事，没人关心它是不是恰好等于视口宽。
加横向拉长时把「内容宽度」也交给画布做了（`scrollregion`），问题立刻现形：

```python
auto = round(min(44.0, (width_px - 24) / bits))     # 版本 A：四舍五入
content_w = max(width_px, 12 + bits * cell + 16)    # 左边距 12 + 右边距 16
```

两个毛病叠在一起：`round()` 会把 26.9 变成 27，右边距的账也没算准
（减的是 24，实际占了 28）。结果 1× —— 也就是标签上明写着 `(fit)` 的那一档 ——
内容比视口宽了 8px，横向滚动条冒出来，拖动却几乎什么都不动，看着像个 bug。

修法是把「自适应」这件事收进一个函数，它同时负责**取整方向**和**边距口径**：

```python
def _fit_cell(width_px, bits, left, right_pad, *, cap, floor):
    auto = int((width_px - left - right_pad) / max(bits, 1))   # 向下取整
    return float(max(floor, min(cap, auto)))
```

* **向下取整**（`int()` 而不是 `round()`）：内容宽度是拿返回值倒推出来的，
  只有向下取整才能保证 `left + bits*cell + right_pad ≤ width_px`，
  「Fit」才是真的 fit；
* **边距口径写在一起**：调用处和算内容宽度的地方共用同一组数字，
  加一条边距不会再漏改另一处；
* `floor` 是"挤不下就算了"的下限（8px/bit 单图、7px/bit 多图）。
  240px 宽的画布要画 64bit 寄存器，硬算只有 5px/bit —— 那种宽度下格子就是
  几条看不清的竖线，不如维持下限、老老实实横向溢出 + 出滚动条。
  窄窗口（Linux 上常见的 ~680px）走的正是这条分支。

另一半是**坐标口径**：能横向滚动之后，`event.x` 是「窗口内」坐标，
而画布上的图形按「画布」坐标摆，两者差了 `xview` 的偏移量 ——
悬停/点击全部要过一遍 `canvasx(event.x)`，否则拉长后鼠标指着的格子和
高亮出来的字段对不上。同一个坑，滚轮也一样：Shift + 滚轮横向滚之前先看
`_overflow_x`（没超宽就别动），免得手一抖视图跑到别处。

**24. 「拉长」这件事需要一张共享的档位表**

- 档位表是**几何级数**（`1 → 1.25 → 1.5 → 2 → 2.5 → 3 → 4 → 6 → 8`）而不是
  加减固定像素：40bit 的寄存器要的是"格宽翻倍"，20bit 的要的是"多几十像素"，
  固定步长必然一档太小、下一档太大；
- 档位存在**视图之外**（`self.cell_zoom`），两个位图视图共享 ——
  否则在 Bitmap 拉到 4×、切到 All Bitmaps 又变回 1×，
  两边同一个 bit 的宽度不一样，看着像画错了；
- 每次重绘都**记住 `xview()` 并回填**（`keep_x = cv.xview()[0]` … `xview_moveto`），
  不然拉一档就弹回最左边，想看的字段得重新找一遍；
- 图例的换行宽度也要跟着内容宽度走（拉长后按视口宽换行会白掉一大截）。

