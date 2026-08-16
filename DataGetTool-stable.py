"""
DataGetTool v0.0.4
用于将 .tar / .tar.gz 文件还原为 .dump 文件，
读取 .bak 文件对应的 SQL Server 版本（压缩备份给出明确提示），
处理 .ba_ 文件（gzip 或 7z 压缩）解压后添加 .bak 后缀，
导入 .xml 文件并检查/转换 UTF-8 编码。
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
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


class DataGetTool:
    def __init__(self, root):
        self.root = root
        self.root.title("DataGetTool v0.0.4")
        self.root.geometry("700x600")
        self.input_file = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.create_menu()
        self.create_widgets()

    def create_menu(self):
        menubar = Menu(self.root)
        help_menu = Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menubar)

    def show_about(self):
        messagebox.showinfo(
            "关于 DataGetTool",
            "DataGetTool v0.0.4\n\n"
            "用于将 .tar / .tar.gz 文件还原为 .dump 文件\n"
            "读取 .bak 文件对应的 SQL Server 版本\n"
            "压缩备份（MSSQLBAK 格式）会给出明确提示\n"
            "处理 .ba_ 文件（gzip 或 7z 压缩）解压后添加 .bak 后缀\n"
            "导入 .xml 文件：检查所在文件夹所有 XML 是否 UTF-8 编码，\n"
            "非 UTF-8 文件自动转换为 UTF-8 并保存至同级新文件夹",
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
                    self.log(f"SQL Server 版本: {version_str}")
                else:
                    self.log("未能识别 SQL Server 版本（可能不是有效的 SQL Server 备份文件）")

                if backup_type:
                    self.log(f"备份类型: {backup_type}")

                self.log("BAK 文件处理完成！")
                self.root.after(0, lambda: messagebox.showinfo("完成", f"SQL Server 版本: {version_str}\n备份类型: {backup_type}"))
                self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                self.root.after(0, lambda: self.progress.stop())
                return

            # ─── 处理 .xml 文件：检查编码，非 UTF-8 则统一转换 ───────────
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

                # 全部为 UTF-8：无需转换
                if not non_utf8_files:
                    self.log(f"检查完成：全部 {len(xml_files)} 个 XML 文件均为 UTF-8 编码 ✓")
                    self.root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "编码检查完成",
                            f"共 {len(xml_files)} 个 XML 文件，全部为 UTF-8 编码。",
                        ),
                    )
                    self.root.after(0, lambda: self.execute_btn.config(state="normal"))
                    self.root.after(0, lambda: self.progress.stop())
                    return

                # 存在非 UTF-8 文件：创建新文件夹并统一转换为 UTF-8
                self.log(
                    f"检查完成：{utf8_count}/{len(xml_files)} 个为 UTF-8，"
                    f"{len(non_utf8_files)} 个非 UTF-8，开始转换..."
                )
                new_folder = os.path.join(folder, "UTF8转换_" + time.strftime("%Y%m%d_%H%M%S"))
                os.makedirs(new_folder, exist_ok=True)
                self.log(f"已创建输出文件夹: {new_folder}")

                success_count = 0
                fail_count = 0
                for idx, xml_path in enumerate(xml_files, 1):
                    dst_path = os.path.join(new_folder, os.path.basename(xml_path))
                    ok, src_enc, err = convert_xml_to_utf8(xml_path, dst_path)
                    if ok:
                        success_count += 1
                        self.log(
                            f"  [{idx}/{len(xml_files)}] {os.path.basename(xml_path)}: "
                            f"{src_enc} → UTF-8 ✓"
                        )
                    else:
                        fail_count += 1
                        self.log(
                            f"  [{idx}/{len(xml_files)}] {os.path.basename(xml_path)}: "
                            f"转换失败 ✗ ({err})"
                        )

                self.log("-" * 40)
                self.log(
                    f"转换完成：成功 {success_count} 个，失败 {fail_count} 个，"
                    f"输出文件夹: {new_folder}"
                )
                if fail_count:
                    self.root.after(
                        0,
                        lambda: messagebox.showwarning(
                            "转换完成（部分失败）",
                            f"成功: {success_count} 个\n失败: {fail_count} 个\n\n"
                            f"输出文件夹:\n{new_folder}",
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "转换完成",
                            f"全部 {success_count} 个 XML 文件已转换为 UTF-8。\n\n"
                            f"输出文件夹:\n{new_folder}",
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
