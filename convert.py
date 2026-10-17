"""嘉立创 Altium ASCII -> OrCAD / Allegro 库。Python 3.10+，仅标准库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import ad_bridge

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "LCEDA_Cadence" / "settings.json"
DESIGN_EXTS = {".pcbdoc", ".schdoc"}
IGNORE_DIRS = {"history", "__macosx", ".git", "translator.run"}


class ConversionError(RuntimeError):
    pass


def load_paths() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("input", "output", "name")
            if isinstance(data.get(k), str) and data[k].strip()}


def save_paths(source: Path, output: Path, name: str = "") -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_name(f"settings.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"input": str(source), "output": str(output), "name": name},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(SETTINGS_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def new_run_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def package_name(value: str | None) -> str:
    name = (value or "").strip()
    reserved = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}
    if (not name or name in {".", ".."} or len(name) > 120 or name.endswith(".")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.split(".")[0].casefold() in reserved):
        raise ConversionError("请输入有效的外层文件夹名称（命令行使用 --name），不能包含路径分隔符或特殊字符。")
    return name


def result_directory(report_path: str | Path) -> Path:
    path = Path(report_path)
    return Path(json.loads(path.read_text(encoding="utf-8"))["output_directory"])


def package_job(job: Path, kind: str, source: Path, dest: Path, key: str, board=False,
                published: dict | None = None) -> list[str]:
    """Replace previous exports, but reject incompatible dependencies in the same run."""
    published = published if published is not None else {}
    candidates = []
    if kind == "schdoc":
        libraries = sorted(p for p in (job / "schematic").iterdir() if p.suffix.lower() == ".olb")
        for library in libraries:
            stem = source.stem if len(libraries) == 1 else f"{source.stem}_{library.stem}"
            target = dest / "symbol" / (stem + ".olb")
            claimed = published.get(target.relative_to(dest).as_posix().casefold())
            if claimed and claimed.read_bytes() != library.read_bytes():
                target = target.with_name(f"{stem}_{key}.olb")
            candidates.append((library, target))
    else:
        allowed = {".dra", ".psm", ".pad", ".ssm", ".bsm", ".fsm", ".txt"}
        candidates = [(p, dest / "footprint" / p.name) for p in sorted((job / "library").rglob("*"))
                      if p.is_file() and p.suffix.lower() in allowed]
        if board:
            candidates.append((job / "result.brd", dest / "footprint" / f"{source.stem}_{key}.brd"))
    if not candidates:
        raise ConversionError("转换结果中没有可打包的库文件。")
    existing = {p.relative_to(dest).as_posix().casefold(): p
                for p in (dest / ("symbol" if kind == "schdoc" else "footprint")).iterdir()}
    available = dict(published)
    planned = []
    files = []
    for source_file, target in candidates:
        identity = target.relative_to(dest).as_posix().casefold()
        target = existing.get(identity, target)
        files.append(str(target.relative_to(dest)))
        previous = available.get(identity)
        if previous:
            if previous.read_bytes() != source_file.read_bytes():
                raise ConversionError(f"本次转换中不同板子的库文件重名且内容不同：{target.name}。请将相关板子分别导出，避免封装依赖冲突。")
            continue
        if target.exists() and not target.is_file():
            raise ConversionError(f"库文件输出位置被文件夹占用：{target}")
        available[identity] = source_file
        planned.append((source_file, target))
    staged = []
    try:
        # Finish copying this job before replacing any previous files.
        for source_file, target in planned:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            staged.append((temporary, target))
            with source_file.open("rb") as src, temporary.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        for temporary, target in staged:
            temporary.replace(target)
        published.update(available)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return sorted(set(files))


def dialog_directory(value: str) -> str:
    """Open pickers near the remembered path, even if that file/folder was removed."""
    try:
        path = Path(value).expanduser().resolve() if value.strip() else HERE
        while not path.is_dir() and path != path.parent:
            path = path.parent
        if path.is_dir():
            return str(path)
    except (OSError, ValueError):
        pass
    return str(HERE)


def confirm_export(parent, output, name: str) -> bool:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    dialog = tk.Toplevel(parent)
    dialog.withdraw()
    dialog.title("确认本次导出位置")
    dialog.transient(parent)
    dialog.resizable(True, False)
    body = ttk.Frame(dialog, padding=18)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="本次文件将保存到：").pack(anchor="w")
    destination = tk.StringVar()
    overwrite_note = tk.StringVar()
    accepted = [False]
    def refresh():
        target = Path(output.get()).expanduser().resolve() / name
        destination.set(str(target))
        overwrite_note.set("文件夹已存在：将覆盖 symbol 和 footprint 中本次导出的同名文件，其他文件保留。"
                           if target.is_dir() else "此文件夹内并列保存 symbol 和 footprint。")
    refresh()
    ttk.Entry(body, textvariable=destination, state="readonly", width=85).pack(fill="x", pady=10)
    ttk.Label(body, textvariable=overwrite_note, wraplength=680).pack(anchor="w")
    buttons = ttk.Frame(body)
    buttons.pack(fill="x", pady=(16, 0))
    def change():
        chosen = filedialog.askdirectory(parent=dialog, title="更换导出根目录",
                                          initialdir=dialog_directory(output.get()), mustexist=False)
        if chosen:
            output.set(chosen)
            refresh()
    def accept():
        folder = Path(output.get()).expanduser().resolve()
        if folder.exists() and not folder.is_dir():
            messagebox.showerror("目录不可用", "请选择文件夹，不能选择一个已有文件。", parent=dialog)
            return
        if (folder / name).exists() and not (folder / name).is_dir():
            messagebox.showerror("目录不可用", "外层文件夹名称被一个已有文件占用，请更换名称。", parent=dialog)
            return
        output.set(str(folder))
        accepted[0] = True
        dialog.destroy()
    ttk.Button(buttons, text="更换目录…", command=change).pack(side="left")
    ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
    start_button = ttk.Button(buttons, text="开始导出", command=accept)
    start_button.pack(side="right", padx=8)
    dialog.bind("<Escape>", lambda event: dialog.destroy())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    # Measure the complete dialog before showing it, centered on its owner.
    dialog.tk.call("tk::PlaceWindow", str(dialog), "widget", str(parent))
    dialog.wait_visibility()
    dialog.grab_set()
    start_button.focus_set()
    parent.wait_window(dialog)
    return accepted[0]


def detect(path: Path) -> dict:
    """Read signatures, never infer ASCII solely from the filename."""
    with path.open("rb") as f:
        raw = f.read(131072)
    result = {"file": str(path), "kind": path.suffix.lower()[1:], "ok": False}
    if raw.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        return result | {"reason": "Altium 二进制文件；需在 AD 另存为 ASCII，或重新从嘉立创导出 Altium 工程。"}
    encoding = "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        try:
            raw.decode(encoding)
        except UnicodeDecodeError:
            encoding = "gb18030"
    text = raw.decode(encoding, errors="replace")
    if "\x00" in text:
        return result | {"reason": "包含二进制数据，不能直接交给 ASCII 转换器。"}
    if path.suffix.lower() == ".pcbdoc":
        ok = bool(re.search(r"\|RECORD=Board\|", text, re.I)) and bool(
            re.search(r"\|VERSION=5\.0(?:1)?(?:\||\r|\n|$)", text, re.I))
        reason = "Altium PCB ASCII 5.x（常见版本字段 5.01）"
    else:
        ok = bool(re.search(r"\|HEADER=Protel for Windows - Schematic Capture Ascii File Version 5\.0(?:\||\r|\n|$)", text, re.I))
        reason = "Altium Schematic ASCII 5.0"
    return result | {"ok": ok, "encoding": encoding,
                     "reason": reason if ok else "未识别为所需的 Altium ASCII 5.x 文档；不会仅修改扩展名冒充转换。"}


def normalize_pcb(source: Path, target: Path, strict_text=True) -> dict:
    """Supply importer bookkeeping and derived text bounds, without changing pad/copper data."""
    encoding = detect(source)["encoding"]
    lines = source.read_text(encoding=encoding).splitlines(keepends=True)
    indexed = {"Component", "Pad", "Via", "Track", "Arc", "Text", "Fill", "Region", "Polygon", "ComponentBody"}
    used: dict[str, set[int]] = {}
    for line in lines:
        fields = dict(re.findall(r"\|([^|=]+)=([^|\r\n]*)", line))
        if fields.get("RECORD") in indexed and "INDEXFORSAVE" in fields:
            used.setdefault(fields["RECORD"], set()).add(int(fields["INDEXFORSAVE"]))
    indexes, bounds, components, pads = 0, 0, 0, 0
    normalized = []
    def mil(value):
        m = re.fullmatch(r"\s*([-+\d.eE]+)\s*(mil|mm)?\s*", value, re.I)
        if not m:
            raise ConversionError(f"无法解析文字尺寸：{value}")
        return float(m[1]) * (1000 / 25.4 if (m[2] or "mil").lower() == "mm" else 1)
    for line in lines:
        fields = dict(re.findall(r"\|([^|=]+)=([^|\r\n]*)", line))
        kind = fields.get("RECORD")
        components += kind == "Component"
        pads += kind == "Pad" and fields.get("COMPONENT", "-1") != "-1"
        additions = []
        if kind in indexed and "INDEXFORSAVE" not in fields:
            taken = used.setdefault(kind, set())
            n = 0
            while n in taken:
                n += 1
            taken.add(n)
            additions.append(f"INDEXFORSAVE={n}")
            indexes += 1
        if kind == "Text" and any(k not in fields for k in ("X1", "Y1", "X2", "Y2")):
            if strict_text:
                raise ConversionError("PCB 文字缺少 X1/Y1/X2/Y2 包围框。严格模式需由 AD 另存 ASCII 后再运行。")
            # Bounds are derived metadata, not the actual text anchor, font size or rotation.
            # No universal exact glyph metrics exist across the two EDA font systems.
            x, y, height = (mil(fields[k]) for k in ("X", "Y", "HEIGHT"))
            angle = math.radians(float(fields.get("ROTATION", "0")))
            width = height * max(1, len(fields.get("TEXT", "")))
            if fields.get("MIRROR", "FALSE").upper() == "TRUE":
                width = -width
            points = [(x + dx * math.cos(angle) - dy * math.sin(angle),
                       y + dx * math.sin(angle) + dy * math.cos(angle))
                      for dx, dy in ((0, 0), (width, 0), (0, height), (width, height))]
            values = (min(p[0] for p in points), min(p[1] for p in points),
                      max(p[0] for p in points), max(p[1] for p in points))
            additions += [f"{k}={v:.6f}mil" for k, v in zip(("X1", "Y1", "X2", "Y2"), values) if k not in fields]
            bounds += 1
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        normalized.append(line.rstrip("\r\n") + "".join("|" + x for x in additions) + ending)
    target.write_text("".join(normalized), encoding="utf-8" if encoding.startswith("utf") else encoding, newline="")
    warnings = [f"估算了 {bounds} 个文字包围框；文字位置/占宽需检查。焊盘坐标、尺寸、引脚号未更改。"] if bounds else []
    return {"added_indexes": indexes, "estimated_text_bounds": bounds,
            "source_components": components, "source_component_pads": pads, "warnings": warnings}


def safe_unzip(source: Path, target: Path) -> None:
    """No path traversal, symlinks, duplicate Windows names or zip bombs."""
    with zipfile.ZipFile(source) as z:
        if len(z.infolist()) > 20000 or sum(i.file_size for i in z.infolist()) > 2 * 1024**3:
            raise ConversionError("ZIP 超过 20,000 项或解压大小超过 2 GB。")
        seen = set()
        for entry in z.infolist():
            name = entry.filename.replace("\\", "/")
            parts = name.rstrip("/").split("/")
            reserved = {"con", "prn", "aux", "nul"} | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)}
            if (name.startswith("/") or any(p in {"", ".", ".."} or re.search(r'[<>:"?*]', p)
                    or p.endswith((" ", ".")) or p.split(".")[0].casefold() in reserved for p in parts)
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ConversionError(f"ZIP 含不安全路径：{entry.filename}")
            dest = target.joinpath(*parts)
            if not dest.resolve().is_relative_to(target.resolve()):
                raise ConversionError("ZIP 路径超出解压目录。")
            key = str(dest).casefold()
            if key in seen:
                raise ConversionError(f"ZIP 含重名路径：{entry.filename}")
            seen.add(key)
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as src, dest.open("xb") as dst:
                    shutil.copyfileobj(src, dst)


def discover(source: Path, scratch: Path, excluded=()) -> tuple[Path, list[Path]]:
    # Resolve both sides: Windows can expose the same directory through an 8.3 alias.
    excluded = tuple(Path(path).resolve() for path in excluded)
    if source.suffix.lower() == ".zip" and source.is_file():
        root = scratch / "unpacked"
        root.mkdir()
        safe_unzip(source, root)
    elif source.is_dir():
        root = source
    elif source.is_file() and source.suffix.lower() in DESIGN_EXTS:
        return source.parent, [source]
    else:
        raise ConversionError("输入必须是嘉立创导出的 ZIP、解压目录、SchDoc 或 PcbDoc。")
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DESIGN_EXTS
                   and not any(p.resolve().is_relative_to(e) for e in excluded)
                   and not any(x.casefold() in IGNORE_DIRS for x in p.relative_to(root).parts[:-1]))
    if not files:
        raise ConversionError("没有找到 SchDoc / PcbDoc；请导出 Altium Designer 工程，而非 epro、Gerber 或二进制库。")
    return root, files


def find_cadence(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        candidates += [Path(os.environ[k]) for k in ("CDSROOT", "CDS_INST_DIR") if os.environ.get(k)]
        candidates += sorted((p for drive in "CDEFG" for p in Path(f"{drive}:/Cadence").glob("SPB_*")), reverse=True)
    for root in candidates:
        if (root / "tools/bin/allegro.exe").is_file() and (root / "tools/bin/Capture.exe").is_file():
            return root.resolve()
    raise ConversionError("未找到 Cadence 安装目录，请用 --cadence 指定 SPB_24.1 目录。")


def run_process(argv: list[str], cwd: Path, log: Path, timeout: int,
                env: dict, *, visible=False, marker: Path | None = None) -> None:
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1 if visible else 0
    with log.open("wb") as stream:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                startupinfo=startup)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker is not None and marker.exists():
                # Capture may route the script to an already open session. Never kill that session.
                return
            code = proc.poll()
            if code is not None:
                if code != 0:
                    raise ConversionError(f"{Path(argv[0]).name} 返回 {code}，日志：{log}")
                if marker is None:
                    return
            time.sleep(0.3)
        if marker is None and proc.poll() is None:
            proc.terminate()  # Only our dedicated PCB/batch process, never the user's Capture.
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise ConversionError(f"等待超时（{timeout} 秒）：{log}。请检查许可或转换器提示。Capture 任务可能仍在运行。")


def write_capture_script(job: Path, root: Path) -> Path:
    # Load the installed Cadence runtime in its own Tcl process. Never automate the user's Capture.
    p = job.as_posix()
    script = '''load "@ROOT@/tools/bin/orDb_Dll_Tcl64.dll" DboTclWriteBasic
load "@ROOT@/tools/bin/orCommonTcl64.dll" OrCommonTcl
proc GetProductVersion {} {return [COrTclUtils_sGetReleaseVersion]}
# The conversion functions only need data APIs. Replace the unused web-dialog dependency,
# NOT licensing/database functions. Actual conversion and DB access remain Cadence code.
namespace eval ::orFlowWuia {}
package provide orFlowWuia 1.4
proc tk_messageBox {args} {
    array set opts {-type ok -icon info -message {}}
    array set opts $args
    puts "TRANSLATOR MESSAGE: $opts(-message)"
    if {$opts(-type) ne "ok" || $opts(-icon) eq "error"} {
        error "Translator requires attention: $opts(-message)"
    }
    return ok
}
lappend auto_path "@ROOT@/tools/capture/tclscripts/orFlow"
namespace eval ::LC2C {}
proc ::LC2C::run {} {
    set logfile [open "@JOB@/capture.log" w]
    fconfigure $logfile -buffering none -encoding utf-8
    set status ERROR
    if {[catch {
        package require orFlowAlt2Cap
        set ::orFlowAlt2Cap::prjpcb_file "@JOB@/input.SchDoc"
        set ::orFlowAlt2Cap::orcad_dir "@JOB@/schematic"
        set ::orFlowAlt2Cap::lib_method ORG
        set ::orFlowAlt2Cap::formType CUSTOM
        set ::orFlowAlt2Cap::pagesize {16560 11710}
        set ::orFlowAlt2Cap::form(frame_size) CUSTOM
        file mkdir "@JOB@/schematic"
        puts $logfile "Starting Altium Schematic Translator"
        foreach step {Load_PrjPCB_Init_Globals Load_PrjPCB_Project Load_Schematic_Data CreateSymbols CreateSchematics} {
            puts $logfile "START $step"
            ::orFlowAlt2Cap::$step
            puts $logfile "DONE $step"
        }
        set libs [glob -nocomplain -directory "@JOB@/schematic" *.OLB *.olb]
        set designs [glob -nocomplain -directory "@JOB@/schematic" *.DSN *.dsn]
        if {[llength $libs] == 0 || [llength $designs] == 0} {error "No DSN/OLB generated; inspect translator log"}
        set status OK
        puts $logfile "Translation returned; generated: $libs $designs"
    } message options]} {
        puts $logfile $message
        puts $logfile [dict get $options -errorinfo]
    }
    close $logfile
    set done [open "@JOB@/capture.done.tmp" w]
    puts $done $status
    close $done
    file rename "@JOB@/capture.done.tmp" "@JOB@/capture.done"
    if {$status ne "OK"} {exit 1}
}
::LC2C::run
'''.replace("@JOB@", p).replace("@ROOT@", root.as_posix())
    script_path = job / "capture.tcl"
    script_path.write_text(script, encoding="ascii")
    return script_path


def verify_library(job: Path, root: Path, env: dict, timeout: int) -> int:
    libs = sorted((job / "schematic").glob("*.[Oo][Ll][Bb]"))
    # Use Cadence's own database reader to verify real symbols, not just a nonempty file.
    paths = " ".join('"' + p.as_posix() + '"' for p in libs)
    code = '''load "@DLL@" DboTclWriteBasic
set session [DboTclHelper_sCreateSession]
set st [DboState]
set total 0
foreach path [list @LIBS@] {
    set lib [$session GetLib [DboTclHelper_sMakeCString $path] $st]
    if {$lib == "NULL"} {error "Cannot open library $path"}
    set iter [$lib NewPackageNamesIter $st]
    set name [DboTclHelper_sMakeCString]
    set st [$iter NextName $name]
    while {[$st Failed] != 1} {
        puts "PART [DboTclHelper_sGetConstCharPtr $name]"
        incr total
        set st [$iter NextName $name]
    }
}
puts "PART_COUNT=$total"
if {$total == 0} {exit 2}
'''.replace("@DLL@", (root / "tools/bin/orDb_Dll_Tcl64.dll").as_posix()).replace("@LIBS@", paths)
    verify = job / "verify_library.tcl"
    verify.write_text(code, encoding="ascii")
    log = job / "verify_library.log"
    run_process([str(root / "tools/bin/tclsh.exe"), str(verify)], job, log, timeout, env)
    match = re.search(r"PART_COUNT=(\d+)", log.read_text(errors="replace"))
    if not match or int(match[1]) < 1:
        raise ConversionError("OrCAD 库验证失败或没有元件，参见 verify_library.log。")
    return int(match[1])


def write_pcb_script(job: Path, root: Path, board_mode=False) -> Path:
    p = job.as_posix()
    stages = '''TRL_ALTIUM2PCB_Setup_Units()
        TRL_ALTIUM2PCB_Setup_Text_Blocks()
        TRL_ALTIUM2PCB_DBCreate_Stackup()
        TRL_ALTIUM2PCB_Export_Footprints()
        TRL_ALTIUM2PCB_Dump_Device_Files()'''
    if board_mode:
        stages = 'TRL_ALTIUM2PCB_Run_Batch()\n        axlDBSave("' + p + '/result.brd")'
    code = '''loadContext("@CONTEXT@")
let((result marker logport)
    result=errset(progn(
        TRL_ALTIUM2PCB_Init()
        TRL_ALTIUM2PCB_Read_Defaults()
        trl_altium2pcb_global->session->pcbDoc="@JOB@/input.PcbDoc"
        trl_altium2pcb_global->runMode="batch"
        TRL_ALTIUM2PCB_Load_PCB_Data("@JOB@/input.PcbDoc")
        TRL_ALTIUM2PCB_Check_Directories()
        TRL_ALTIUM2PCB_Extract_Staged_Data()
        @STAGES@
        logport=outfile("@JOB@/translator.log")
        fprintf(logport "%L\\n" trl_altium2pcb_global->viewLog)
        close(logport)
        t
    ) t)
    marker=outfile("@JOB@/pcb.done")
    if(result then fprintf(marker "OK") else fprintf(marker "ERROR"))
    close(marker)
)
'''.replace("@CONTEXT@", (root / "share/pcb/etc/context/64bit/translators.cxt").as_posix()).replace("@JOB@", p).replace("@STAGES@", stages)
    (job / "convert_pcb.il").write_text(code, encoding="ascii")
    commands = ["setwindow pcb", f'skill load("{p}/convert_pcb.il")', "exit"]
    script = job / "convert_pcb.scr"
    script.write_text("\n".join(commands) + "\n", encoding="ascii")
    return script


def execute_job(job: Path, kind: str, root: Path, args, log) -> dict:
    env = os.environ.copy()
    env["CDSROOT"] = str(root)
    env["PATH"] = str(root / "tools/bin") + os.pathsep + env.get("PATH", "")
    if kind == "schdoc":
        script = write_capture_script(job, root)
        if args.prepare_only:
            return {"status": "prepared", "script": str(script)}
        log("  调用 Capture 原理图转换器…")
        marker = job / "capture.done"
        run_process([str(root / "tools/bin/tclsh.exe"), str(script)], job,
                    job / "capture_process.log", args.timeout, env)
        if not marker.exists() or marker.read_text().strip() != "OK":
            raise ConversionError("Capture 转换失败，参见 capture.log。")
        count = verify_library(job, root, env, args.timeout)
        return {"status": "success", "library_parts": count}
    script = write_pcb_script(job, root, args.board)
    if args.prepare_only:
        return {"status": "prepared", "script": str(script)}
    log("  调用 Allegro PCB 转换器…")
    command = [str(root / "tools/bin/allegro.exe"), "-s", str(script), "-product", args.product]
    if not args.show_pcb:
        command.append("-nographic")
    run_process(command, job, job / "allegro_process.log", args.timeout, env, visible=args.show_pcb)
    board = job / "result.brd"
    marker = job / "pcb.done"
    if not marker.exists() or marker.read_text().strip() != "OK":
        raise ConversionError("PCB 原生转换函数失败，参见 allegro_process.log / allegro.jrl；可用 AD 另存 ASCII 后重试。")
    journal = (job / "allegro.jrl").read_text(errors="replace") if (job / "allegro.jrl").exists() else ""
    process_log = (job / "allegro_process.log").read_text(errors="replace")
    if re.search(r"\bERROR\s*\(|\*Error\*|Unknown command|^E-", journal + "\n" + process_log, re.I | re.M):
        raise ConversionError("Allegro 执行日志含错误，请检查 allegro.jrl。")
    library = job / "library"
    library.mkdir()
    log("  收集 Allegro 封装、焊盘及依赖…")
    if args.board:
        if not board.exists():
            raise ConversionError("未生成最终 BRD。")
        run_process([str(root / "tools/bin/dump_libraries.exe"), "-pdamsfc", str(board)],
                    library, job / "export_library.log", args.timeout, env)
    else:
        for path in (job / "symbols").iterdir():
            if path.suffix.lower() in {".dra", ".psm", ".pad", ".ssm", ".bsm", ".fsm"}:
                shutil.copy2(path, library / path.name)
        if (job / "devices").exists():
            shutil.copytree(job / "devices", library / "devices")
    count = len(list(library.glob("*.psm")))
    if count == 0:
        raise ConversionError("未导出 PSM 封装，不能判定成功；请检查输入是否放置了元件。")
    expected = len({r.get("PATTERN") for r in ad_bridge.read_pcb(job / "input.PcbDoc")
                    if r.get("RECORD") == "Component" and r.get("PATTERN")})
    if count < expected:
        raise ConversionError(f"封装数量不足：输入有 {expected} 种封装，输出仅 {count} 个 PSM。")
    return {"status": "success", "footprints": count,
            "padstacks": len(list(library.glob("*.pad")))}


def convert(args, log=print) -> Path:
    source = Path(args.input).expanduser().resolve()
    output = Path(args.output or load_paths().get("output") or HERE / "output").expanduser().resolve()
    name = package_name(getattr(args, "name", None))
    dest = output / name
    for folder in (dest, dest / "symbol", dest / "footprint"):
        if folder.exists() and not folder.is_dir():
            raise ConversionError(f"导出目录位置被已有文件占用：{folder}")
    if not source.exists():
        raise ConversionError(f"输入不存在：{source}")
    if source.is_dir() and source == output:
        raise ConversionError("输出根目录不能与输入目录相同，请选择一个单独的输出目录。")
    # Native translators are much more reliable with short ASCII working paths.
    work_root = Path(args.work_root).resolve()
    if not re.fullmatch(r"[A-Za-z]:[\\/][A-Za-z0-9_\\/.-]+", str(work_root)):
        raise ConversionError("工作目录请使用英文短路径，不含空格，例如 C:\\tmp\\LCEDA_Cadence。")
    runid = getattr(args, "run_id", None) or new_run_id()
    if not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", runid):
        raise ConversionError("本次导出目录编号无效。")
    scratch = work_root / runid
    scratch.mkdir(parents=True)
    base, files = discover(source, scratch, excluded=(output, work_root) if source.is_dir() else ())
    # Freeze discovery before creating output, so nested output folders are never self-ingested.
    checks = [detect(f) | {"relative": str(f.relative_to(base))} for f in files]
    for file, check in zip(files, checks):
        if check["ok"]:
            check["missing_text_bounds"] = ad_bridge.missing_text_bounds(file) if check["kind"] == "pcbdoc" else 0
            check["requires_ad_in_strict_mode"] = bool(check["missing_text_bounds"])
    if dest.exists():
        log(f"复用已有文件夹，将更新本次导出的同名库文件：{dest}")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "symbol").mkdir(exist_ok=True)
    (dest / "footprint").mkdir(exist_ok=True)
    log(f"本次导出目录：{dest}")
    try:
        save_paths(source, output, name)
    except OSError as exc:
        log(f"路径记忆未保存（不影响本次导出）：{exc}")
    report = {"source": str(source), "work_directory": str(scratch), "output_directory": str(dest),
              "ad_policy": "字段完整则直转；默认不估算文字，缺少包围框时调用 AD 原生另存；二进制或未知格式拒绝", "checks": checks, "jobs": []}
    report_path = scratch / "report.json"
    def save_report():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["转换结果", f"输入：{source}", f"导出目录：{dest}", f"状态：{report.get('status', '处理中')}",
                 "默认仅生成库文件；完整 PCB 转换需要 --board。", ""]
        for c in checks:
            lines.append(f"格式检查：{c['relative']} — {c['reason']}")
            if c.get("requires_ad_in_strict_mode"):
                lines.append("  严格模式需要 AD 补齐文字包围框。")
        for j in report["jobs"]:
            lines += ["", f"文件：{j['source']}", f"结果：{j['status']}", f"工作目录：{scratch / j['directory']}"]
            lines.extend(f"导出文件：{dest / path}" for path in j.get("files", []))
            if j.get("ad_used"):
                lines.append("已使用 AD 原生另存补齐数据，未估算文字。")
            if "error" in j:
                lines.append("错误：" + j["error"])
            lines.extend(j.get("compatibility", {}).get("warnings", []))
        (scratch / "结果说明.txt").write_text("\n".join(lines), encoding="utf-8-sig")
    save_report()
    if any(not c["ok"] for c in checks):
        report["status"] = "input_rejected"
        save_report()
        raise ConversionError(f"有输入不是 ASCII 5.x，已停止：{report_path}")
    log(f"已识别 {len(checks)} 个 ASCII 文档，将按字段完整性决定是否跳过 AD。")
    if args.inspect:
        report["status"] = "inspected"
        save_report()
        return report_path
    root = find_cadence(args.cadence)
    report["cadence"] = str(root)
    log(f"Cadence：{root}")
    published = {}
    for index, (file, check) in enumerate(zip(files, checks), 1):
        if args.only != "all" and check["kind"] != args.only + "doc":
            continue
        key = f"{index:03d}_{check['kind']}_{hashlib.sha256(check['relative'].encode()).hexdigest()[:8]}"
        job = scratch / key
        job.mkdir()
        suffix = ".PcbDoc" if check["kind"] == "pcbdoc" else ".SchDoc"
        record = {"source": check["relative"], "directory": key, "status": "running"}
        report["jobs"].append(record)
        save_report()
        log(f"[{index}/{len(files)}] {check['relative']}")
        try:
            shutil.copyfile(file, job / ("source" + suffix))
            if check["kind"] == "pcbdoc":
                pcb_source = file
                record["ad_used"] = False
                if args.strict_text and ad_bridge.missing_text_bounds(file):
                    if args.no_ad:
                        raise ConversionError("文字包围框缺失；严格模式且指定 --no-ad，停止转换，未进行估算。")
                    if args.prepare_only:
                        ad_bridge.prepare(job, file)
                        write_pcb_script(job, root, args.board)
                        record.update(status="prepared", requires_ad=True)
                        continue
                    log("  文字包围框缺失：调用 AD 原生另存，保留精确数据（不估算）。")
                    pcb_source = ad_bridge.normalize(file, job, args.altium, args.timeout)
                    record["ad_used"] = True
                record["compatibility"] = normalize_pcb(pcb_source, job / ("input" + suffix), args.strict_text)
                for warning in record["compatibility"]["warnings"]:
                    log("  注意：" + warning)
            else:
                shutil.copyfile(file, job / ("input" + suffix))
            record.update(execute_job(job, check["kind"], root, args, log))
            if record["status"] == "success":
                record["files"] = package_job(job, check["kind"], file, dest, key, args.board, published)
        except Exception as exc:
            record.update(status="failed", error=str(exc))
            log(f"  失败：{exc}")
        finally:
            # Keep diagnostics in the work directory; export only final libraries.
            save_report()
    report["status"] = ("failed" if any(j["status"] == "failed" for j in report["jobs"])
                        else "prepared" if args.prepare_only else "success")
    if not report["jobs"]:
        report["status"] = "no_matching_documents"
    save_report()
    log(f"结果报告：{report_path}")
    if report["status"] in {"failed", "no_matching_documents"}:
        raise ConversionError(f"存在失败项或无匹配文档，详见 {report_path}")
    return report_path


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", nargs="?", help="导出的 ZIP、目录、SchDoc 或 PcbDoc")
    p.add_argument("--output", help="结果根目录，默认使用上次路径")
    p.add_argument("--name", help="自定义外层文件夹名称，内含 symbol 和 footprint")
    p.add_argument("--gui", action="store_true", help="打开界面，导出前显示路径并允许更换")
    p.add_argument("--cadence", help="Cadence SPB 安装目录")
    p.add_argument("--altium", help="AD X2.EXE 路径，仅在确有必要时调用")
    p.add_argument("--no-ad", action="store_true", help="禁止 AD 中转；遇到缺失字段明确失败")
    p.add_argument("--work-root", default="C:/tmp/LCEDA_Cadence")
    p.add_argument("--product", default="Allegro_X_Designer_Layout_plus")
    p.add_argument("--timeout", type=int, default=600, help="每个阶段超时秒数")
    p.add_argument("--only", choices=["all", "sch", "pcb"], default="all")
    p.add_argument("--inspect", action="store_true", help="仅检查格式，不启动 Cadence")
    p.add_argument("--prepare-only", action="store_true", help="仅生成原生脚本，不启动转换")
    p.add_argument("--show-pcb", action="store_true", help="显示 PCB 编辑器以处理交互提示")
    p.add_argument("--board", action="store_true", help="额外转换完整 PCB；默认仅提取封装库，不转换布线及板级规则")
    p.add_argument("--strict-text", action="store_true", default=True, help="禁止估算缺失的文字包围框（默认）")
    p.add_argument("--allow-text-estimation", action="store_false", dest="strict_text",
                   help="明确允许估算文字包围框以跳过 AD；文字位置及尺寸需检查")
    return p


def gui(options=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    window = tk.Tk()
    window.withdraw()
    window.title("嘉立创 → OrCAD / Allegro 库转换")
    window.geometry("800x590")
    options = options or parser().parse_args([])
    remembered = load_paths()
    source = tk.StringVar(value=options.input or remembered.get("input", ""))
    output = tk.StringVar(value=options.output or remembered.get("output") or str(HERE / "output"))
    name = tk.StringVar(value=options.name or remembered.get("name", ""))
    mode = tk.StringVar(value=options.only)
    inspect = tk.BooleanVar(value=options.inspect)
    show = tk.BooleanVar(value=options.show_pcb)
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="先在嘉立创导出 Altium Designer 工程，然后选择 ZIP 或解压目录。", wraplength=740).pack(anchor="w")
    ttk.Label(frame, text="字段完整则跳过 AD；缺少文字包围框时自动调用 AD 原生另存。默认不估算。", wraplength=740).pack(anchor="w", pady=(0, 12))
    for label, var in [("输入", source), ("输出目录", output)]:
        row = ttk.Frame(frame); row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=10).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        if var is source:
            ttk.Button(row, text="文件", command=lambda: source.set(filedialog.askopenfilename(parent=window, initialdir=dialog_directory(source.get()), filetypes=[("Altium 导出", "*.zip *.PcbDoc *.SchDoc"), ("所有文件", "*.*")]) or source.get())).pack(side="left")
            ttk.Button(row, text="目录", command=lambda: source.set(filedialog.askdirectory(parent=window, initialdir=dialog_directory(source.get())) or source.get())).pack(side="left")
        else:
            ttk.Button(row, text="选择", command=lambda: output.set(filedialog.askdirectory(parent=window, title="选择导出根目录", initialdir=dialog_directory(output.get()), mustexist=False) or output.get())).pack(side="left")
    row = ttk.Frame(frame); row.pack(fill="x", pady=5)
    ttk.Label(row, text="文件夹名称", width=10).pack(side="left")
    ttk.Entry(row, textvariable=name).pack(side="left", fill="x", expand=True)
    ttk.Label(frame, text="导出结构：所选目录 / 自定义名称 / symbol、footprint").pack(anchor="w")
    row = ttk.Frame(frame); row.pack(fill="x", pady=8)
    for label, value in [("原理图 + PCB", "all"), ("仅 PCB 封装", "pcb"), ("仅原理图库", "sch")]:
        ttk.Radiobutton(row, text=label, variable=mode, value=value).pack(side="left", padx=5)
    ttk.Checkbutton(frame, text="只检查格式", variable=inspect).pack(anchor="w")
    ttk.Checkbutton(frame, text="显示 PCB 编辑器（排查许可问题时使用；默认全后台）", variable=show).pack(anchor="w")
    console = scrolledtext.ScrolledText(frame, height=16, state="disabled")
    console.pack(fill="both", expand=True, pady=10)
    events = queue.Queue()
    running = [False]
    def emit(s): events.put(("log", s))
    def start():
        try:
            if not source.get().strip() or not Path(source.get()).expanduser().exists():
                raise ValueError("请选择一个存在的嘉立创导出 ZIP、文件或目录。")
            if not output.get().strip():
                raise ValueError("请填写或选择导出目录。")
            output.set(str(Path(output.get()).expanduser().resolve()))
            name.set(package_name(name.get()))
        except (OSError, ValueError, ConversionError) as exc:
            messagebox.showerror("路径不可用", str(exc), parent=window)
            return
        runid = new_run_id()
        if not confirm_export(window, output, name.get()):
            return
        args = argparse.Namespace(**vars(options))
        args.input, args.output, args.only = source.get(), output.get(), mode.get()
        args.name = name.get()
        args.run_id = runid
        args.inspect, args.show_pcb = inspect.get(), show.get()
        button.configure(state="disabled"); running[0] = True
        def work():
            try: events.put(("done", str(convert(args, emit))))
            except Exception as exc: events.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()
    button = ttk.Button(frame, text="开始", command=start); button.pack(anchor="e")
    def poll():
        while not events.empty():
            kind, value = events.get()
            console.configure(state="normal"); console.insert("end", value + "\n"); console.see("end"); console.configure(state="disabled")
            if kind in {"done", "error"}:
                running[0] = False; button.configure(state="normal")
                if kind == "done": messagebox.showinfo("完成", f"导出目录：\n{result_directory(value)}\n\nsymbol：原理图库\nfootprint：封装与焊盘库\n报告：{value}\n转换后请核对引脚编号、焊盘和尺寸。", parent=window)
                else: messagebox.showerror("未完成", value, parent=window)
        window.after(200, poll)
    def close():
        if running[0]: messagebox.showinfo("正在转换", "请等待本次转换结束或超时，再关闭窗口。", parent=window)
        else: window.destroy()
    window.protocol("WM_DELETE_WINDOW", close)
    window.update_idletasks()
    width = min(800, window.winfo_screenwidth())
    height = min(590, window.winfo_screenheight())
    x = (window.winfo_screenwidth() - width) // 2
    y = (window.winfo_screenheight() - height) // 2
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.deiconify()
    poll(); window.mainloop()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    options = parser().parse_args()
    if options.gui or not options.input:
        gui(options)
    else:
        try:
            print(convert(options))
        except Exception as error:
            print(f"错误：{error}", file=sys.stderr)
            sys.exit(1)
