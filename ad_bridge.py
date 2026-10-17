"""Use Altium's own script/save engine only when Cadence needs missing derived fields."""
from pathlib import Path
import math
import re
import shutil
import subprocess
import time


def read_pcb(path):
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("AD 导出文件的编码无法识别。")
    if not text.lstrip().startswith("|RECORD=Board|"):
        raise RuntimeError("AD 没有生成 PCB ASCII 文件。")
    return [dict(re.findall(r"\|([^|=]+)=([^|\r\n]*)", line)) for line in text.splitlines()]


def missing_text_bounds(path):
    return sum(r.get("RECORD") == "Text" and any(k not in r for k in ("X1", "Y1", "X2", "Y2"))
               for r in read_pcb(path))


def validate_geometry(source, saved):
    """AD may add defaults, but must preserve supplied pad/component geometry."""
    a, b = read_pcb(source), read_pcb(saved)
    if missing_text_bounds(saved):
        raise RuntimeError("AD 导出仍缺少文字包围框。")
    for kind, keys in {
        "Component": ("X", "Y", "ROTATION", "PATTERN", "SOURCEDESIGNATOR"),
        "Pad": ("X", "Y", "XSIZE", "YSIZE", "ROTATION", "HOLESIZE", "NAME", "COMPONENT", "LAYER", "SHAPE"),
    }.items():
        old = [r for r in a if r.get("RECORD") == kind]
        new = [r for r in b if r.get("RECORD") == kind]
        identity = (lambda r: (r.get("ID", ""),)) if kind == "Component" else (
            lambda r: (r.get("COMPONENT", ""), r.get("NAME", ""), dimension(r.get("X", "0")), dimension(r.get("Y", "0"))))
        old.sort(key=identity); new.sort(key=identity)
        if len(old) != len(new):
            raise RuntimeError(f"AD 另存后 {kind} 数量发生变化，停止继续转换。")
        for before, after in zip(old, new):
            for key in keys:
                if key not in before:
                    continue
                if key not in after:
                    raise RuntimeError(f"AD 另存后缺少 {kind}.{key}。")
                if key in {"X", "Y", "XSIZE", "YSIZE", "ROTATION", "HOLESIZE"}:
                    equal = math.isclose(dimension(before[key]), dimension(after[key]), abs_tol=0.001, rel_tol=0)
                else:
                    equal = before[key] == after[key]
                if not equal:
                    raise RuntimeError(f"AD 另存后 {kind}.{key} 变化：{before[key]} -> {after[key]}。")


def dimension(value):
    m = re.fullmatch(r"\s*([-+\d.eE]+)\s*(mil|mm)?\s*", value, re.I)
    if not m:
        raise RuntimeError(f"无法解析尺寸：{value}")
    return float(m[1]) * (1000 / 25.4 if (m[2] or "mil").lower() == "mm" else 1)


def find_altium(explicit=None):
    if explicit:
        exe = Path(explicit)
        if exe.is_file():
            return exe.resolve()
        raise RuntimeError(f"Altium 程序不存在：{exe}")
    candidates = []
    for drive in "CDEFG":
        candidates.extend(Path(f"{drive}:/Altium").glob("AD*/X2.EXE"))
        candidates.extend(Path(f"{drive}:/Program Files/Altium").glob("*/X2.EXE"))
    if candidates:
        return sorted(candidates, reverse=True)[0].resolve()
    raise RuntimeError("缺少精确文字包围框且未找到 AD；请用 --altium 指定 X2.EXE，或先在 AD 另存 ASCII。")


def prepare(job, source):
    bridge = job / "ad_bridge"
    bridge.mkdir()
    pcb = bridge / "input.PcbDoc"
    shutil.copyfile(source, pcb)
    project = bridge / "Normalize.PrjScr"
    project.write_text("[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Normalize.pas\n", encoding="ascii")
    script = '''Procedure Normalize;
Var Doc : IServerDocument; Status : TStringList;
Begin
    Status := TStringList.Create;
    Try
        Try
            Doc := Client.OpenDocument('PCB', '@PCB@');
            If Doc = Nil Then Status.Add('ERROR: cannot open working copy')
            Else Begin
                Client.ShowDocument(Doc);
                Doc.Modified := True;
                If Doc.DoFileSave('PCB ASCII File') Then Begin
                    Status.Add('OK');
                    Doc.Modified := False;
                End Else Status.Add('ERROR: ASCII save failed');
            End;
        Except
            Status.Add('ERROR: ' + GetExceptionMessage);
        End;
        Status.SaveToFile('@MARKER@');
    Finally
        Status.Free;
    End;
End;
'''.replace("@PCB@", str(pcb)).replace("@MARKER@", str(bridge / "ad.done"))
    (bridge / "Normalize.pas").write_text(script, encoding="ascii")
    return project, pcb


def normalize(source, job, explicit=None, timeout=180):
    exe = find_altium(explicit)
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq X2.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, check=True).stdout.lower()
    # Reusing an unrelated interactive AD session would defeat the isolated worker contract.
    if b'"x2.exe"' in running:
        raise RuntimeError("检测到 AD 已运行。请保存并关闭 AD 后重试；脚本不会接管或关闭你正在编辑的 AD 会话。")
    project, pcb = prepare(job, source)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    # Altium parses the raw command line. list2cmdline would escape its internal quotes,
    # which makes -R silently ineffective. No shell is used here.
    cmd = f'"{exe}" -RScriptingSystem:RunScript(ProjectName="{project}"|ProcName="Normalize.pas>Normalize")'
    log = job / "ad_bridge.log"
    with log.open("w", encoding="utf-8") as output:
        output.write(f"Altium: {exe}\nWorking copy: {pcb}\n")
        output.flush()
        proc = subprocess.Popen(cmd, cwd=project.parent, startupinfo=si,
                                stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            stable = 0
            previous = None
            while time.monotonic() < deadline:
                try:
                    signature = (pcb.stat().st_size, pcb.stat().st_mtime_ns)
                    ready = missing_text_bounds(pcb) == 0
                except (OSError, RuntimeError):
                    ready = False
                    signature = None
                stable = stable + 1 if ready and signature == previous else 0
                previous = signature
                if stable >= 4:
                    # Some AD versions wait in a format-warning dialog after writing the
                    # complete ASCII file. Validate the saved artifact, not a UI message.
                    validate_geometry(source, pcb)
                    output.write("ASCII ready; exact text bounds present; source pad/component geometry verified.\n")
                    return pcb
                marker = project.parent / "ad.done"
                if marker.exists() and marker.read_text(errors="replace").strip().startswith("ERROR"):
                    raise RuntimeError(marker.read_text(errors="replace"))
                if proc.poll() is not None:
                    raise RuntimeError("AD 在生成合格的 ASCII 文件前退出。")
                time.sleep(0.5)
            raise RuntimeError("AD 原生另存超时；请检查许可及 ad_bridge.log，或手动另存 ASCII 后重试。")
        finally:
            # This process was created only after confirming no AD process existed.
            # It contains only the disposable working copy, never the user's original.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
