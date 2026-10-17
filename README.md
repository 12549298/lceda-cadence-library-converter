# LCEDA Cadence Library Converter

将嘉立创 EDA 专业版导出的 Altium ASCII 工程转换为 OrCAD 原理图库和 Allegro 封装库。提供 Windows 图形界面和命令行入口，优先调用软件原生接口，不依赖鼠标宏。

## 环境与快速开始

| 依赖 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 |
| Python | 3.10+，包含 Tkinter；无需 pip 安装第三方包 |
| Cadence | 已安装且许可可用；原生转换已验证 24.1 S008 |
| Altium Designer | 仅 PCB 缺少必要字段时需要；中转已验证 25.7.1 |

1. 下载本仓库 ZIP 并解压，或使用 Git 克隆。
2. 在嘉立创专业版导出 Altium Designer 工程 ZIP。
3. 双击 `start.cmd`，选择输入、输出目录和外层文件夹名称。
4. 确认导出位置，在 `symbol` 和 `footprint` 文件夹中取得库文件。

本项目不包含 Cadence、Altium 或其运行库，不能脱离已安装的 EDA 软件独立转换。其他 EDA 版本尚需验证，可通过命令行指定安装路径和产品许可选项。

## 界面操作

双击 **start.cmd**，选择嘉立创导出的 Altium Designer ZIP 或解压目录，点击“开始”。也可以把 ZIP 拖到 start.cmd 上。

界面的“输出目录”可以手动填写或点击“选择”更换。在“文件夹名称”中输入外层文件夹名称，例如“我的器件库”。程序自动记住上次实际使用的输入路径、输出根目录和名称，下次打开时自动填入；文件选择窗口也会从上次的位置打开。

主窗口启动时在屏幕中央显示，确认弹窗在主窗口中央显示。每次点击“开始”都会先显示**本次完整导出路径**，包括你命名的外层文件夹。可以选择“开始导出”“更换目录…”或“取消”。允许使用已有同名文件夹：确认窗口会提示覆盖范围，成功转换的同名库文件会被更新，其他文件保留。转换失败的任务不会提前删除旧库文件。转换完成后会再次提示实际导出目录。拖拽 ZIP 到 start.cmd 也会经过这个界面。

路径记忆保存在 `%LOCALAPPDATA%\LCEDA_Cadence\settings.json`。取消导出不会覆盖上一次的路径记忆。命令行直接运行 `convert.py` 时，`--output` 优先；省略时沿用上次输出根目录，并在日志中显示本次目录，不弹出交互窗口，方便批处理。需要命令行启动确认界面时加 `--gui`。

脚本使用软件原生接口，不使用鼠标坐标、键盘录制或桌面点击。AD 工作进程仍可能短暂显示软件窗口。

## 是否需要 AD

