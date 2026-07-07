# -*- coding: utf-8 -*-
"""
文字認識ツール
  Ctrl+Shift+X : 範囲選択 → 文章認識(日本語+英語、一瞬)      → memo.md に追記 + クリップボード
  Ctrl+Shift+Z : 範囲選択 → 数式認識(LaTeX形式、約1秒)        → memo.md に追記 + クリップボード
  Ctrl+Shift+A : 範囲選択 → 文章+数式の混在認識(2〜4秒、高精度) → memo.md に追記 + クリップボード
終了するときは、画面右下のタスクトレイのアイコンを右クリック →「終了」
"""

__version__ = "1.1.2"

# ============ 設定(ここを書き換えると動作を変えられます) ============
HOTKEY_TEXT = "ctrl+shift+x"    # 文章認識のショートカットキー
HOTKEY_MATH = "ctrl+shift+z"    # 数式認識のショートカットキー
HOTKEY_MIXED = "ctrl+shift+a"   # 文章+数式の混在認識のショートカットキー
MEMO_FILENAME = "memo.md"       # 保存先ファイル名(このフォルダの中に作られます)

# GPUメモリの自動解放
GPU_RELEASE_THRESHOLD = 0.85    # GPU全体の使用率がこれを超えたら高精度モデルを解放(0.85 = 85%)
GPU_CHECK_INTERVAL_SEC = 20     # 使用率をチェックする間隔(秒)
RELOAD_PAUSE_MIN = 30           # 手動で再読み込みした後、自動解放を止めておく時間(分)
# ====================================================================

# 注意: onnxruntime は他の部品より先に読み込む必要がある(DLL初期化の競合を避けるため)
import onnxruntime  # noqa: F401

import os
import re
import sys

# pythonw(コンソール無し)で実行したとき、標準出力への書き込みでエラーにならないようにする
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
os.environ.setdefault("DISABLE_TQDM", "true")
import time
import queue
import socket
import asyncio
import threading
import traceback
import ctypes
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMO_PATH = os.path.join(BASE_DIR, MEMO_FILENAME)
LOG_PATH = os.path.join(BASE_DIR, "ocr_tool.log")


def log(msg):
    try:
        # ログが1MBを超えたら古い分を .old に退避(肥大化防止)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


# ---- 多重起動の防止(本体として起動したときだけチェックする) ----
_lock_socket = None


def acquire_single_instance_lock():
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", 50917))
    except OSError:
        ctypes.windll.user32.MessageBoxW(
            0, "文字認識ツールはすでに起動しています。", "文字認識ツール", 0x40)
        sys.exit(0)

# ---- 高DPI対応(画面の拡大率が100%以外でも座標がずれないように) ----
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    ctypes.windll.user32.SetProcessDPIAware()

import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFont
import mss
import keyboard
import pyperclip
import winsound
import winocr
import pystray

ui_queue = queue.Queue()

# ---- 数式認識モデル(起動後にバックグラウンドで読み込む) ----
math_model = None
math_model_ready = threading.Event()


def load_math_model():
    global math_model
    try:
        from rapid_latex_ocr import LaTeXOCR
        math_model = LaTeXOCR()
        log("math model loaded")
    except Exception:
        log("math model load failed:\n" + traceback.format_exc())
    finally:
        math_model_ready.set()


# ---- 文章認識(Windows標準OCR) ----
_CJK = r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿！-｠]"


def clean_japanese_spaces(text):
    """Windows OCRが日本語の文字間に入れる余計なスペースを取り除く"""
    text = re.sub(rf"(?<={_CJK}) +", "", text)
    text = re.sub(rf" +(?={_CJK})", "", text)
    # 日本語エンジンが英単語中の r を「 と誤認識する癖への対処
    text = re.sub(r"(?<=[A-Za-z])「(?=[A-Za-z])", "r", text)
    return text


