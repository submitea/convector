"""Окно конвертера: фиолетово-чёрная тема, раскладка по эскизу пользователя."""
from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw

import core

APP_NAME = "Конвертер"
AUTHOR = "submite"
KEEP_ALIVE_MIN = 10   # после закрытия окна программа ещё столько минут ждёт в фоне: следующий запуск мгновенный (0 = выключить)
ASSETS = core.APP_DIR / "assets"

# ---------- палитра (чёрный с фиолетовым оттенком, мягкие контрасты) ----------
BG = "#0D0B14"
CARD = "#15121F"
CARD_BORDER = "#261F3B"
CHIP = "#1D1930"
CHIP_HOVER = "#2B2547"
CHIP_BORDER = "#2D2749"
ACCENT = "#8B5CF6"
ACCENT_HOVER = "#7C3AED"
ACCENT_SOFT = "#A78BFA"
TEXT = "#EEEAFB"
MUTED = "#8E88A8"
OK = "#4ADE80"
ERR = "#F87171"
WARN = "#FBBF24"
DIS_BG = "#231E37"
DIS_TX = "#6F6990"

W = 640
GLYPH = {"audio": "♪", "video": "▶", "image": "▣"}


# ---------- мелкие помощники ----------
def _load_fonts() -> bool:
    """Регистрирует шрифты из assets/fonts только для этого процесса (Windows)."""
    fdir = ASSETS / "fonts"
    files = sorted(fdir.glob("*.ttf")) if fdir.exists() else []
    if sys.platform == "win32":
        for f in files:
            try:
                ctypes.windll.gdi32.AddFontResourceExW(str(f), 0x10, 0)  # FR_PRIVATE
            except Exception:
                pass
    return bool(files)


def _short(s: str, n: int = 44) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _tail(s: str, n: int = 34) -> str:
    return s if len(s) <= n else "…" + s[-(n - 1):]


def _plural(n: int, forms=("файл", "файла", "файлов")) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _fmt_size(b: float) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.2f} ГБ"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.1f} МБ"
    return f"{max(b / 1024, 0.1):.0f} КБ"


def _fmt_dur(sec: float) -> str:
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _info_lines(info, n):
    if not info:
        return "Не удалось прочитать файл", "", ""
    c = info["cat"]
    size = _fmt_size(info["size"])
    more = f"   +ещё {n - 1}" if n > 1 else ""
    if c == "audio":
        l1 = " — ".join(x for x in (info.get("artist"), info.get("title")) if x) or info["name"]
        l2 = " · ".join(x for x in (info.get("album"), info.get("year")) if x)
        parts = [info["ext"], _fmt_dur(info.get("dur"))]
        if info.get("sr"):
            parts.append(f"{info['sr'] / 1000:g} kHz")
        if info.get("br"):
            parts.append(f"{info['br']} kbps")
        parts.append(size)
        return _short(l1, 40), _short(l2, 46), _short(" · ".join(parts) + more, 62)
    if c == "video":
        l1 = f"{info.get('w')}×{info.get('h')}"
        if info.get("fps"):
            l1 += f" · {info['fps']:.0f} fps"
        l1 += f" · {_fmt_dur(info.get('dur'))}"
        l2 = (info.get("vcodec") or "?").upper() + (" + " + info["acodec"].upper() if info.get("acodec") else "")
        l3 = f"{info['ext']} · {size}" + (f" · {info['br']} kbps" if info.get("br") else "") + more
        return l1, l2, _short(l3, 62)
    return (f"{info.get('w')}×{info.get('h')} px", f"{info.get('fmt') or info['ext']} · {info.get('mode')}",
            _short(size + more, 62))


def _tile() -> Image.Image:
    im = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, 159, 159), radius=32, fill=CHIP)
    return im


