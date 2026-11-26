"""Native DataGetTool operations used by the toolbox UI.

Ported from DataGetTool v0.0.4beta so the desktop shell can run the same
operations without launching its tkinter window.
"""
from __future__ import annotations

import codecs
import gzip
import os
import re
import shutil
import struct
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

EXIT_CODES = {"INVALID_INPUT": 2, "INTERNAL": 5, "DSH_UNAVAILABLE": 7}


class ToolError(Exception):
    def __init__(self, code: str, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code

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
OFFSET_AFTER_MSCI = 168


def _require_file(value: str, label: str = "文件") -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ToolError("INVALID_INPUT", f"{label}不存在：{path}", exit_code=EXIT_CODES["INVALID_INPUT"])
    return path


def get_sql_version(path: Path) -> tuple[str | None, int | None]:
    try:
        with path.open("rb") as f:
            if f.read(4) != TAPE_MAGIC:
                return None, None
            buffer = b""
            while True:
                chunk = f.read(65536)
                if not chunk:
                    return None, None
                buffer = (buffer + chunk)[-65540:]
                idx = buffer.find(MSCI_MAGIC)
                if idx != -1:
                    position = f.tell() - len(chunk) + idx + len(MSCI_MAGIC)
                    f.seek(position + OFFSET_AFTER_MSCI)
                    break
            raw = f.read(2)
            if len(raw) < 2:
                return None, None
            version = struct.unpack("<h", raw)[0]
            return VERSION_MAP.get(version, f"Unknown (code: {version})"), version
    except Exception:  # noqa: BLE001
        return None, None


def detect_backup_type(path: Path) -> str:
    try:
        with path.open("rb") as f:
            first = f.read(8)
            if first.startswith(b"MSSQLBAK"):
                return "压缩备份 (Compressed)"
            f.seek(3750)
            ch = f.read(1)
            return {
                b"D": "完整备份 (Full)",
                b"L": "事务日志备份 (Log)",
                b"I": "差异备份 (Differential)",
            }.get(ch, "未知类型")
    except Exception:  # noqa: BLE001
        return "未知类型"


def inspect_bak(value: str) -> dict[str, Any]:
    path = _require_file(value, "BAK 文件")
    version, code = get_sql_version(path)
    return {
        "ok": True,
        "action": "inspect",
        "path": str(path),
        "version": version,
        "version_code": code,
        "backup_type": detect_backup_type(path),
        "logs": [
            f"文件：{path.name}",
            f"SQL Server 版本：{version or '未识别'}（内部版本号：{code or '—'}）",
            f"备份类型：{detect_backup_type(path)}",
        ],
    }


def _safe_extract_tar(tar: tarfile.TarFile, output: Path) -> list[str]:
    logs: list[str] = []
    members = tar.getmembers()
    for member in members:
        try:
            tar.extract(member, path=output, filter="data")
            logs.append(f"已解包：{member.name}")
        except (tarfile.FilterError, OSError) as exc:
            raise ToolError("INVALID_INPUT", f"解包失败 {member.name}：{exc}", exit_code=EXIT_CODES["INVALID_INPUT"]) from exc
    return logs


def restore_tar(value: str, output_dir: str) -> dict[str, Any]:
    source = _require_file(value, "TAR 文件")
    output = Path(output_dir).expanduser().resolve()
    if not output.is_dir():
        raise ToolError("INVALID_INPUT", f"输出目录不存在：{output}", exit_code=EXIT_CODES["INVALID_INPUT"])
    lower = source.name.lower()
    temp_tar: Path | None = None
    logs: list[str] = []
    try:
        if lower.endswith((".tar.gz", ".tgz")):
            handle = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
            temp_tar = Path(handle.name)
            handle.close()
            with gzip.open(source, "rb") as src, temp_tar.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            tar_path = temp_tar
            logs.append("已解压 gzip 外层")
        elif lower.endswith(".tar"):
            tar_path = source
        else:
            raise ToolError("INVALID_INPUT", "只支持 .tar / .tar.gz / .tgz", exit_code=EXIT_CODES["INVALID_INPUT"])
        with tarfile.open(tar_path, "r:*") as tar:
            logs.extend(_safe_extract_tar(tar, output))
        dumps = sorted(str(path) for path in output.rglob("*") if path.is_file() and path.suffix.lower() == ".dump")
        logs.append(f"共解包 {len(logs) - 1} 个条目，找到 {len(dumps)} 个 .dump 文件")
        return {"ok": True, "action": "tar", "output_dir": str(output), "dump_files": dumps, "logs": logs}
    finally:
        if temp_tar and temp_tar.exists():
            temp_tar.unlink(missing_ok=True)


def extract_ba(value: str, output_dir: str = "") -> dict[str, Any]:
    source = _require_file(value, "BA_ 文件")
    output = Path(output_dir).expanduser().resolve() if output_dir else source.parent
    if not output.is_dir():
        raise ToolError("INVALID_INPUT", f"输出目录不存在：{output}", exit_code=EXIT_CODES["INVALID_INPUT"])
    base = source.name[:-4] if source.name.lower().endswith(".ba_") else source.stem
    extract_dir = output / f"{base}_extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    magic = source.read_bytes()[:6]
    logs = [f"文件：{source.name}", f"输出：{extract_dir}"]
    if magic[:2] == b"\x1f\x8b":
        target = extract_dir / base
        with gzip.open(source, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        logs.append("检测到 gzip，已解压")
    elif magic == b"\x37\x7a\xbc\xaf\x27\x1c":
        try:
            import py7zr
        except ImportError as exc:
            raise ToolError("INVALID_INPUT", "7z 需要 py7zr：pip install py7zr", exit_code=EXIT_CODES["INVALID_INPUT"]) from exc
        temp = Path(tempfile.gettempdir()) / f"{base}_{os.getpid()}.7z"
        shutil.copy2(source, temp)
        try:
            with py7zr.SevenZipFile(temp, mode="r") as archive:
                archive.extractall(path=extract_dir)
            logs.append("检测到 7z，已解压")
        finally:
            temp.unlink(missing_ok=True)
    else:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise ToolError("INVALID_INPUT", f"不支持的 .ba_ 文件头：{magic.hex()}", exit_code=EXIT_CODES["INVALID_INPUT"])
    baks: list[str] = []
    for path in extract_dir.rglob("*"):
        if not path.is_file():
            continue
        target = path if path.suffix.lower() == ".bak" else path.with_name(path.name + ".bak")
        if target != path:
            path.rename(target)
            logs.append(f"重命名：{path.name} → {target.name}")
        baks.append(str(target))
    for bak in baks:
        version, code = get_sql_version(Path(bak))
        logs.append(f"  {Path(bak).name}：{version or '未识别 SQL Server 版本'}（{code or '—'}），{detect_backup_type(Path(bak))}")
    return {"ok": True, "action": "ba", "output_dir": str(extract_dir), "bak_files": baks, "logs": logs}


def check_xml_utf8(path: Path) -> tuple[bool, bool, str | None]:
    has_bom = False
    declared = None
    try:
        head = path.read_bytes()[:512]
        has_bom = head.startswith(b"\xef\xbb\xbf")
        match = re.search(rb'encoding\s*=\s*["\']([^"\']+)["\']', head, re.IGNORECASE)
        if match:
            declared = match.group(1).decode("ascii", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        with path.open("rb") as f:
            decoder = codecs.getincrementaldecoder("utf-8")()
            while True:
                chunk = f.read(65536)
                if not chunk:
                    decoder.decode(b"", final=True)
                    break
                decoder.decode(chunk, final=False)
        return True, has_bom, declared
    except UnicodeDecodeError:
        return False, has_bom, declared


def convert_xml_to_utf8(source: Path, target: Path) -> tuple[bool, str | None, str | None]:
    try:
        raw = source.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return False, None, f"读取失败：{exc}"
    declared = None
    match = re.search(rb'encoding\s*=\s*["\']([^"\']+)["\']', raw[:512], re.IGNORECASE)
    if match:
        declared = match.group(1).decode("ascii", errors="replace")
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
    text = None
    source_encoding = None
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
        return False, None, "无法识别编码（已尝试：" + ", ".join(candidates) + "）"
    new_text = re.sub(
        r'(<\?xml[^>]*?encoding\s*=\s*["\'])[^"\']+(["\'])',
        r"\1utf-8\2",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    try:
        target.write_bytes(new_text.encode("utf-8"))
        return True, source_encoding, None
    except Exception as exc:  # noqa: BLE001
        return False, source_encoding, f"写入失败：{exc}"


def split_large_xml(path: Path, output_dir: Path, logs: list[str] | None = None) -> tuple[list[str], str | None]:
    budget = logs if logs is not None else []
    try:
        content = path.read_text(encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        return [], f"读取文件失败：{exc}"
    decl_match = re.match(r"<\?xml[^>]*\?>", content)
    if decl_match:
        declaration = re.sub(r'encoding="[^"]+"', 'encoding="utf-8"', decl_match.group(0), count=1)
        decl_end = decl_match.end()
    else:
        declaration = '<?xml version="1.0" encoding="utf-8"?>'
        decl_end = 0
    root_start = decl_end
    while root_start < len(content) and content[root_start] in " \t\r\n":
        root_start += 1
    if root_start >= len(content) or content[root_start] != "<":
        return [], "无法找到根元素"
    i = root_start + 1
    in_quote = None
    while i < len(content):
        char = content[i]
        if in_quote:
            if char == in_quote:
                in_quote = None
        elif char in "\"'":
            in_quote = char
        elif char == ">":
            break
        i += 1
    root_open_end = i + 1
    root_open_tag = content[root_start:root_open_end]
    name_match = re.match(r"<([^\s/>]+)", root_open_tag)
    if not name_match:
        return [], "无法解析根标签名"
    root_name = name_match.group(1)
    root_close_tag = f"</{root_name}>"
    root_close_pos = content.rfind(root_close_tag)
    if root_close_pos == -1:
        return [], "无法找到根闭标签"
    inner_start, inner_end = root_open_end, root_close_pos
    elements: list[tuple[int, int, int]] = []
    pos, depth = inner_start, 0
    while pos < inner_end:
        lt = content.find("<", pos)
        if lt == -1 or lt >= inner_end:
            break
        if content[lt : lt + 4] == "<!--":
            ce = content.find("-->", lt + 4)
            pos = ce + 3 if ce != -1 else len(content)
            continue
        if content[lt : lt + 9] == "<![CDATA[":
            ce = content.find("]]>", lt + 9)
            pos = ce + 3 if ce != -1 else len(content)
            continue
        j = lt + 1
        quote = None
        while j < inner_end:
            char = content[j]
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == ">":
                break
            j += 1
        tag_end = j + 1
        tag_text = content[lt:tag_end]
        if tag_text.startswith("</"):
            depth -= 1
            pos = tag_end
        elif tag_text.rstrip().endswith("/>"):
            if depth == 0:
                elements.append((lt, tag_end, len(re.findall(r">\s*<", content[lt:tag_end])) + 1))
            pos = tag_end
        else:
            if depth == 0:
                element_start = lt
                element_name = re.match(r"<([^\s/>]+)", tag_text).group(1)
                search_pos, d = tag_end, 1
                while d > 0 and search_pos < inner_end:
                    next_lt = content.find("<", search_pos)
                    if next_lt == -1 or next_lt >= inner_end:
                        break
                    if content[next_lt : next_lt + 4] == "<!--":
                        ce = content.find("-->", next_lt + 4)
                        search_pos = ce + 3 if ce != -1 else len(content)
                        continue
                    if content[next_lt : next_lt + 9] == "<![CDATA[":
                        ce = content.find("]]>", next_lt + 9)
                        search_pos = ce + 3 if ce != -1 else len(content)
                        continue
                    k = next_lt + 1
                    inner_quote = None
                    while k < inner_end:
                        char = content[k]
                        if inner_quote:
                            if char == inner_quote:
                                inner_quote = None
                        elif char in "\"'":
                            inner_quote = char
                        elif char == ">":
                            break
                        k += 1
                    nt_end = k + 1
                    nt_text = content[next_lt:nt_end]
                    if nt_text.startswith("</"):
                        close_name = re.match(r"</([^\s/>]+)", nt_text)
                        if close_name and close_name.group(1) == element_name:
                            d -= 1
                            if d == 0:
                                elements.append(
                                    (element_start, nt_end, len(re.findall(r">\s*<", content[element_start:nt_end])) + 1)
                                )
                                break
                    elif not nt_text.rstrip().endswith("/>"):
                        open_name = re.match(r"<([^\s/>]+)", nt_text)
                        if open_name and open_name.group(1) == element_name:
                            d += 1
                    search_pos = nt_end
                pos = search_pos if d == 0 else inner_end
            else:
                depth += 1
                pos = tag_end
    if not elements:
        return [], "未找到一级子元素"
    target_max, target_min = 2_500_000, 1_000_000
    total_lines = sum(item[2] for item in elements)
    budget.append(f"共 {len(elements)} 个一级子元素，预计约 {total_lines} 行")
    chunks: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_lines = 0
    for start, end, lines in elements:
        if current and current_lines + lines > target_max and current_lines >= target_min:
            chunks.append(current)
            current = []
            current_lines = 0
        current.append((start, end))
        current_lines += lines
    if current:
        chunks.append(current)
    if len(chunks) <= 1:
        budget.append(f"约 {total_lines} 行，未超过拆分阈值")
        return [], None
    base = path.stem
    split_files: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        target = output_dir / f"{base}_part{index}.xml"
        try:
            with target.open("w", encoding="utf-8") as f:
                f.write(declaration + "\n")
                f.write(root_open_tag + "\n")
                for start, end in chunk:
                    f.write(re.sub(r">\s*<", ">\n<", content[start:end]) + "\n")
                f.write(root_close_tag + "\n")
            split_files.append(str(target))
            budget.append(f"已生成：{target.name}")
        except Exception as exc:  # noqa: BLE001
            return split_files, f"写入子文件失败：{exc}"
    return split_files, None


def process_xml(value: str) -> dict[str, Any]:
    source = _require_file(value, "XML 文件")
    folder = source.parent
    xml_files = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".xml")
    logs = [f"文件：{source.name}", f"目录：{folder}", f"发现 {len(xml_files)} 个 XML"]
    non_utf8: list[Path] = []
    for path in xml_files:
        is_utf8, has_bom, declared = check_xml_utf8(path)
        logs.append(f"{path.name}：{'UTF-8' if is_utf8 else '非 UTF-8'}{'（BOM）' if has_bom else ''}{f'，声明 {declared}' if declared else ''}")
        if not is_utf8:
            non_utf8.append(path)
    converted_folder = None
    split_folder = None
    split_files: list[str] = []
    file_to_split = source
    if non_utf8:
        converted_folder = folder / ("UTF8转换_" + time.strftime("%Y%m%d_%H%M%S"))
        converted_folder.mkdir(parents=True, exist_ok=True)
        for path in xml_files:
            target = converted_folder / path.name
            ok, enc, exc = convert_xml_to_utf8(path, target)
            logs.append(f"转换 {path.name}：{'成功' if ok else '失败'}{f'（{enc}）' if enc else ''}{f'：{exc}' if exc else ''}")
        file_to_split = converted_folder / source.name
    size_mb = file_to_split.stat().st_size / 1024 / 1024
    if file_to_split.stat().st_size > 100 * 1024 * 1024:
        split_folder = (converted_folder or folder) / ("拆分_" + time.strftime("%Y%m%d_%H%M%S"))
        split_folder.mkdir(parents=True, exist_ok=True)
        split_files, exc = split_large_xml(file_to_split, split_folder, logs)
        if exc:
            logs.append(f"拆分失败：{exc}")
    else:
        logs.append(f"{size_mb:.1f}MB，未超过 100MB，无需拆分")
    return {
        "ok": True,
        "action": "xml",
        "folder": str(folder),
        "converted_folder": str(converted_folder) if converted_folder else None,
        "split_folder": str(split_folder) if split_folder else None,
        "split_files": split_files,
        "non_utf8": [path.name for path in non_utf8],
        "logs": logs,
    }
