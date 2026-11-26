# DataGetTool

一个 Windows 桌面小工具（Python + Tkinter 图形界面），用于处理数据备份与导出文件的常用前置工作。

## 功能

- **TAR 解包还原**：将 `.tar` / `.tar.gz` / `.tgz` 解包，还原出 `.dump` 文件
- **SQL Server 备份版本识别**：扫描 `.bak` 文件的 MTF 格式头部，读取对应的 SQL Server 内部版本号（覆盖 SQL Server 6.0 – 2025）
- **压缩备份版本查询**（beta 版）：对 `MSSQLBAK` 格式的压缩备份，自动通过 SQL Server 实例执行 `RESTORE HEADERONLY` 查询版本
- **.ba_ 文件处理**：自动识别 gzip / 7z 压缩格式，解压后添加 `.bak` 后缀并读取版本
- **XML 编码检查与转换**：检查所选文件所在文件夹内所有 XML 是否为 UTF-8 编码，非 UTF-8 文件自动批量转换并保存至新文件夹
- **大 XML 拆分**（beta 版）：超过 100MB 的 XML 自动拆分为多个子文件（每个 100 万 – 250 万行，保持一级子元素完整）

## 版本说明

| 版本 | 源码 | 说明 |
| --- | --- | --- |
| 稳定版 | `DataGetTool-stable.py` | 基础功能 + 压缩备份识别提示 |
| beta 版 | `DataGetTool-beta.py` | 稳定版全部功能 + 压缩备份版本查询 + XML 大文件拆分 |

`DataGetTool.py` 为开发主文件，功能与 beta 版同步。

## 下载

见 [Releases](../../releases) 页面。单文件 exe（PyInstaller 打包），无需安装 Python 环境。

## beta 版：压缩备份版本查询

压缩备份（`MSSQLBAK` 格式）的版本号位于专有压缩块内，无法纯字节扫描读取，需要借助 SQL Server 实例：

1. 菜单 **[设置 → SQL Server 连接设置]** 中配置服务器/实例（如 `.\MSSQLSERVER19`）与认证方式（Windows 集成 / SQL Server 登录）；
2. 密码**不保存**，每次查询时弹窗输入；
3. 机器上需有 SQL Server ODBC 驱动（优先使用 ODBC Driver 17）；
4. 注意：`RESTORE HEADERONLY` 中的文件路径在 **SQL Server 服务器端**解析，备份文件需对 SQL Server 服务账户可读（远程服务器需为其可访问的共享路径）。

连接配置保存在 exe 同目录的 `DataGetTool-sqlserver.json`（不含密码）。

## 从源码构建

```bash
pip install pyodbc py7zr
python -m PyInstaller DataGetTool-beta.spec    # beta 版
python -m PyInstaller DataGetTool-stable.spec  # 稳定版
```

- `pyodbc`：beta 版压缩备份版本查询所需（打包必须，运行时缺 ODBC 驱动会提示）
- `py7zr`：可选，仅处理 7z 格式的 `.ba_` 文件时需要

## 许可证

[MIT](LICENSE)

## 开发说明

本项目采用 **Vibe Coding** 方式开发，全程由 [Kimi Code](https://www.kimi.com/)（Moonshot AI 推出的 AI 编程助手）协作完成：功能实现、调试、打包与发布均由自然语言驱动、AI 辅助编码。

## CLI sidecar（工具箱接入）

独立核心：`datagettool_core.py`；CLI：`datagettool_cli.py`。

```powershell
python datagettool_cli.py inspect --path backup.bak
python datagettool_cli.py tar --path backup.tar --out .\out
python datagettool_cli.py ba --path backup.ba_ --out .\out
python datagettool_cli.py xml --path data.xml
```n
工具箱通过 CLI 调用，不再复制核心逻辑。
