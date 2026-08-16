"""
DataGetTool v0.0.4
用于将 .tar / .tar.gz 文件还原为 .dump 文件，
读取 .bak 文件对应的 SQL Server 版本，
压缩备份（MSSQLBAK 格式）可通过 SQL Server 实例执行 RESTORE HEADERONLY 查询版本，
处理 .ba_ 文件（gzip 或 7z 压缩）解压后添加 .bak 后缀，
导入 .xml 文件并检查/转换 UTF-8 编码，
超过 100MB 的 XML 文件自动拆分为多个子文件。
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk, simpledialog
from tkinter import Menu
import tarfile
import os
import gzip
import tempfile
import threading
import shutil
import struct
import codecs
import re
import time
import sys
import json


# ─── SQL Server 内部版本号映射 ───────────────────────────────────────────────

VERSION_MAP = {
    406: "SQL Server 6.0",
    408: "SQL Server 6.5",
    515: "SQL Server 7.0",
    539: "SQL Server 2000",
    611: "SQL Server 2005",
    612: "SQL Server 2005 SP2+ (VarDecimal)",
    655: "SQL Server 2008",
    660: "SQL Server 2008 R2",
    661: "SQL Server 2008 R2",
    684: "SQL Server 2012 CTP1 (Denali)",
    706: "SQL Server 2012",
    782: "SQL Server 2014",
    852: "SQL Server 2016",
    868: "SQL Server 2017",
    869: "SQL Server 2017",
    895: "SQL Server 2019 CTP 2.0",
    896: "SQL Server 2019 CTP 2.1/2.2",
    897: "SQL Server 2019 CTP 2.3-2.5",
    902: "SQL Server 2019 CTP 3.0/3.1",
    904: "SQL Server 2019 RTM",
    950: "SQL Server 2022 RC 1",
    957: "SQL Server 2022 RTM",
    997: "SQL Server 2025 CTP 1.5",
}

TAPE_MAGIC = b"TAPE"
MSCI_MAGIC = b"MSCI"
MSSQLBAK_MAGIC = b"MSSQLBAK"
OFFSET_AFTER_MSCI = 168

# 压缩备份提示信息（MSSQLBAK 格式：版本号位于专有压缩块内，纯字节扫描无法读取）
COMPRESSED_BACKUP_MSG = (
    "压缩备份（MSSQLBAK 格式，无法直接读取版本号，"
    "可在 SQL Server 中执行 RESTORE HEADERONLY 查询）"
)


def get_sql_version(bak_path):
    """读取 .bak 文件对应的 SQL Server 版本。

    扫描 MTF 格式头部，找到 MSCI 标记后偏移 168 字节读取 2 字节小端版本号。
    返回 (version_string, internal_version)：
    - 普通 MTF 备份：返回识别到的版本；
    - 压缩备份（MSSQLBAK 头）：返回 (COMPRESSED_BACKUP_MSG, None)；
    - 无法识别：返回 (None, None)。
    """
    try:
        with open(bak_path, "rb") as f:
            # 验证文件头：压缩备份以 MSSQLBAK 开头，普通 MTF 备份以 TAPE 开头
            header = f.read(8)
            if header == MSSQLBAK_MAGIC:
                return COMPRESSED_BACKUP_MSG, None
            if not header.startswith(TAPE_MAGIC):
                return None, None

            # 扫描 MSCI 标记
            buffer = b""
            while True:
                chunk = f.read(65536)
                if not chunk:
                    return None, None
                buffer = (buffer + chunk)[-65540:]
                idx = buffer.find(MSCI_MAGIC)
                if idx != -1:
                    # 定位到 MSCI 标记后偏移 168 字节处
                    position_after_msci = f.tell() - len(chunk) + idx + len(MSCI_MAGIC)
                    f.seek(position_after_msci + OFFSET_AFTER_MSCI)
                    break

            # 读取 2 字节小端版本号
            raw = f.read(2)
            if len(raw) < 2:
                return None, None
            version = struct.unpack("<h", raw)[0]
            version_str = VERSION_MAP.get(version, f"Unknown (code: {version})")
            return version_str, version
    except Exception:
        return None, None


def detect_backup_type(bak_path):
    """检测 .bak 文件的备份类型（完整/差异/日志/压缩）。"""
    try:
        with open(bak_path, "rb") as f:
            first = f.read(8)
            if first.startswith(b"MSSQLBAK"):
                return "压缩备份 (Compressed)"
            f.seek(3750)
            ch = f.read(1)
            if ch == b"D":
                return "完整备份 (Full)"
            if ch == b"L":
                return "事务日志备份 (Log)"
            if ch == b"I":
                return "差异备份 (Differential)"
            return "未知类型"
    except Exception:
        return "未知类型"


# ─── 通过 SQL Server 实例查询压缩备份版本 ───────────────────────────────────

# RESTORE HEADERONLY 返回的 SoftwareVersionMajor/Minor → 产品名
SQL_PRODUCT_MAP = {
    (17, 0): "SQL Server 2025",
    (16, 0): "SQL Server 2022",
    (15, 0): "SQL Server 2019",
    (14, 0): "SQL Server 2017",
    (13, 0): "SQL Server 2016",
    (12, 0): "SQL Server 2014",
    (11, 0): "SQL Server 2012",
    (10, 50): "SQL Server 2008 R2",
    (10, 0): "SQL Server 2008",
    (9, 0): "SQL Server 2005",
    (8, 0): "SQL Server 2000",
}

DEFAULT_SQL_CONFIG = {
    "server": ".",
    "auth": "windows",  # windows = Windows 集成认证；sql = SQL Server 登录
    "user": "",
    # 密码不保存：每次查询时弹窗输入
}


def _sql_config_path():
    """配置文件保存在 exe（或脚本）同目录下。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "DataGetTool-sqlserver.json")