class App(ctk.CTk):
    def __init__(self, files, inbox: queue.Queue, standby: bool = False):
        if sys.platform == "win32":
            try:  # отдельная иконка на панели задач, а не значок Python
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ConverterPro.App")
            except Exception:
                pass
        have_fonts = _load_fonts()
        self.fam = {
            "display": "CV Display" if have_fonts else "Segoe UI Semibold",
            "bold": "CV Text Bold" if have_fonts else "Segoe UI Semibold",
            "text": "CV Text" if have_fonts else "Segoe UI",
        }
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__(fg_color=BG)
        self.title(APP_NAME)
        self.resizable(False, False)
        self._placed = False
        self._geom(760)
        self._set_icon()
        self._pop_to_front()

        self.inbox = inbox
        self.standby = standby and KEEP_ALIVE_MIN > 0
        self.hidden = False
        self.hidden_at = 0.0
        self.ui_q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.finished = False
        self.files: list[str] = []
        self.cat = None
        self.sel = None
        self.buttons: dict = {}
        self.outputs: list = []
        self.out_dir = None
        self.go_text = "Конвертировать"
        self._info_token = 0
        self._info_img = None
        self._tile_img = ctk.CTkImage(_tile(), size=(80, 80))

        self._build()
        self._rebuild_chips()
        self._update_name()
        self.add_files(files)
        self._fit_height()
        self.after(120, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- окно ----------
    def _pop_to_front(self):
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))

    def _geom(self, h, center=False):
        try:
            sc = self._get_window_scaling()
        except Exception:
            sc = 1.0
        h = min(int(h), int(self.winfo_screenheight() / sc * 0.94))
        if center or not self._placed:
            x = max(0, int((self.winfo_screenwidth() - W * sc) / 2))
            y = max(0, int((self.winfo_screenheight() - h * sc) / 2.6))
            self._placed = True
        else:
            x, y = max(0, self.winfo_x()), max(0, self.winfo_y())
        self.geometry(f"{W}x{h}+{x}+{y}")

    def _fit_height(self, center=False):
        """Окно подгоняется под содержимое: пустых зон нет."""
        self.update_idletasks()
        try:
            sc = self._get_window_scaling()
        except Exception:
            sc = 1.0
        self._geom(self.winfo_reqheight() / sc + 1, center)

    def _set_icon(self):
        try:
            self.iconbitmap(str(ASSETS / "icon.ico"))
        except Exception:
            try:
                import tkinter as tk
                self._icon_ref = tk.PhotoImage(file=str(ASSETS / "icon_256.png"))
                self.iconphoto(True, self._icon_ref)
            except Exception:
                pass

    def F(self, kind: str, size: int) -> ctk.CTkFont:
        return ctk.CTkFont(family=self.fam[kind], size=size)

    # ---------- интерфейс ----------
    def _build(self):
        PAD = 22
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # 0. шапка: лого + название слева, «+ Файл» справа
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(18, 10))
        logo = ctk.CTkImage(Image.open(ASSETS / "icon_256.png"), size=(42, 42))
        ctk.CTkLabel(head, image=logo, text="").pack(side="left")
        ctk.CTkLabel(head, text=APP_NAME, font=self.F("display", 22), text_color=TEXT).pack(side="left", padx=(12, 0))
        ctk.CTkButton(head, text="＋  Файл", width=104, height=38, corner_radius=12, font=self.F("bold", 13),
                      fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, border_width=1,
                      border_color=CHIP_BORDER, command=self._pick).pack(side="right")

        # 1. название файла
        namebox = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1,
                               border_color=CARD_BORDER, height=44)
        namebox.grid(row=1, column=0, sticky="ew", padx=PAD, pady=(0, 10))
        namebox.pack_propagate(False)
        self.name_lbl = ctk.CTkLabel(namebox, text="", anchor="w", font=self.F("bold", 14), text_color=TEXT)
        self.name_lbl.pack(expand=True, fill="x", padx=16)

        # 2. большая карточка «В какой формат конвертировать» + блок «О файле»
        self.card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=18, border_width=1, border_color=CARD_BORDER)
        self.card.grid(row=2, column=0, sticky="nsew", padx=PAD, pady=(0, 10))
        ctk.CTkLabel(self.card, text="В какой формат конвертировать?", anchor="w",
                     font=self.F("display", 14), text_color=TEXT).pack(fill="x", padx=20, pady=(16, 6))

        self.info = ctk.CTkFrame(self.card, fg_color="transparent")
        self.info.pack(side="bottom", fill="x", padx=18, pady=(0, 16))
        ctk.CTkFrame(self.info, height=1, fg_color=CARD_BORDER).pack(fill="x", pady=(0, 14))
        irow = ctk.CTkFrame(self.info, fg_color="transparent", height=80)
        irow.pack(fill="x")
        irow.pack_propagate(False)
        self.thumb = ctk.CTkLabel(irow, image=self._tile_img, text="♪", compound="center", width=80, height=80,
                                  font=self.F("display", 26), text_color=MUTED)
        self.thumb.pack(side="left")
        txt = ctk.CTkFrame(irow, fg_color="transparent")
        txt.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.i1 = ctk.CTkLabel(txt, text="", anchor="w", font=self.F("bold", 14), text_color=TEXT)
        self.i1.pack(fill="x", pady=(6, 0))
        self.i2 = ctk.CTkLabel(txt, text="", anchor="w", font=self.F("text", 12), text_color=MUTED)
        self.i2.pack(fill="x")
        self.i3 = ctk.CTkLabel(txt, text="", anchor="w", font=self.F("bold", 12), text_color=ACCENT_SOFT)
        self.i3.pack(fill="x", pady=(2, 0))
        drow = ctk.CTkFrame(self.info, fg_color="transparent")
        drow.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(drow, text="Сохранять в:", font=self.F("text", 12), text_color=MUTED).pack(side="left")
        self.dest_lbl = ctk.CTkLabel(drow, text="", font=self.F("bold", 12), text_color=TEXT)
        self.dest_lbl.pack(side="left", padx=(8, 0))
        self.btn_dest = ctk.CTkButton(drow, text="Изменить…", width=100, height=30, corner_radius=10,
                                      font=self.F("bold", 12), fg_color=CHIP, hover_color=CHIP_HOVER,
                                      text_color=TEXT, border_width=1, border_color=CHIP_BORDER,
                                      command=self._choose_dir)
        self.btn_dest.pack(side="right")
        self.btn_dest_reset = ctk.CTkButton(drow, text="✕", width=30, height=30, corner_radius=10,
                                            font=self.F("bold", 12), fg_color=CHIP, hover_color=CHIP_HOVER,
                                            text_color=TEXT, border_width=1, border_color=CHIP_BORDER,
                                            command=self._reset_dir)
        self._update_dest()

        self.card_body = ctk.CTkFrame(self.card, fg_color="transparent")
        self.card_body.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        # 3. средний ряд: настройки (слева) + картинка (справа)
        mid = ctk.CTkFrame(self, fg_color="transparent", height=170)
        mid.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(0, 10))
        mid.grid_propagate(False)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(mid, fg_color=CARD, corner_radius=18, border_width=1, border_color=CARD_BORDER)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        side.pack_propagate(False)
        ctk.CTkLabel(side, text="НАСТРОЙКИ", anchor="w", font=self.F("bold", 11),
                     text_color=ACCENT_SOFT).pack(fill="x", padx=20, pady=(12, 0))
        inner = ctk.CTkFrame(side, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=18, pady=(6, 10))

        self.stems_var = ctk.BooleanVar(value=False)
        self.sec_stems = ctk.CTkFrame(inner, fg_color="transparent")
        ctk.CTkCheckBox(self.sec_stems, text="Добавить акапеллу?", variable=self.stems_var,
                        command=self._on_stems, font=self.F("bold", 14), text_color=TEXT,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color="#4C4470",
                        checkmark_color="white", corner_radius=7, border_width=2,
                        checkbox_width=24, checkbox_height=24).pack(anchor="w")
        row = ctk.CTkFrame(self.sec_stems, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(row, text="Формат акапеллы", font=self.F("text", 13), text_color=MUTED).pack(side="left")
        self.stem_var = ctk.StringVar(value="FLAC")
        self.stem_menu = self._menu(row, ["FLAC", "WAV", "MP3"], self.stem_var)
        self.stem_menu.pack(side="right")
        self.stem_menu.configure(state="disabled")

        self.sec_br = ctk.CTkFrame(inner, fg_color="transparent")
        ctk.CTkLabel(self.sec_br, text="Битрейт, kbps", font=self.F("text", 13), text_color=MUTED).pack(side="left")
        self.br_var = ctk.StringVar(value="320")
        self._menu(self.sec_br, ["128", "192", "256", "320"], self.br_var).pack(side="right")

        self.sec_q = ctk.CTkFrame(inner, fg_color="transparent")
        ctk.CTkLabel(self.sec_q, text="Качество", font=self.F("text", 13), text_color=MUTED).pack(side="left")
        self.q_label = ctk.CTkLabel(self.sec_q, text="92", width=32, font=self.F("bold", 13), text_color=TEXT)
        self.q_label.pack(side="right")
        self.q_slider = ctk.CTkSlider(self.sec_q, from_=50, to=100, number_of_steps=50, width=170,
                                      fg_color=CHIP, progress_color=ACCENT, button_color=ACCENT_SOFT,
                                      button_hover_color="white",
                                      command=lambda v: self.q_label.configure(text=str(int(v))))
        self.q_slider.set(92)
        self.q_slider.pack(side="right", padx=10)

        self.sec_none = ctk.CTkLabel(inner, text="Для этого формата настройки не нужны",
                                     font=self.F("text", 13), text_color=MUTED, anchor="w")

        hero_frame = ctk.CTkFrame(mid, fg_color=CARD, corner_radius=22, border_width=2,
                                  border_color=ACCENT, width=170, height=170)
        hero_frame.grid(row=0, column=1, sticky="ns")
        hero_frame.pack_propagate(False)
        hero = ctk.CTkImage(Image.open(ASSETS / "hero.png"), size=(160, 160))
        ctk.CTkLabel(hero_frame, image=hero, text="").pack(expand=True)

        # 4. статус
        st = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1,
                          border_color=CARD_BORDER, height=92)
        st.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(0, 10))
        st.pack_propagate(False)
        self.progress = ctk.CTkProgressBar(st, height=8, corner_radius=4, fg_color=CHIP, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
        self.status = ctk.CTkLabel(st, text="Готов к работе", anchor="nw", justify="left", wraplength=560,
                                   font=self.F("text", 13), text_color=MUTED)
        self.status.pack(side="top", fill="both", expand=True, padx=16, pady=(12, 6))

        # 5. кнопка справа внизу
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", padx=PAD, pady=(0, 6))
        bottom.grid_columnconfigure(0, weight=1)
        self.btn_open = ctk.CTkButton(bottom, text="Показать в папке", height=50, width=190, corner_radius=14,
                                      font=self.F("bold", 13), fg_color=CHIP, hover_color=CHIP_HOVER,
                                      text_color=TEXT, border_width=1, border_color=CHIP_BORDER,
                                      command=self._open_folder)
        self.btn_open.grid(row=0, column=0, sticky="w")
        self.btn_open.grid_remove()
        self.btn_go = ctk.CTkButton(bottom, text=self.go_text, height=50, width=240, corner_radius=14,
                                    font=self.F("display", 14), command=self._go_clicked)
        self.btn_go.grid(row=0, column=1, sticky="e")

        # 6. подпись автора
        sig = ctk.CTkFrame(self, fg_color="transparent")
        sig.grid(row=6, column=0, pady=(2, 12))
        ctk.CTkLabel(sig, text="by ", font=self.F("text", 12), text_color=MUTED).pack(side="left")
        ctk.CTkLabel(sig, text=AUTHOR, font=self.F("display", 12), text_color=ACCENT_SOFT).pack(side="left")

    def _menu(self, parent, values, var):
        return ctk.CTkOptionMenu(parent, values=values, variable=var, width=100, height=32, corner_radius=10,
                                 font=self.F("bold", 13), dropdown_font=self.F("bold", 13),
                                 fg_color=CHIP, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                                 text_color=TEXT, dropdown_fg_color=CARD, dropdown_hover_color=CHIP_HOVER,
                                 dropdown_text_color=TEXT)

    # ---------- файлы ----------
    def _pick(self):
        exts = " ".join(f"*.{e}" for e in sorted(core.ALL_EXT))
        paths = filedialog.askopenfilenames(filetypes=[("Поддерживаемые файлы", exts), ("Все файлы", "*.*")])
        if paths:
            self.add_files(paths)

    def _new_batch(self):
        """Новые файлы после завершённой конвертации — это новая партия: старый список и статус очищаются."""
        self.files = []
        self.outputs = []
        self.finished = False
        self.go_text = "Конвертировать"
        self.progress.set(0)
        self._set_status("Готов к работе")
        self.btn_open.grid_remove()

    def add_files(self, paths):
        new = [str(p) for p in paths if core.category(p) and str(p) not in self.files]
        if not new:
            return
        if self.finished and not self.busy:
            self._new_batch()
        self.files.extend(new)
        counts = Counter(core.category(f) for f in self.files)
        new_cat = counts.most_common(1)[0][0]
        if new_cat != self.cat and not self.busy:
            self.cat = new_cat
            self._rebuild_chips()
        self._update_name()
        self._load_info()
        self._refresh()
        self._fit_height()

    def _active_files(self):
        return [f for f in self.files if core.category(f) == self.cat]

    def _update_name(self):
        act = self._active_files()
        if not act:
            self.name_lbl.configure(text="Файл не выбран", text_color=MUTED)
            return
        if len(act) == 1:
            txt = _short(Path(act[0]).name)
        else:
            txt = f"{len(act)} {_plural(len(act))}  ·  {_short(Path(act[0]).name, 28)}"
        skipped = len(self.files) - len(act)
        if skipped:
            txt += f"   (пропущено: {skipped})"
        self.name_lbl.configure(text=txt, text_color=TEXT)

    # ---------- «О файле» и папка сохранения ----------
    def _load_info(self):
        act = self._active_files()
        if not act:
            self.info.pack_forget()
            return
        self.info.pack(side="bottom", fill="x", padx=18, pady=(0, 16), before=self.card_body)
        self._info_token += 1
        tok, f, cat, n = self._info_token, act[0], self.cat, len(act)
        self.thumb.configure(image=self._tile_img, text=GLYPH.get(cat, ""))
        self.i1.configure(text="Читаю файл…")
        self.i2.configure(text="")
        self.i3.configure(text="")

        def job():
            try:
                info = core.file_info(f)
            except Exception:
                info = None
            self.ui_q.put(("info", tok, info, core.make_thumb(f, cat, 160), n))
        threading.Thread(target=job, daemon=True).start()

    def _apply_info(self, tok, info, thumb, n):
        if tok != self._info_token:
            return
        l1, l2, l3 = _info_lines(info, n)
        self.i1.configure(text=l1)
        self.i2.configure(text=l2)
        self.i3.configure(text=l3)
        if thumb is not None:
            self._info_img = ctk.CTkImage(thumb, size=(80, 80))
            self.thumb.configure(image=self._info_img, text="")

    def _update_dest(self):
        if self.out_dir:
            self.dest_lbl.configure(text=_tail(self.out_dir))
            self.btn_dest_reset.pack(side="right", padx=(0, 6))
        else:
            self.dest_lbl.configure(text="рядом с исходным файлом")
            self.btn_dest_reset.pack_forget()

    def _choose_dir(self):
        d = filedialog.askdirectory(title="Куда сохранять результат")
        if d:
            self.out_dir = str(Path(d))
            self._update_dest()

    def _reset_dir(self):
        self.out_dir = None
        self._update_dest()

    # ---------- форматы ----------
    def _rebuild_chips(self):
        for w in self.card_body.winfo_children():
            w.destroy()
        self.buttons.clear()
        self.sel = None
        c = self.cat
        if c == "audio":
            groups = [(None, "audio", list(core.AUDIO_FORMATS))]
        elif c == "video":
            groups = [("Видео", "video", list(core.VIDEO_FORMATS)),
                      ("Достать только звук", "extract", list(core.AUDIO_FORMATS))]
        elif c == "image":
            groups = [(None, "image", list(core.IMAGE_FORMATS))]
        else:
            groups = []

        if not groups:
            ctk.CTkLabel(self.card_body, text="Добавь файл, чтобы выбрать формат", font=self.F("text", 14),
                         text_color=MUTED).pack(expand=True, pady=24)
            self.info.pack_forget()
        for title, mode, fmts in groups:
            if title:
                ctk.CTkLabel(self.card_body, text=title, anchor="w", font=self.F("bold", 12),
                             text_color=ACCENT_SOFT).pack(fill="x", padx=8, pady=(8, 2))
            grid = ctk.CTkFrame(self.card_body, fg_color="transparent")
            grid.pack(fill="x")
            for i in range(len(fmts)):
                grid.grid_columnconfigure(i, weight=1, uniform=f"chip_{mode}")
            for i, f in enumerate(fmts):
                b = ctk.CTkButton(grid, text=f, height=38, width=40, corner_radius=11, border_width=1,
                                  font=self.F("bold", 13),
                                  command=lambda m=mode, x=f: self._toggle(m, x))
                b.grid(row=0, column=i, padx=3, pady=3, sticky="ew")
                self._style(b, False)
                self.buttons[(mode, f)] = b
        self._refresh()

    @staticmethod
    def _style(b, on):
        if on:
            b.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="white", border_color=ACCENT)
        else:
            b.configure(fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, border_color=CHIP_BORDER)

    def _toggle(self, mode, fmt):
        if self.busy:
            return
        self.sel = None if self.sel == (mode, fmt) else (mode, fmt)
        for key, b in self.buttons.items():
            self._style(b, key == self.sel)
        self._refresh()

    def _on_stems(self):
        on = bool(self.stems_var.get())
        self.stem_menu.configure(state="normal" if on else "disabled")
        if on:  # заранее прогреваем PyTorch/Demucs, пока пользователь выбирает формат
            threading.Thread(target=core.warm_demucs, daemon=True).start()
        self._refresh()

    # ---------- состояние ----------
    def _stems_on(self):
        return bool(self.stems_var.get()) and self.cat in ("audio", "video")

    def _refresh(self):
        for s in (self.sec_stems, self.sec_br, self.sec_q, self.sec_none):
            s.pack_forget()
        if self.cat in ("audio", "video"):
            self.sec_stems.pack(fill="x", pady=(0, 8))
        sel = self.sel
        if sel and sel[0] in ("audio", "extract") and core.AUDIO_FORMATS[sel[1]]["br"]:
            self.sec_br.pack(fill="x")
        elif sel and sel[0] == "image" and sel[1] in ("JPG", "WEBP"):
            self.sec_q.pack(fill="x")
        elif self.cat == "image":
            self.sec_none.pack(fill="x")
        ready = bool(self._active_files()) and (self.sel is not None or self._stems_on())
        if self.busy:
            self.btn_go.configure(text="Отмена", state="normal", fg_color=CHIP, hover_color=CHIP_HOVER,
                                  text_color=TEXT, border_width=1, border_color=CHIP_BORDER)
        elif ready:
            self.btn_go.configure(text=self.go_text, state="normal", fg_color=ACCENT,
                                  hover_color=ACCENT_HOVER, text_color="white", border_width=0)
        else:
            self.btn_go.configure(text=self.go_text, state="disabled", fg_color=DIS_BG,
                                  text_color_disabled=DIS_TX, border_width=0)

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text, text_color=color)

    # ---------- конвертация ----------
    def _go_clicked(self):
        if self.busy:
            self.cancel.set()
        else:
            self._start()

    def _start(self):
        files = self._active_files()
        stems = self._stems_on()
        if self.busy or not files or (self.sel is None and not stems):
            return
        self.busy = True
        self.finished = False
        self.cancel.clear()
        self.outputs = []
        self.btn_open.grid_remove()
        self.progress.set(0)
        self._set_status("Начинаю…")
        self._refresh()
        opts = dict(bitrate=self.br_var.get(), quality=int(self.q_slider.get()),
                    stem_fmt=self.stem_var.get(), out_dir=self.out_dir)
        threading.Thread(target=self._worker, args=(files, self.sel, stems, opts, self.cat), daemon=True).start()

    def _worker(self, files, sel, stems, opts, cat):
        n = len(files)
        w_main = 1.0 if sel else 0.0
        w_stem = (6.0 if sel else 1.0) if stems else 0.0
        total = w_main + w_stem
        results, errors, failed = [], [], set()
        cancelled = False

        def emit(frac, txt):
            self.ui_q.put(("progress", frac, txt))

        # 1) основная конвертация: несколько файлов одновременно
        if sel:
            prog = [0.0] * n
            done = [0]

            def run_one(i, f):
                def cb(frac, txt):
                    prog[i] = frac
                    head = f"Готово {done[0]} из {n}\n" if n > 1 else ""
                    emit(w_main / total * sum(prog) / n, head + txt)
                return core.convert(f, sel[0], sel[1], opts, cb, self.cancel)

            with ThreadPoolExecutor(max_workers=max(1, min(n, core.workers_for(cat)))) as ex:
                futs = {ex.submit(run_one, i, f): (i, f) for i, f in enumerate(files)}
                for fut in as_completed(futs):
                    i, f = futs[fut]
                    try:
                        results += fut.result()
                        prog[i] = 1.0
                        done[0] += 1
                    except core.Cancelled:
                        cancelled = True
                    except Exception as e:  # noqa: BLE001
                        failed.add(i)
                        errors.append(f"{Path(f).name}: {e}")

        # 2) акапелла: нейросеть тяжёлая, поэтому по одному файлу
        if stems and not cancelled:
            todo = [f for i, f in enumerate(files) if i not in failed]
            for k, f in enumerate(todo):
                head = f"[{k + 1}/{len(todo)}] {_short(Path(f).name, 38)}\n" if len(todo) > 1 else ""

                def cb(frac, txt, k=k, head=head):
                    emit((w_main + w_stem * (k + frac) / len(todo)) / total, head + txt)
                try:
                    results += core.convert(f, "split", "", opts, cb, self.cancel)
                except core.Cancelled:
                    cancelled = True
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{Path(f).name}: {e}")
        self.ui_q.put(("done", results, errors, cancelled))

    def _poll(self):
        try:
            while True:
                paths = self.inbox.get_nowait()
                if self.hidden:
                    self._reopen(paths)
                else:
                    self.add_files(paths)
        except queue.Empty:
            pass
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if msg[0] == "progress":
                    self.progress.set(min(max(msg[1], 0), 1))
                    self._set_status(msg[2])
                elif msg[0] == "info":
                    self._apply_info(*msg[1:])
                else:
                    self._finish(*msg[1:])
        except queue.Empty:
            pass
        if self.hidden and time.time() - self.hidden_at > KEEP_ALIVE_MIN * 60:
            self.destroy()
            return
        self.after(120, self._poll)

    def _finish(self, results, errors, cancelled):
        self.busy = False
        self.outputs = results
        self.go_text = "Конвертировать ещё"
        if cancelled:
            self._set_status("Отменено", WARN)
            self.progress.set(0)
        elif errors:
            self._set_status("Ошибка:\n" + "\n".join(errors)[:300], ERR)
        else:
            self.progress.set(1)
            self._set_status(f"Готово ✓   Сохранено {len(results)} {_plural(len(results))}", OK)
        self.finished = bool(results) and not errors and not cancelled
        if results:
            self.btn_open.grid()
        self._refresh()

    def _open_folder(self):
        if self.outputs:
            subprocess.Popen(["explorer", f"/select,{self.outputs[0]}"])

    # ---------- закрытие / быстрый повторный запуск ----------
    def _on_close(self):
        self.cancel.set()
        if self.standby:
            self.hidden = True
            self.hidden_at = time.time()
            self.withdraw()
        else:
            self.destroy()

    def _reopen(self, paths):
        if self.busy:  # дожидаемся остановки прошлой задачи
            self.after(200, self._reopen, paths)
            return
        self.hidden = False
        self.files, self.outputs, self.finished = [], [], False
        self.cat = None
        self.go_text = "Конвертировать"
        self.stems_var.set(False)
        self.stem_menu.configure(state="disabled")
        self.progress.set(0)
        self._set_status("Готов к работе")
        self.btn_open.grid_remove()
        self.add_files(paths)
        self._rebuild_chips() if not self.files else None
        self._fit_height(center=True)
        self.deiconify()
        self.lift()
        self._pop_to_front()
        self.focus_force()