def recognize_text(img):
    orig = img
    # 小さい画像は拡大してから認識すると精度が上がる
    if img.width < 1200:
        scale = min(3, max(2, 1200 // max(img.width, 1)))
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")

    async def _run(lang):
        return await winocr.recognize_pil(img, lang)

    # 日本語OCRが入っていないPC(英語版Windows等)では英語エンジンにフォールバック
    try:
        result = asyncio.run(_run("ja"))
    except Exception:
        try:
            result = asyncio.run(_run("en"))
        except Exception:
            log("windows OCR unavailable:\n" + traceback.format_exc())
            if surya_predictors is not None:
                lines2 = [convert_surya_line(t) for t in surya_run(orig)]
                return "\n".join(line for line in lines2 if line)
            raise RuntimeError("OCRエンジンを利用できません(Windowsの言語パックを確認)")
    lines = [clean_japanese_spaces(line.text).strip() for line in result.lines]
    text = "\n".join(line for line in lines if line)

    # ほぼ英語だけの文章はWindows OCR(日本語エンジン)が苦手なので、
    # 高精度モデルが使えるならそちらで認識し直す
    chars = [c for c in text if not c.isspace()]
    if chars and surya_predictors is not None:
        ascii_ratio = sum(1 for c in chars if ord(c) < 128) / len(chars)
        if ascii_ratio > 0.7:
            try:
                lines2 = [convert_surya_line(t) for t in surya_run(orig)]
                text2 = "\n".join(line for line in lines2 if line)
                if text2:
                    return text2
            except Exception:
                log("surya text fallback failed:\n" + traceback.format_exc())
    return text


# ---- 数式認識 ----
def recognize_math(img):
    # 高精度モデル(Surya)が読み込み済みならそちらを使う(精度が高い)
    if surya_predictors is not None:
        try:
            latex = recognize_math_surya(img)
            if latex:
                return latex
        except Exception:
            log("surya math failed, falling back:\n" + traceback.format_exc())
    # 起動直後でまだ読み込み中のときは軽量モデル(RapidLaTeXOCR)で認識
    if not math_model_ready.is_set():
        ui_queue.put(("toast", "数式モデルを準備中です。少しお待ちください…"))
        math_model_ready.wait(timeout=60)
    if math_model is None:
        raise RuntimeError("数式モデルの読み込みに失敗しました(ocr_tool.log を確認)")
    import io
    buf = io.BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="PNG")
    latex, _elapse = math_model(buf.getvalue())
    return latex.strip()


# ---- 高精度認識(文章+数式、Surya OCR) ----
# モデルが大きいので、起動時にバックグラウンドで読み込む(20秒ほど)
surya_predictors = None
surya_lock = threading.Lock()
surya_suspended = False          # GPUメモリ確保のために解放中かどうか
surya_busy = 0                   # 認識処理の実行中カウント(実行中は解放しない)
gpu_release_paused_until = 0.0   # この時刻までは自動解放しない


def load_surya():
    global surya_predictors
    from surya.foundation import FoundationPredictor
    from surya.detection import DetectionPredictor
    from surya.recognition import RecognitionPredictor
    rec = RecognitionPredictor(FoundationPredictor())
    det = DetectionPredictor()
    surya_predictors = (rec, det)
    log("surya loaded")


def ensure_surya(notify=True):
    """モデルが未読み込みなら読み込む(起動直後に使われた場合のため)"""
    with surya_lock:
        if surya_predictors is None:
            if notify:
                ui_queue.put(("toast", "高精度モデルを読み込み中です(20秒ほど)…"))
            load_surya()
    return surya_predictors


def release_surya(auto=False):
    """高精度モデルを破棄してGPUメモリを解放する"""
    global surya_predictors, surya_suspended
    import gc
    with surya_lock:
        if surya_predictors is None:
            return False
        surya_predictors = None
        surya_suspended = True
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass
    log("surya released (auto)" if auto else "surya released (manual)")
    return True


def reload_surya_manual():
    """トレイメニューからの手動再読み込み。しばらく自動解放を止める"""
    global surya_suspended, gpu_release_paused_until
    surya_suspended = False
    gpu_release_paused_until = time.time() + RELOAD_PAUSE_MIN * 60
    try:
        ensure_surya(notify=True)
        ui_queue.put(("toast",
                      f"高精度モデルを読み込みました。\n今後{RELOAD_PAUSE_MIN}分間は自動解放しません"))
    except Exception:
        log("manual reload failed:\n" + traceback.format_exc())
        ui_queue.put(("toast", "モデルの再読み込みに失敗しました(ocr_tool.log を確認)"))


def query_gpu_usage():
    """(使用中MB, 全体MB) を返す。取得できなければ None"""
    import subprocess, shutil
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    if not os.path.isfile(exe):
        return None
    out = subprocess.run(
        [exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if out.returncode != 0:
        return None
    used, total = out.stdout.strip().splitlines()[0].split(",")
    return int(used), int(total)


def gpu_monitor():
    """ゲーム等でGPUメモリが逼迫したら高精度モデルを自動解放する監視ループ"""
    fail_logged = False
    while True:
        time.sleep(GPU_CHECK_INTERVAL_SEC)
        try:
            if surya_predictors is None or surya_busy > 0:
                continue
            if time.time() < gpu_release_paused_until:
                continue
            usage = query_gpu_usage()
            if usage is None:
                continue
            used, total = usage
            if used / total > GPU_RELEASE_THRESHOLD:
                log(f"gpu pressure: {used}/{total} MB")
                if release_surya(auto=True):
                    ui_queue.put((
                        "toast",
                        "GPUメモリが混み合ってきたため、高精度モデルを一時停止しました。\n"
                        "文章(X)と数式(Z)はそのまま使えます。\n"
                        "復帰: トレイアイコン右クリック →「高精度モデルを再読み込み」"))
        except Exception:
            if not fail_logged:
                log("gpu_monitor error:\n" + traceback.format_exc())
                fail_logged = True


def surya_run(img):
    """Suryaで認識し、行ごとの生テキスト(HTML風タグ入り)を返す"""
    global surya_busy
    rec, det = ensure_surya()
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 2倍に拡大した画像も渡すと小さい文字の認識精度が上がる
    # (すでに大きい画像は拡大しない: 全画面選択などでのメモリ急増・低速化を防ぐ)
    if img.width < 2000:
        highres = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    else:
        highres = img
    surya_busy += 1
    try:
        pages = rec([img], det_predictor=det, highres_images=[highres], math_mode=True)
    finally:
        surya_busy -= 1
    return [line.text for line in pages[0].text_lines]


def convert_surya_line(text):
    """Suryaの出力(HTML風タグ)をMarkdown/LaTeX形式に変換する"""
    import html
    text = text.strip()
    # 行全体が数式なら $$...$$、文中の数式は $...$ にする
    full = re.fullmatch(r"<math[^>]*>(.*?)</math>", text, flags=re.S)
    if full:
        return "$$" + html.unescape(full.group(1).strip()) + "$$"
    text = re.sub(r"<math[^>]*>(.*?)</math>",
                  lambda m: "$" + m.group(1).strip() + "$", text, flags=re.S)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)  # 残りの装飾タグを除去
    return html.unescape(text).strip()


def recognize_mixed(img):
    if surya_predictors is None and surya_suspended:
        ui_queue.put((
            "toast",
            "GPUメモリ確保のため高精度モデルは停止中です。\n"
            "トレイアイコン右クリック →「高精度モデルを再読み込み」で復帰できます"))
        return None
    lines = [convert_surya_line(t) for t in surya_run(img)]
    return "\n".join(line for line in lines if line)


def recognize_math_surya(img):
    """数式クロップをSuryaで認識し、LaTeX文字列を返す"""
    import html
    parts = []
    for t in surya_run(img):
        maths = re.findall(r"<math[^>]*>(.*?)</math>", t, flags=re.S)
        if maths:
            parts.extend(m.strip() for m in maths)
        else:
            cleaned = re.sub(r"</?[a-zA-Z][^>]*>", "", t).strip()
            if cleaned:
                parts.append(cleaned)
    return html.unescape("\n".join(parts).strip())


# ---- メモへの追記 ----
memo_write_lock = threading.Lock()


def append_to_memo(content, kind):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if kind == "math":
        body = f"$$\n{content}\n$$"
        label = "数式"
    elif kind == "mixed":
        body = content
        label = "文章+数式"
    else:
        body = content
        label = "文章"
    # 連続キャプチャ時の書き込み競合を防ぐ + 他アプリがファイルを掴んでいる場合は少し待って再試行
    with memo_write_lock:
        for attempt in range(3):
            try:
                if not os.path.exists(MEMO_PATH):
                    with open(MEMO_PATH, "w", encoding="utf-8") as f:
                        f.write("# OCRメモ\n\n")
                with open(MEMO_PATH, "a", encoding="utf-8") as f:
                    f.write(f"## {stamp}({label})\n\n{body}\n\n")
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.5)


# ---- 認識処理(ワーカースレッドで実行) ----
def process_capture(img, mode):
    try:
        if mode == "math":
            text = recognize_math(img)
        elif mode == "mixed":
            text = recognize_mixed(img)
        else:
            text = recognize_text(img)
        if text is None:  # 案内済み(モデル停止中など)
            return
        if not text:
            ui_queue.put(("toast", "文字を認識できませんでした"))
            return
        # 先にクリップボードへ。メモへの書き込みが失敗しても認識結果を失わないようにする
        try:
            pyperclip.copy(text)
        except Exception:
            log("clipboard copy failed:\n" + traceback.format_exc())
        try:
            append_to_memo(text, mode)
        except Exception:
            log("memo append failed:\n" + traceback.format_exc())
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            ui_queue.put(("toast",
                          "認識はできましたが、メモに書き込めませんでした。\n"
                          f"保存先: {MEMO_PATH}\n"
                          "(認識結果はクリップボードにコピー済みです。\n"
                          " フォルダの場所や、Windowsのランサムウェア防止設定を確認してください)"))
            return
        winsound.MessageBeep(winsound.MB_OK)
        preview = text if len(text) <= 120 else text[:120] + "…"
        ui_queue.put(("toast", f"メモに追記しました:\n{preview}"))
    except Exception:
        log("process_capture failed:\n" + traceback.format_exc())
        ui_queue.put(("toast", "エラーが発生しました(ocr_tool.log を確認)"))


# ---- 範囲選択オーバーレイ ----
class RegionSelector:
    def __init__(self, root, screenshot, vx, vy, on_done):
        self.on_done = on_done
        self.screenshot = screenshot
        self.start = None
        self.rect_id = None
        self.finished = False

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        w, h = screenshot.size
        self.top.geometry(f"{w}x{h}+{vx}+{vy}")
        self.top.attributes("-topmost", True)

        dark = screenshot.convert("RGB").point(lambda p: int(p * 0.45))
        self.photo = ImageTk.PhotoImage(dark)
        self.canvas = tk.Canvas(self.top, width=w, height=h,
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_text(
            w // 2, 40, text="ドラッグで範囲を選択 / Esc・右クリックでキャンセル",
            fill="white", font=("Meiryo UI", 14))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<ButtonPress-3>", lambda e: self.finish(None))
        self.top.bind("<Escape>", lambda e: self.finish(None))
        # 何らかの理由でウィンドウが外部から閉じられても「選択中」のまま残らないようにする
        self.top.bind("<Destroy>", self._on_destroy)
        self.top.focus_force()

    def finish(self, crop):
        """結果を一度だけ通知してウィンドウを閉じる(多重呼び出し・異常終了に安全)"""
        if self.finished:
            return
        self.finished = True
        try:
            if self.top.winfo_exists():
                self.top.destroy()
        except tk.TclError:
            pass
        self.on_done(crop)

    def _on_destroy(self, e):
        if e.widget is self.top:
            self.finish(None)

    def on_press(self, e):
        self.start = (e.x, e.y)

    def on_drag(self, e):
        if self.start is None:
            return
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start[0], self.start[1], e.x, e.y,
            outline="#00c8ff", width=2)

    def on_release(self, e):
        if self.start is None:
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        left, top = min(x0, x1), min(y0, y1)
        right, bottom = max(x0, x1), max(y0, y1)
        if right - left < 8 or bottom - top < 8:
            self.finish(None)
            return
        crop = self.screenshot.crop((left, top, right, bottom))
        self.finish(crop)


# ---- 通知トースト ----
def show_toast(root, message):
    toast = tk.Toplevel(root)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)
    frame = tk.Frame(toast, bg="#222831", padx=14, pady=10)
    frame.pack()
    tk.Label(frame, text=message, bg="#222831", fg="white",
             font=("Meiryo UI", 10), justify="left", wraplength=360).pack()
    toast.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    tw, th = toast.winfo_width(), toast.winfo_height()
    toast.geometry(f"+{sw - tw - 24}+{sh - th - 80}")
    toast.after(3500, toast.destroy)