def load_sql_config():
    cfg = dict(DEFAULT_SQL_CONFIG)
    try:
        with open(_sql_config_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update(saved)
    except Exception:
        pass
    cfg.pop("password", None)  # 密码不持久化，每次查询时弹窗输入
    return cfg


def save_sql_config(cfg):
    with open(_sql_config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def query_bak_version_via_server(bak_path, cfg):
    """连接 SQL Server 实例执行 RESTORE HEADERONLY 查询 .bak 版本。

    返回 (version_str, error_msg)：成功时 error_msg 为 None。
    注意：DISK 路径在服务器端解析，文件需对 SQL Server 服务账户可读。
    """
    try:
        import pyodbc
    except ImportError:
        return None, "缺少 pyodbc 模块"

    # 选择 ODBC 驱动：优先 17；Driver 18 默认强制加密，本地连接易因证书失败
    available = pyodbc.drivers()
    driver = None
    for cand in ("ODBC Driver 17 for SQL Server",
                 "SQL Server Native Client 11.0",
                 "SQL Server"):
        if cand in available:
            driver = cand
            break
    if driver is None and "ODBC Driver 18 for SQL Server" in available:
        driver = "ODBC Driver 18 for SQL Server"
    if driver is None:
        return None, "未找到可用的 SQL Server ODBC 驱动"

    conn_str = f"DRIVER={{{driver}}};SERVER={cfg.get('server', '.')};"
    if driver.startswith("ODBC Driver 18"):
        conn_str += "Encrypt=no;"
    if cfg.get("auth") == "sql":
        conn_str += f"UID={cfg.get('user', '')};PWD={cfg.get('password', '')};"
    else:
        conn_str += "Trusted_Connection=yes;"

    conn = None
    try:
        conn = pyodbc.connect(conn_str, timeout=10)
        cur = conn.cursor()
        safe_path = bak_path.replace("'", "''")
        cur.execute(f"RESTORE HEADERONLY FROM DISK = N'{safe_path}'")
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
        if row is None:
            return None, "备份文件中未找到备份集"
        data = dict(zip(cols, row))
        major = data.get("SoftwareVersionMajor")
        minor = data.get("SoftwareVersionMinor")
        build = data.get("SoftwareVersionBuild")
        if major is None:
            return None, "无法从备份头读取版本信息"
        product = SQL_PRODUCT_MAP.get(
            (major, minor), f"SQL Server (版本 {major}.{minor})"
        )
        return f"{product} ({major}.{minor}.{build})", None
    except Exception as e:
        return None, str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass



def check_xml_utf8(xml_path):
    """检查 XML 文件是否为 UTF-8 编码。

    返回 (is_utf8, has_bom, declared_encoding)：
    - is_utf8: 实际字节是否为合法 UTF-8（逐块增量解码判定，支持大文件）。
    - has_bom: 是否带 UTF-8 BOM (EF BB BF)。
    - declared_encoding: XML 声明中 encoding 属性的值（未声明或解析失败时为 None）。
    """
    has_bom = False
    declared_encoding = None

    # 读取文件开头，判断 BOM 并解析 XML 声明中的 encoding 属性
    try:
        with open(xml_path, "rb") as f:
            head = f.read(512)
        has_bom = head.startswith(b"\xef\xbb\xbf")
        m = re.search(rb'encoding\s*=\s*["\']([^"\']+)["\']', head, re.IGNORECASE)
        if m:
            declared_encoding = m.group(1).decode("ascii", errors="replace")
    except Exception:
        pass

    # 逐块验证实际字节是否为合法 UTF-8（避免一次性读入大文件）
    try:
        with open(xml_path, "rb") as f:
            decoder = codecs.getincrementaldecoder("utf-8")()
            while True:
                chunk = f.read(65536)
                if not chunk:
                    decoder.decode(b"", final=True)
                    break
                decoder.decode(chunk, final=False)
        return True, has_bom, declared_encoding
    except UnicodeDecodeError:
        return False, has_bom, declared_encoding


def convert_xml_to_utf8(src_path, dst_path):
    """将 XML 文件转换为 UTF-8 编码并保存到 dst_path。

    按 UTF-16 BOM 优先识别，再依次尝试 UTF-8、XML 声明编码、GB18030、Big5。
    成功后将 XML 声明中的 encoding 改为 utf-8（若存在声明），以无 BOM 的 UTF-8 写入。
    返回 (success, source_encoding, error_msg)。
    """
    try:
        with open(src_path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return False, None, f"读取失败: {e}"

    # 解析 XML 声明中的 encoding 属性
    declared = None
    m = re.search(rb'encoding\s*=\s*["\']([^"\']+)["\']', raw[:512], re.IGNORECASE)
    if m:
        try:
            declared = m.group(1).decode("ascii", errors="replace")
        except Exception:
            declared = None

    # 根据 BOM 及声明构建候选编码列表
    if raw[:2] == b"\xff\xfe":
        candidates = ["utf-16-le"]
    elif raw[:2] == b"\xfe\xff":
        candidates = ["utf-16-be"]
    else:
        candidates = ["utf-8"]
        if declared and declared.lower() not in ("utf-8", "utf-8-sig"):
            candidates.append(declared)
        for enc in ("gb18030", "big5"):
            if enc not in candidates:
                candidates.append(enc)

    # 逐个候选编码尝试解码
    source_encoding = None
    text = None
    for enc in candidates:
        try:
            text = raw.decode(enc)
            if text.startswith("\ufeff"):
                text = text[1:]
            source_encoding = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if text is None:
        return False, None, "无法识别编码（已尝试: " + ", ".join(candidates) + "）"

    # 更新 XML 声明中的 encoding 为 utf-8（若存在声明）
    new_text = re.sub(
        r'(<\?xml[^>]*?encoding\s*=\s*["\'])[^"\']+(["\'])',
        r'\1utf-8\2',
        text,
        count=1,
        flags=re.IGNORECASE,
    )

    try:
        with open(dst_path, "wb") as f:
            f.write(new_text.encode("utf-8"))
        return True, source_encoding, None
    except Exception as e:
        return False, source_encoding, f"写入失败: {e}"


def split_large_xml(utf8_path, output_dir, log_fn=None):
    """拆分大 XML 文件为多个子文件。

    每个子文件包含 XML 声明和根元素包裹，保持一级子元素完整。
    目标行数：100万-250万行/文件（按格式化后 >< 之间换行统计）。

    返回 (split_files, error_msg)：
    - split_files: 生成的子文件路径列表（无需拆分时为空列表）。
    - error_msg: 错误信息（无错误时为 None）。
    """
    try:
        with open(utf8_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
    except Exception as e:
        return [], f"读取文件失败: {e}"

    # 提取 XML 声明
    decl_match = re.match(r'<\?xml[^>]*\?>', content)
    if decl_match:
        declaration = decl_match.group(0)
        declaration = re.sub(
            r'encoding="[^"]+"', 'encoding="utf-8"', declaration, count=1
        )
        decl_end = decl_match.end()
    else:
        declaration = '<?xml version="1.0" encoding="utf-8"?>'
        decl_end = 0

    # 查找根元素开标签（跳过空白，处理属性值中的引号）
    root_start = decl_end
    while root_start < len(content) and content[root_start] in ' \t\r\n':
        root_start += 1

    if root_start >= len(content) or content[root_start] != '<':
        return [], "无法找到根元素"

    i = root_start + 1
    in_quote = None
    while i < len(content):
        c = content[i]
        if in_quote:
            if c == in_quote:
                in_quote = None
        elif c in '"\'':
            in_quote = c
        elif c == '>':
            break
        i += 1
    root_open_end = i + 1
    root_open_tag = content[root_start:root_open_end]

    name_match = re.match(r'<([^\s/>]+)', root_open_tag)
    if not name_match:
        return [], "无法解析根标签名"
    root_name = name_match.group(1)
    root_close_tag = f'</{root_name}>'

    root_close_pos = content.rfind(root_close_tag)
    if root_close_pos == -1:
        return [], "无法找到根闭标签"

    inner_start = root_open_end
    inner_end = root_close_pos

    # 扫描一级子元素（深度跟踪，处理引号/注释/CDATA）
    elements = []
    pos = inner_start
    depth = 0

    while pos < inner_end:
        lt = content.find('<', pos)
        if lt == -1 or lt >= inner_end:
            break

        if content[lt:lt + 4] == '<!--':
            ce = content.find('-->', lt + 4)
            pos = (ce + 3) if ce != -1 else len(content)
            continue

        if content[lt:lt + 9] == '<![CDATA[':
            ce = content.find(']]>', lt + 9)
            pos = (ce + 3) if ce != -1 else len(content)
            continue

        j = lt + 1
        in_q = None
        while j < inner_end:
            c = content[j]
            if in_q:
                if c == in_q:
                    in_q = None
            elif c in '"\'':
                in_q = c
            elif c == '>':
                break
            j += 1
        tag_end = j + 1
        tag_text = content[lt:tag_end]

        if tag_text.startswith('</'):
            depth -= 1
            pos = tag_end
        elif tag_text.rstrip().endswith('/>'):
            if depth == 0:
                est_lines = len(re.findall(r'>\s*<', content[lt:tag_end])) + 1
                elements.append((lt, tag_end, est_lines))
            pos = tag_end
        else:
            if depth == 0:
                elem_start = lt
                elem_name = re.match(r'<([^\s/>]+)', tag_text).group(1)
                search_pos = tag_end
                d = 1
                while d > 0 and search_pos < inner_end:
                    next_lt = content.find('<', search_pos)
                    if next_lt == -1 or next_lt >= inner_end:
                        break

                    if content[next_lt:next_lt + 4] == '<!--':
                        ce = content.find('-->', next_lt + 4)
                        search_pos = (ce + 3) if ce != -1 else len(content)
                        continue

                    if content[next_lt:next_lt + 9] == '<![CDATA[':
                        ce = content.find(']]>', next_lt + 9)
                        search_pos = (ce + 3) if ce != -1 else len(content)
                        continue

                    k = next_lt + 1
                    iq = None
                    while k < inner_end:
                        c = content[k]
                        if iq:
                            if c == iq:
                                iq = None
                        elif c in '"\'':
                            iq = c
                        elif c == '>':
                            break
                        k += 1
                    nt_end = k + 1
                    nt_text = content[next_lt:nt_end]

                    if nt_text.startswith('</'):
                        cn = re.match(r'</([^\s/>]+)', nt_text)
                        if cn and cn.group(1) == elem_name:
                            d -= 1
                            if d == 0:
                                elem_end = nt_end
                                break
                    elif not nt_text.rstrip().endswith('/>'):
                        on = re.match(r'<([^\s/>]+)', nt_text)
                        if on and on.group(1) == elem_name:
                            d += 1
                    search_pos = nt_end

                if d == 0:
                    est_lines = len(re.findall(r'>\s*<', content[elem_start:elem_end])) + 1
                    elements.append((elem_start, elem_end, est_lines))
                    pos = elem_end
                else:
                    pos = inner_end
            else:
                depth += 1
                pos = tag_end

    if not elements:
        return [], "未找到一级子元素"

    total_lines = sum(e[2] for e in elements)
    if log_fn:
        log_fn(f"  共 {len(elements)} 个一级子元素，估计格式化后约 {total_lines} 行")

    # 分组：目标每文件 100万-250万行
    TARGET_MAX = 2_500_000
    TARGET_MIN = 1_000_000

    chunks = []
    current = []
    current_lines = 0

    for start, end, est_lines in elements:
        if current and current_lines + est_lines > TARGET_MAX and current_lines >= TARGET_MIN:
            chunks.append(current)
            current = []
            current_lines = 0
        current.append((start, end))
        current_lines += est_lines

    if current:
        chunks.append(current)

    # 无需拆分
    if len(chunks) <= 1:
        if log_fn:
            log_fn(f"  总行数约 {total_lines}，未超过 {TARGET_MAX} 行，无需拆分")
        return [], None

    if log_fn:
        log_fn(f"  将拆分为 {len(chunks)} 个子文件...")

    # 写入子文件
    base_name = os.path.splitext(os.path.basename(utf8_path))[0]
    split_files = []
    for idx, chunk in enumerate(chunks, 1):
        split_name = f"{base_name}_part{idx}.xml"
        split_path = os.path.join(output_dir, split_name)
        try:
            with open(split_path, "w", encoding="utf-8") as f:
                f.write(declaration + "\n")
                f.write(root_open_tag + "\n")
                chunk_lines = 2
                for s, e in chunk:
                    elem_text = content[s:e]
                    formatted = re.sub(r'>\s*<', '>\n<', elem_text)
                    f.write(formatted + "\n")
                    chunk_lines += formatted.count('\n') + 1
                f.write(root_close_tag + "\n")
                chunk_lines += 1
            split_files.append(split_path)
            if log_fn:
                log_fn(f"  已生成: {split_name} ({chunk_lines} 行, {len(chunk)} 个元素)")
        except Exception as e:
            return split_files, f"写入子文件失败: {e}"

    return split_files, None


class DataGetTool:
    def __init__(self, root):
        self.root = root
        self.root.title("DataGetTool v0.0.4")
        self.root.geometry("700x600")
        self.input_file = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.sql_config = load_sql_config()
        self.create_menu()
        self.create_widgets()

    def create_menu(self):
        menubar = Menu(self.root)
        settings_menu = Menu(menubar, tearoff=0)
        settings_menu.add_command(
            label="SQL Server 连接设置", command=self.show_sql_settings
        )
        menubar.add_cascade(label="设置", menu=settings_menu)
        help_menu = Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menubar)

    def show_sql_settings(self):
        """SQL Server 连接设置对话框（用于查询压缩备份版本）。"""
        win = tk.Toplevel(self.root)
        win.title("SQL Server 连接设置")
        win.geometry("380x240")
        win.transient(self.root)
        win.grab_set()

        cfg = dict(self.sql_config)
        server_var = tk.StringVar(value=cfg.get("server", "."))
        auth_var = tk.StringVar(
            value="sql" if cfg.get("auth") == "sql" else "windows"
        )
        user_var = tk.StringVar(value=cfg.get("user", ""))

        tk.Label(win, text="服务器/实例:").grid(
            row=0, column=0, sticky="w", padx=10, pady=(15, 5)
        )
        tk.Entry(win, textvariable=server_var, width=30).grid(
            row=0, column=1, padx=10, pady=(15, 5)
        )

        tk.Label(win, text="认证方式:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        auth_frame = tk.Frame(win)
        auth_frame.grid(row=1, column=1, sticky="w", padx=10, pady=5)
        tk.Radiobutton(
            auth_frame, text="Windows 集成认证", variable=auth_var, value="windows"
        ).pack(anchor="w")
        tk.Radiobutton(
            auth_frame, text="SQL Server 登录", variable=auth_var, value="sql"
        ).pack(anchor="w")

        tk.Label(win, text="用户名:").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        tk.Entry(win, textvariable=user_var, width=30).grid(row=2, column=1, padx=10, pady=5)

        tk.Label(
            win,
            text="密码不保存，每次查询时弹窗输入。\n"
            "提示：备份文件路径在服务器端解析，\n需对 SQL Server 服务账户可读。",
            fg="gray",
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=5)

        def on_save():
            self.sql_config = {
                "server": server_var.get().strip() or ".",
                "auth": auth_var.get(),
                "user": user_var.get().strip(),
            }
            try:
                save_sql_config(self.sql_config)
            except Exception as e:
                messagebox.showerror("设置", f"保存配置失败: {e}", parent=win)
                return
            win.destroy()

        tk.Button(win, text="保存", command=on_save, width=12).grid(
            row=4, column=0, columnspan=2, pady=10
        )

    def show_about(self):
        messagebox.showinfo(
            "关于 DataGetTool",
            "DataGetTool v0.0.4\n\n"
            "用于将 .tar / .tar.gz 文件还原为 .dump 文件\n"
            "读取 .bak 文件对应的 SQL Server 版本\n"
            "压缩备份（MSSQLBAK 格式）自动通过 SQL Server 实例\n"
            "执行 RESTORE HEADERONLY 查询版本（菜单 [设置] 中配置连接）\n"
            "处理 .ba_ 文件（gzip 或 7z 压缩）解压后添加 .bak 后缀\n"
            "导入 .xml 文件：检查所在文件夹所有 XML 是否 UTF-8 编码，\n"
            "非 UTF-8 文件自动转换为 UTF-8 并保存至同级新文件夹\n"
            "超过 100MB 的 XML 文件自动拆分为多个子文件（100万-250万行/个）",
        )

    def create_widgets(self):
        # ─── 输入文件 ───────────────────────────────────────────────────
        tk.Label(self.root, text="输入文件 (.tar / .tar.gz / .tgz / .bak / .ba_ / .xml):").pack(
            anchor="w", padx=10, pady=(10, 0)
        )
        f1 = tk.Frame(self.root)
        f1.pack(fill="x", padx=10)
        tk.Entry(f1, textvariable=self.input_file, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tk.Button(f1, text="选择文件", command=self.select_input).pack(side="left", padx=(5, 0))

        # ─── 输出目录 ───────────────────────────────────────────────────
        tk.Label(self.root, text="输出目录（.bak / .ba_ / .xml 文件可省略）:").pack(anchor="w", padx=10, pady=(10, 0))
        f2 = tk.Frame(self.root)
        f2.pack(fill="x", padx=10)
        tk.Entry(f2, textvariable=self.output_dir, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        tk.Button(f2, text="选择目录", command=self.select_output).pack(side="left", padx=(5, 0))

        # ─── 执行按钮 ───────────────────────────────────────────────────
        self.execute_btn = tk.Button(
            self.root,
            text="执行",
            command=self.execute_extract,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 12, "bold"),
        )
        self.execute_btn.pack(pady=10)

        # ─── 进度条 ─────────────────────────────────────────────────────
        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=10)

        # ─── 日志区 ─────────────────────────────────────────────────────
        tk.Label(self.root, text="执行日志:").pack(anchor="w", padx=10, pady=(10, 0))
        self.log_text = tk.Text(self.root, height=15)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def select_input(self):
        path = filedialog.askopenfilename(
            title="选择 TAR / TAR.GZ / BAK / BA_ / XML 文件",
            filetypes=[
                ("所有支持格式", "*.tar *.tar.gz *.tgz *.bak *.ba_ *.xml"),
                ("TAR 文件", "*.tar"),
                ("TAR.GZ 文件", "*.tar.gz *.tgz"),
                ("BAK 文件", "*.bak"),
                ("BA_ 文件", "*.ba_"),
                ("XML 文件", "*.xml"),
            ],
        )
        if path:
            self.input_file.set(path)

    def select_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir.set(path)

    def log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def execute_extract(self):
        input_path = self.input_file.get()
        output_path = self.output_dir.get()
        lower = input_path.lower()

        if not os.path.isfile(input_path):
            messagebox.showerror("错误", "请选择有效的输入文件")
            return

        # .bak / .ba_ / .xml 文件不需要输出目录
        is_bak_only = lower.endswith(".bak")
        is_ba_ = lower.endswith(".ba_")
        is_xml = lower.endswith(".xml")
        if not is_bak_only and not is_ba_ and not is_xml and not os.path.isdir(output_path):
            messagebox.showerror("错误", "请选择有效的输出目录")
            return

        self.log(f"源文件: {input_path}")
        if output_path:
            self.log(f"输出目录: {output_path}")
        self.log("开始处理...")
        self.execute_btn.config(state="disabled")
        self.progress.start()

        threading.Thread(
            target=self._extract,
            args=(input_path, output_path),
            daemon=True,
        ).start()

    def _ask_password(self, server, user):
        """在主线程弹窗输入 SQL Server 密码（工作线程中调用，阻塞等待）。
        返回密码字符串；用户取消时返回 None。"""
        result = {}
        done = threading.Event()

        def ask():
            try:
                result["pwd"] = simpledialog.askstring(
                    "SQL Server 密码",
                    f"请输入 {user} @ {server} 的密码：",
                    show="*",
                    parent=self.root,
                )
            finally:
                done.set()

        self.root.after(0, ask)
        done.wait()
        return result.get("pwd")

    def _query_compressed_version(self, bak_path):
        """压缩备份：通过 SQL Server 实例查询版本，返回版本字符串或 None。"""
        cfg = dict(getattr(self, "sql_config", None) or load_sql_config())
        if cfg.get("auth") == "sql":
            # 密码不保存，每次查询时弹窗输入
            pwd = self._ask_password(cfg.get("server", "."), cfg.get("user", ""))
            if pwd is None:
                self.log("已取消密码输入，跳过版本查询")
                return None
            cfg["password"] = pwd
        self.log(f"正在通过 SQL Server 实例 ({cfg.get('server')}) 查询版本...")
        qver, qerr = query_bak_version_via_server(bak_path, cfg)
        if qver:
            self.log(f"查询成功: {qver}")
            return qver
        self.log(f"查询失败: {qerr}")
        self.log(
            "提示: 可在菜单 [设置 → SQL Server 连接设置] 中修改连接配置；"
            "备份文件路径需对 SQL Server 服务账户可读"
        )
        return None

    def _extract(self, input_path, output_path):
        try:
            lower = input_path.lower()

            # ─── 处理 .ba_ 文件：按实际格式解压 → 添加 .bak 后缀 ─────────
            if lower.endswith(".ba_"):
                self.log("检测到 .ba_ 文件，正在处理...")
                if not output_path:
                    output_path = os.path.dirname(input_path)

                base_name = os.path.basename(input_path)[:-4]  # 去掉 .ba_ 后缀
                extract_dir = os.path.join(output_path, base_name + "_extracted")
                os.makedirs(extract_dir, exist_ok=True)

                # 读取文件头以判断真实压缩格式
                with open(input_path, "rb") as f:
                    magic = f.read(6)

                try:
                    if magic[:2] == b"\x1f\x8b":
                        # gzip 压缩（常见于 SQL Server 备份重命名为 .ba_）
                        self.log("检测到 gzip 压缩格式，正在解压...")
                        decompressed_path = os.path.join(extract_dir, base_name)
                        with gzip.GzipFile(input_path, "rb") as f_in, open(decompressed_path, "wb") as f_out:
                            while True:
                                chunk = f_in.read(65536)
                                if not chunk:
                                    break
                                f_out.write(chunk)
                        self.log("gzip 解压完成")
                    elif magic == b"\x37\x7a\xbc\xaf\x27\x1c":
                        # 7z 压缩
                        self.log("检测到 7z 压缩格式，正在解压...")
                        temp_7z_path = os.path.join(
                            tempfile.gettempdir(), base_name + "_" + str(os.getpid()) + ".7z"
                        )
                        shutil.copy2(input_path, temp_7z_path)
                        try:
                            import py7zr
                            with py7zr.SevenZipFile(temp_7z_path, mode="r") as z:
                                z.extractall(path=extract_dir)
                            self.log("7z 解压完成")
                        finally:
                            try:
                                if os.path.exists(temp_7z_path):
                                    os.unlink(temp_7z_path)
                            except Exception:
                                pass
                    else:
                        raise ValueError(
                            f"不支持的 .ba_ 文件格式（文件头: {magic.hex()}），"
                            "应为 gzip（1f8b）或 7z（377abcaf271c）"
                        )
                except Exception as e_ba:
                    self.log(f".ba_ 解压失败: {e_ba}")
                    shutil.rmtree(extract_dir, ignore_errors=True)
                    self.root.after(0, lambda: messagebox.showerror("错误", f"无法解压 .ba_ 文件:\n{e_ba}"))
                    self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                    self.root.after(0, lambda: self.progress.stop())
                    return

                # 遍历解压结果，为所有文件添加 .bak 后缀
                bak_files = []
                for root_dir, dirs, files in os.walk(extract_dir):
                    for fname in files:
                        src = os.path.join(root_dir, fname)
                        # 如果文件本身没有 .bak 后缀，添加 .bak
                        if not fname.lower().endswith(".bak"):
                            dst = src + ".bak"
                            os.rename(src, dst)
                            bak_files.append(dst)
                            self.log(f"已重命名: {fname} → {fname}.bak")
                        else:
                            bak_files.append(src)

                self.log(f"解压完成，共 {len(bak_files)} 个文件")

                # 读取每个 .bak 文件的 SQL Server 版本
                for bak_path in bak_files:
                    version_str, version_code = get_sql_version(bak_path)
                    backup_type = detect_backup_type(bak_path)
                    if version_code is not None:
                        self.log(f"  {os.path.basename(bak_path)}: {version_str} (版本号: {version_code})")
                    elif version_str:
                        self.log(f"  {os.path.basename(bak_path)}: {version_str}")
                        # 压缩备份：尝试通过 SQL Server 实例查询
                        self._query_compressed_version(bak_path)
                    else:
                        self.log(f"  {os.path.basename(bak_path)}: 未能识别 SQL Server 版本")
                    if backup_type:
                        self.log(f"  备份类型: {backup_type}")

                self.log("BA_ 文件处理完成！解压后的 bak 在同级的 extracted 文件夹中")
                self.root.after(0, lambda: messagebox.showinfo("完成", f"已解压并转换 {len(bak_files)} 个文件"))
                self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                self.root.after(0, lambda: self.progress.stop())
                return

            # ─── 处理 .bak 文件：读取 SQL Server 版本 ────────────────────
            if lower.endswith(".bak"):
                self.log("检测到 .bak 文件，正在读取 SQL Server 版本信息...")
                version_str, version_code = get_sql_version(input_path)
                backup_type = detect_backup_type(input_path)

                if version_code is not None:
                    self.log(f"SQL Server 版本: {version_str} (内部版本号: {version_code})")
                elif version_str:
                    # 压缩备份：纯字节扫描读不到版本，尝试通过 SQL Server 实例查询
                    self.log(f"SQL Server 版本: {version_str}")
                    qver = self._query_compressed_version(input_path)
                    if qver:
                        version_str = qver
                else:
                    self.log("未能识别 SQL Server 版本（可能不是有效的 SQL Server 备份文件）")

                if backup_type:
                    self.log(f"备份类型: {backup_type}")

                self.log("BAK 文件处理完成！")
                self.root.after(0, lambda: messagebox.showinfo("完成", f"SQL Server 版本: {version_str}\n备份类型: {backup_type}"))
                self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                self.root.after(0, lambda: self.progress.stop())
                return

            # ─── 处理 .xml 文件：检查编码 → 转换 → 拆分大文件 ───────────
            if lower.endswith(".xml"):
                self.log("检测到 XML 文件，正在检查编码...")
                folder = os.path.dirname(input_path) or "."
                xml_files = sorted(
                    os.path.join(folder, name)
                    for name in os.listdir(folder)
                    if name.lower().endswith(".xml")
                    and os.path.isfile(os.path.join(folder, name))
                )
                self.log(f"导入文件: {os.path.basename(input_path)}")
                self.log(f"所在文件夹: {folder}")
                self.log(f"共发现 {len(xml_files)} 个 XML 文件，开始逐个检查...")

                utf8_count = 0
                non_utf8_files = []
                norm_input = os.path.normpath(input_path)
                for idx, xml_path in enumerate(xml_files, 1):
                    is_utf8, has_bom, declared = check_xml_utf8(xml_path)
                    marker = "★" if os.path.normpath(xml_path) == norm_input else " "
                    decl_note = f"，声明编码: {declared}" if declared else ""
                    if is_utf8:
                        utf8_count += 1
                        bom_note = "（含 BOM）" if has_bom else ""
                        self.log(
                            f"  [{idx}/{len(xml_files)}] {marker} {os.path.basename(xml_path)}: UTF-8 ✓{bom_note}{decl_note}"
                        )
                    else:
                        non_utf8_files.append(xml_path)
                        self.log(
                            f"  [{idx}/{len(xml_files)}] {marker} {os.path.basename(xml_path)}: 非 UTF-8 ✗{decl_note}"
                        )

                self.log("-" * 40)

                # 确定后续处理的文件路径（UTF-8 版本）
                file_to_split = input_path
                split_base_folder = folder
                conv_success = 0
                conv_fail = 0

                # 存在非 UTF-8 文件：创建新文件夹并统一转换为 UTF-8
                if non_utf8_files:
                    self.log(
                        f"检查完成：{utf8_count}/{len(xml_files)} 个为 UTF-8，"
                        f"{len(non_utf8_files)} 个非 UTF-8，开始转换..."
                    )
                    new_folder = os.path.join(folder, "UTF8转换_" + time.strftime("%Y%m%d_%H%M%S"))
                    os.makedirs(new_folder, exist_ok=True)
                    self.log(f"已创建输出文件夹: {new_folder}")

                    for idx, xml_path in enumerate(xml_files, 1):
                        dst_path = os.path.join(new_folder, os.path.basename(xml_path))
                        ok, src_enc, err = convert_xml_to_utf8(xml_path, dst_path)
                        if ok:
                            conv_success += 1
                            self.log(
                                f"  [{idx}/{len(xml_files)}] {os.path.basename(xml_path)}: "
                                f"{src_enc} → UTF-8 ✓"
                            )
                        else:
                            conv_fail += 1
                            self.log(
                                f"  [{idx}/{len(xml_files)}] {os.path.basename(xml_path)}: "
                                f"转换失败 ✗ ({err})"
                            )

                    self.log("-" * 40)
                    self.log(
                        f"转换完成：成功 {conv_success} 个，失败 {conv_fail} 个，"
                        f"输出文件夹: {new_folder}"
                    )
                    # 转换后，导入文件的 UTF-8 版本在新文件夹中
                    file_to_split = os.path.join(new_folder, os.path.basename(input_path))
                    split_base_folder = new_folder
                else:
                    self.log(f"检查完成：全部 {len(xml_files)} 个 XML 文件均为 UTF-8 编码 ✓")

                # 检查导入文件是否超过 100MB → 拆分
                file_size = os.path.getsize(file_to_split)
                size_mb = file_size / 1024 / 1024
                split_result_msg = ""

                if file_size > 100 * 1024 * 1024:
                    self.log(f"导入文件 {size_mb:.1f}MB 超过 100MB，开始拆分...")
                    split_folder = os.path.join(
                        split_base_folder, "拆分_" + time.strftime("%Y%m%d_%H%M%S")
                    )
                    os.makedirs(split_folder, exist_ok=True)
                    split_files, split_err = split_large_xml(file_to_split, split_folder, self.log)
                    self.log("-" * 40)
                    if split_err:
                        self.log(f"拆分失败: {split_err}")
                        split_result_msg = f"\n拆分失败: {split_err}"
                    elif split_files:
                        self.log(f"拆分完成：共 {len(split_files)} 个子文件")
                        self.log(f"输出文件夹: {split_folder}")
                        split_result_msg = f"\n已拆分为 {len(split_files)} 个子文件\n输出文件夹: {split_folder}"
                    else:
                        self.log("文件格式化后未超过拆分阈值，无需拆分")
                        split_result_msg = "\n文件无需拆分"
                else:
                    self.log(f"导入文件 {size_mb:.1f}MB，未超过 100MB，无需拆分")

                # 显示最终结果
                if non_utf8_files:
                    conv_msg = (
                        f"编码转换: 成功 {conv_success} 个，失败 {conv_fail} 个\n"
                        f"输出文件夹: {split_base_folder}"
                    )
                else:
                    conv_msg = f"编码检查: 全部 {len(xml_files)} 个 XML 文件均为 UTF-8"

                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "处理完成",
                        f"{conv_msg}{split_result_msg}",
                    ),
                )
                self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                self.root.after(0, lambda: self.progress.stop())
                return

            # ─── 处理 .tar.gz / .tgz 文件 ────────────────────────────────
            if lower.endswith((".tar.gz", ".tgz")):
                self.log("检测到 .tar.gz 文件，正在解压...")
                temp_tar = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
                temp_tar.close()
                with gzip.open(input_path, "rb") as f_in:
                    with open(temp_tar.name, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)  # type: ignore[arg-type]
                tar_path = temp_tar.name
                self.log(f"解压完成: {tar_path}")
            elif lower.endswith(".tar"):
                tar_path = input_path
            else:
                self.log("不支持的文件格式")
                self.root.after(0, lambda: messagebox.showerror("错误", "不支持的文件格式"))
                self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                self.root.after(0, lambda: self.progress.stop())
                return

            # ─── 解包 tar 文件 ────────────────────────────────────────────
            with tarfile.open(tar_path, "r:*") as tar:
                members = tar.getmembers()
                total = len(members)
                self.log(f"共 {total} 个文件/目录")

                for i, member in enumerate(members, 1):
                    tar.extract(member, path=output_path)
                    self.log(f"已解包: {member.name}")
                    if i % 100 == 0:
                        self.root.after(0, lambda: None)  # keep UI responsive

            # ─── 统计 .dump 文件 ──────────────────────────────────────────
            dump_files = []
            for root_dir, dirs, files in os.walk(output_path):
                for d in files:
                    if d.lower().endswith(".dump"):
                        dump_files.append(os.path.join(root_dir, d))

            if dump_files:
                self.log(f"找到 {len(dump_files)} 个 .dump 文件:")
                for d in dump_files:
                    self.log(f"  - {d}")
                self.log(
                    f"还原完成！\n共 {total} 个文件，其中 {len(dump_files)} 个 .dump 文件"
                )
            else:
                self.log("未找到 .dump 文件（已解包所有文件）")

            # ─── 清理临时文件 ─────────────────────────────────────────────
            if lower.endswith((".tar.gz", ".tgz")) and os.path.exists(tar_path):
                os.unlink(tar_path)

            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "完成",
                    f"共 {total} 个文件，其中 {len(dump_files)} 个 .dump 文件",
                ),
            )
        except Exception as e:
            self.log(f"错误: {e}")
            self.root.after(0, lambda: messagebox.showerror("错误", str(e)))
        finally:
            self.root.after(0, lambda: self.execute_btn.config(state="normal"))
            self.root.after(0, lambda: self.progress.stop())


if __name__ == "__main__":
    root = tk.Tk()
    app = DataGetTool(root)
    root.mainloop()