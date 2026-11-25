"""calibrate.py — live calibration helper (Task 6; run WITH the user present).
Pure probe_argv/png_size/save_calib are smoke-tested; main() only saves calib
that gui_driver.load_calib accepts (non-send return/enter ban enforced at save)."""
import argparse, json, os, shlex, struct, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gui_driver import PHASES, load_calib
ENV = {**os.environ, "DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/user/1000",
       "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
def probe_argv(line):
    """shlex-split an operator command; `timeout 10` prefix caps any hang."""
    argv = shlex.split(line)
    if not argv: raise ValueError("empty command")
    return ["timeout", "10", *argv]
def png_size(path):
    with open(path, "rb") as fh: head = fh.read(24)  # IHDR is chunk #1
    if len(head) < 24 or head[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR": raise ValueError(f"not a PNG IHDR: {path}")
    return struct.unpack(">II", head[16:24])
def save_calib(calib, path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp"); tmp.write_text(json.dumps(calib, ensure_ascii=False), "utf-8")
    os.chmod(tmp, 0o600)
    try: load_calib(tmp)
    except Exception: tmp.unlink(missing_ok=True); raise  # refuse: old file kept, no stale tmp
    tmp.replace(path)
def probe(label, n=0):
    """Live-run typed commands ('=' accepts last, blank ends phase, n=exact count); {IDENT}/{TEXT} probe as sample text."""
    acc, last = [], None
    while True:
        line = input(f"[{label}] > ").strip()
        if line == "=":
            acc += [shlex.split(last)] if last else []; last = None
        elif not line and acc and (not n or len(acc) == n): return acc
        elif line:
            t = line.replace("{IDENT}", "141").replace("{TEXT}", "probe")  # {IDENT}=sample row y
            print("rc =", subprocess.run(probe_argv(t), env=ENV).returncode, "| '=' accepts")
            last = line
def main():
    ap = argparse.ArgumentParser(description="live calib session")
    ap.add_argument("--out", default=str(Path.home() / ".wechat-monitor/calib.json"))
    out, steps = Path(ap.parse_args().out), {}
    png = Path("/tmp/wm_diag/calib_full.png"); png.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["gnome-screenshot", "-f", str(png)], env=ENV); wh = png_size(png)
    except (OSError, ValueError) as exc: print("screenshot unavailable:", exc); wh = None
    while True:
        if raw := input(f"view {png}; screen WxH{f' [Enter={wh[0]}x{wh[1]}]' if wh else ' (manual, e.g. 1920 1080)'}> ").split():
            screen = [int(raw[0]), int(raw[1])]; break
        if wh: screen = list(wh); break
    while True:
        for ph in PHASES:
            if ph not in steps:
                # wl-copy daemon inherits caller fds -> redirect, else piped invocations hang
                steps[ph] = [["sh", "-c", 'wl-copy "$1" >/dev/null 2>&1', "_", "{TEXT}"], probe("paste key", 1)[0]] if ph == "paste" else probe(ph)
        try:
            save_calib({"screen": screen, "steps": steps}, out); print("saved ->", out); break
        except ValueError as exc: print("REJECTED:", exc); steps.pop(input("re-probe which phase?> ").strip(), None)
    print('roster.json: {"targets":{"<key>":{"kind":"user","ident":"RPT-备注","row":<置顶行y>,"wxid":"<可选,直连表名>"}}}; kind=group ident=chatroom id，同样必须带 row。先在微信里置顶目标，再从新截图量取置顶行 y 坐标填入 row（calibrate 不量 row）')
if __name__ == "__main__":
    main()