嘉立创专业版导出的 Altium 工程本来就是 ASCII 5.x。原理图文件头为 `Ascii File Version 5.0`；PCB 常见版本字段为 `VERSION=5.01`。[嘉立创官方说明](https://prodocs.lceda.cn/cn/import-export/export-altium-designer/)

**但 ASCII 格式正确，不代表字段足够完整。** 验证样例的嘉立创原始 PCB 缺少 `INDEXFORSAVE` 及文字的 `X1/Y1/X2/Y2`，Cadence 原生转换器会报错。因此默认策略是：

- 原理图直接调用 Cadence Tcl 转换器和数据库 DLL，不启动或接管 Capture 窗口。
- PCB 字段完整时跳过 AD，以 Allegro SKILL 原生函数直接导出封装。
- PCB 缺少文字包围框时，自动启动 AD，仅对临时副本用原生 `DoFileSave('PCB ASCII File')` 重新保存，再继续转换。脚本检查元件、焊盘数量及输入已有的坐标、尺寸、引脚号等是否保持一致。
- 默认不估算文字。如果 AD 不可用，或 AD 已在运行，会明确停止该项并保留日志，不会修改你的原始文件或关闭已有 AD 会话。

AD 中转采用官方的脚本命令入口与文件保存接口：[命令行运行脚本](https://resources.altium.com/p/continuous-integration-implementation-using-altium-designer)、[IServerDocument](https://www.altium.com/documentation/altium-dxp-developer/iserverdocument-interface)。AD 的 ASCII 保存种类以本机 `System/advpcb.ins` 中的 `PCB ASCII File` 为准。

AD 不是独立的无界面转换器，启动时可能短暂显示窗口；这里不发送鼠标和键盘输入。它作为独立工作进程运行，完成临时副本保存及校验后退出。中转期间不要在这个工作实例里打开其他工程；如果已有 AD 在编辑文件，先保存并关闭它，再开始批处理。

## 输入与输出

在嘉立创中选择“文件 → 导出 → Altium Designer”，导出包含所需器件的工程。脚本从该 ZIP 开始工作，嘉立创导出本身仍由你操作。

支持 ZIP、解压目录、单个 `.SchDoc` 或 `.PcbDoc`。自动忽略 History 文件夹。不能把 `.SchLib/.PcbLib`、Gerber 或 `.epro` 改名后交给本脚本。二进制 Altium 文件会被识别并拒绝，应先另存为 ASCII。

每次运行在 `所选输出目录/自定义名称/` 创建结果；首次使用默认根目录是脚本旁的 `output`。外层目录下只有并列的 `symbol` 和 `footprint` 两个文件夹：

```text
我的器件库/
├── symbol/
│   └── 原理图名称.olb
└── footprint/
    ├── 封装名称.dra
    ├── 封装名称.psm
    ├── 焊盘名称.pad
    └── 器件描述.txt
```

| 位置 | 内容 |
| --- | --- |
| `symbol/*.olb` | OrCAD 原理图库，以来源原理图命名 |
| `footprint/*.dra/*.psm/*.pad` | Allegro 封装及焊盘库 |
| `footprint/*.ssm/*.bsm/*.fsm/*.txt` | 形状等依赖库、元件设备描述（有则导出） |
| `footprint/*.brd` | 仅指定 `--board` 时导出完整板文件 |

报告 `report.json`、`结果说明.txt`、中间 DSN、原生脚本和日志都保留在本次工作目录，不混入交付库文件夹。运行日志和完成提示会给出报告路径。只有成功转换的库会被收集到上述两个文件夹；失败信息写入报告。

重复导出到同名文件夹时，本次生成的同名库文件会覆盖上次的文件。**同一次批量转换内部**，多个原理图生成同名但内容不同的 OLB 时，自动为库文件名增加区分标识；多个板子的封装或焊盘重名且内容一致时共享文件，内容不同则报错，请将相关板子分别导出，避免依赖冲突。

默认任务是**封装和原理图库提取**，不会迁移完整 PCB 的布线、板级约束或多页工程层级关系。原理图按图页分别转换，适合提取库。如果要迁移整板，另加 `--board`；复杂板及层级工程需要单独核查。将 Allegro 的 `psmpath`、`padpath` 指向相应 `footprint` 目录即可使用封装；脚本不会修改你的全局库路径。

## 命令行

```powershell
# 自动判断 AD 是否必要，默认不估算
py -3 convert.py "D:\导出\工程.zip" --output "D:\转换结果" --name "我的器件库"

# 只提取 PCB 封装
py -3 convert.py "D:\导出\工程.zip" --name "PCB封装库" --only pcb

# 仅检查文件头和文字字段，不启动 EDA 软件
py -3 convert.py "D:\导出\工程.zip" --name "格式检查" --inspect

# 不允许 AD 中转；遇到缺失字段即失败，不悄悄估算
py -3 convert.py "D:\导出\工程.zip" --name "直接转换" --no-ad

# 指定安装位置
py -3 convert.py "D:\导出\工程.zip" --name "我的器件库" --cadence "E:\Cadence\SPB_24.1" --altium "E:\Altium\AD25\X2.EXE"

# 只生成原生脚本供审查
py -3 convert.py "D:\导出\工程.zip" --name "脚本准备" --prepare-only
```

Cadence 产品许可选项默认是验证过的 `Allegro_X_Designer_Layout_plus`。如你的许可不同，用 `--product` 指定可用产品。`--timeout 600` 控制每个阶段的超时时间。

为了兼容 EDA 原生工具，工作副本放在英文短路径 `C:\tmp\LCEDA_Cadence`。用 `--work-root D:\eda_work` 可以更换；该路径不能有空格、中文或特殊字符。输入路径及最终输出路径可包含中文和空格。工作副本保留以便排错，确认输出后可自行清理。

## 关于文字估算

只有显式传入 `--allow-text-estimation` 才允许估算缺失的文字包围框，便于在没有 AD 时尝试提取库。它不修改源数据中的文字锚点、旋转角、焊盘尺寸或引脚号，但 Cadence 可能利用包围框计算文字摆放，因此**文字仍可能偏移或重叠**。报告会记录估算数量。不能把这种结果视为精确转换；默认严格模式不会走这条路径。

任何格式迁移完成后，都应在目标 EDA 中核对引脚映射、焊盘、阻焊/钢网、尺寸和文字。脚本的“成功”表示转换步骤完成、输出文件存在且通过指定结构检查，不代表已完成所有电气及制造检查。

## 验证与限制

已完成实际样例验证：原始嘉立创 ZIP → 自动 AD 另存 PCB → Allegro 原生提取库，以及原始 SchDoc → 独立 Tcl 转换 → 数据库读取验证。生成 1 个原理图库元件、1 个 TSSOP-8 封装、1 个焊盘库；严格流程的文字估算数量为 0。实际设计样例及转换结果不随仓库分发。

另对直接兼容路径的样例读取了 Allegro 实际封装数据库，其 8 个引脚编号及局部坐标与 AD 另存后转换的基准一致。这仅是该样例的验证，不能替代对其他封装的检查。

Cadence 转换器的内部 Tcl/SKILL 接口可能随版本变化；当前验证版本为 24.1。脚本不修改或分发厂商程序，仅调用本机安装的运行库和函数。不同版本、许可缺失或不支持的图元会产生失败日志，不会制造一个空文件当作成功。

运行输入保护和兼容处理测试：

```powershell
py -3 -m unittest discover -s tests -v
```

GitHub Actions 在 Windows 上运行 Python 3.10 和 3.13 的单元测试。CI 使用合成数据和模拟接口，不安装或启动 EDA 软件；CI 通过不代表所有厂商版本的原生转换都已验证。

## 许可

本项目源码采用 [MIT License](LICENSE)。该许可不涵盖第三方 EDA 软件、商标、设计文件或厂商运行库。本项目不是嘉立创、Cadence 或 Altium 的官方产品。