# ---- タスクトレイ ----
def make_tray_icon_image():
    img = Image.new("RGB", (64, 64), "#1a73e8")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/meiryo.ttc", 40)
        d.text((32, 30), "字", font=font, fill="white", anchor="mm")
    except OSError:
        d.rectangle((16, 16, 48, 48), fill="white")
    return img


def setup_tray():
    def on_reload(icon, item):
        threading.Thread(target=reload_surya_manual, daemon=True).start()

    def on_release(icon, item):
        if release_surya(auto=False):
            ui_queue.put(("toast", "高精度モデルを停止してGPUメモリを解放しました。\n"
                                   "復帰はメニューの「高精度モデルを再読み込み」から"))
        else:
            ui_queue.put(("toast", "高精度モデルはすでに停止しています"))

    def open_memo():
        if os.path.exists(MEMO_PATH):
            os.startfile(MEMO_PATH)
        else:
            # 無反応だと「作られていない」ようにしか見えないので、場所を案内する
            ui_queue.put(("toast",
                          "メモはまだ作成されていません(1回認識すると作られます)。\n"
                          f"保存先: {MEMO_PATH}"))

    menu = pystray.Menu(
        pystray.MenuItem("メモを開く", open_memo, default=True),
        pystray.MenuItem("フォルダを開く", lambda: os.startfile(BASE_DIR)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("高精度モデルを再読み込み", on_reload),
        pystray.MenuItem("GPUメモリを解放(ゲーム前に)", on_release),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("終了", lambda: ui_queue.put(("quit", None))),
    )
    icon = pystray.Icon("ocr_tool", make_tray_icon_image(),
                        "文字認識ツール (X:文章 / Z:数式 / A:混在 ※Ctrl+Shift+)", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


# ---- メイン ----
def main():
    root = tk.Tk()
    root.withdraw()

    state = {"selecting": False}
    tray_icon = setup_tray()

    threading.Thread(target=load_math_model, daemon=True).start()

    def preload_surya():
        try:
            ensure_surya(notify=False)
        except Exception:
            log("surya preload failed:\n" + traceback.format_exc())

    threading.Thread(target=preload_surya, daemon=True).start()
    threading.Thread(target=gpu_monitor, daemon=True).start()

    # suppress=True: 押したキーを他のアプリに渡さない(ブラウザ等のショートカットと衝突しないように)
    keyboard.add_hotkey(HOTKEY_TEXT, lambda: ui_queue.put(("capture", "text")), suppress=True)
    keyboard.add_hotkey(HOTKEY_MATH, lambda: ui_queue.put(("capture", "math")), suppress=True)
    keyboard.add_hotkey(HOTKEY_MIXED, lambda: ui_queue.put(("capture", "mixed")), suppress=True)

    def start_capture(mode):
        if state["selecting"]:
            return
        state["selecting"] = True
        try:
            with mss.mss() as sct:
                mon = sct.monitors[0]  # 全モニタをまとめた仮想画面
                shot = sct.grab(mon)
                screenshot = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                vx, vy = mon["left"], mon["top"]

            def on_done(crop):
                state["selecting"] = False
                if crop is not None:
                    threading.Thread(target=process_capture,
                                     args=(crop, mode), daemon=True).start()

            RegionSelector(root, screenshot, vx, vy, on_done)
        except Exception:
            # スリープ復帰直後などは画面の取得に失敗することがある。
            # 失敗したまま「選択中」フラグが残ると全キーが死ぬので必ず戻す
            state["selecting"] = False
            log("start_capture failed:\n" + traceback.format_exc())
            ui_queue.put(("toast", "画面の取得に失敗しました。もう一度キーを押してください"))

    def poll():
        # このループが止まると全ホットキーが無反応になるため、
        # どんなエラーが起きても必ず次回の実行を予約し直す
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "capture":
                    start_capture(payload)
                elif kind == "toast":
                    show_toast(root, payload)
                elif kind == "quit":
                    tray_icon.stop()
                    keyboard.unhook_all()
                    root.destroy()
                    os._exit(0)
        except queue.Empty:
            pass
        except Exception:
            log("poll error:\n" + traceback.format_exc())
            state["selecting"] = False  # 引っかかったままにならないよう解除
        root.after(80, poll)

    poll()
    log(f"started (v{__version__}) memo={MEMO_PATH}")
    root.mainloop()


if __name__ == "__main__":
    try:
        acquire_single_instance_lock()
        main()
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        raise
