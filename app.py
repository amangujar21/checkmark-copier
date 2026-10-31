"""Checkmark Copier — a local Windows GUI for copying Fill & Sign checks."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

import fitz
from PIL import Image, ImageGrab, ImageTk

import checkmark_core as core

APP_NAME = "Checkmark Copier"
APP_VERSION = "1.0.0"

# Palette -------------------------------------------------------------------
BG = "#F3F5F9"
CARD = "#FFFFFF"
BORDER = "#E3E8EF"
TEXT = "#0F172A"
MUTED = "#667085"
FAINT = "#98A2B3"
ACCENT = "#2563EB"
ACCENT_DARK = "#1D4ED8"
ACCENT_SOFT = "#EFF6FF"
NAVY = "#0B1220"
NAVY_SOFT = "#172033"
AMBER = "#D97706"
AMBER_SOFT = "#FFFBEB"
GREEN = "#15803D"
GREEN_SOFT = "#F0FDF4"
RED = "#DC2626"
RED_SOFT = "#FEF2F2"

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"

SWATCHES = [
    ("Black", (0.0, 0.0, 0.0)),
    ("Adobe blue", core.ADOBE_BLUE),
    ("Red", core.ADOBE_RED),
    ("Green", (0.09, 0.50, 0.24)),
    ("Purple", (0.49, 0.23, 0.93)),
    ("Slate", (0.28, 0.33, 0.41)),
]


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def setup_logging() -> logging.Logger:
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CheckmarkCopier")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, "app.log"), maxBytes=1_500_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    except Exception:
        logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("checkmark-copier")
    logger.info("Starting %s %s", APP_NAME, APP_VERSION)
    return logger


LOG = setup_logging()


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str, delay: int = 500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self._tip = tk.Toplevel(self.widget)
        self._tip.overrideredirect(True)
        self._tip.configure(bg="#111827")
        tk.Label(
            self._tip,
            text=self.text,
            bg="#111827",
            fg="#F9FAFB",
            padx=8,
            pady=4,
            font=(FONT, 8),
        ).pack()
        self._tip.geometry(f"+{x}+{y}")

    def _hide(self, _event=None):
        self._cancel()
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class ColourSwatch(tk.Canvas):
    def __init__(self, master, colour: tuple[float, float, float], command, size: int = 24):
        super().__init__(master, width=size, height=size, bg=CARD, highlightthickness=0, cursor="hand2")
        self.colour = colour
        self.command = command
        self.size = size
        self.selected = False
        self._draw()
        self.bind("<Button-1>", lambda _e: self.command(self.colour))

    def _draw(self):
        self.delete("all")
        s = self.size
        self.create_oval(1, 1, s - 1, s - 1, fill=core.colour_to_hex(self.colour), outline="")
        if self.selected:
            self.create_oval(0, 0, s - 1, s - 1, outline=ACCENT, width=2)
        else:
            self.create_oval(0.5, 0.5, s - 1.5, s - 1.5, outline="#D0D5DD", width=1)

    def set_selected(self, value: bool):
        if self.selected != value:
            self.selected = value
            self._draw()


class ScrollFrame(tk.Frame):
    """A vertically scrolling sidebar that still feels native."""

    def __init__(self, master, width: int = 360, **kwargs):
        super().__init__(master, bg=BG, **kwargs)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, width=width)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._inside = False
        self.canvas.bind("<Enter>", lambda _e: setattr(self, "_inside", True))
        self.canvas.bind("<Leave>", lambda _e: setattr(self, "_inside", False))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_inner_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_scrollbar()

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._update_scrollbar()

    def _update_scrollbar(self):
        try:
            self.canvas.update_idletasks()
            needed = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
            mapped = bool(self.scrollbar.winfo_ismapped())
            if needed and not mapped:
                self.scrollbar.pack(side="right", fill="y")
            elif not needed and mapped:
                self.scrollbar.pack_forget()
        except Exception:
            pass

    def _on_wheel(self, event):
        if not self._inside:
            return
        if self.inner.winfo_height() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"


class CheckmarkCopierApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.doc: fitz.Document | None = None
        self.pdf_path: str | None = None
        self.password: str | None = None
        self.page_index = 0
        self.marks: list[core.CheckMark] = []
        self.selected: int | None = None
        self.target_pages: list[int] = []
        self.result_pos = 0
        self.preview_mode = "source"
        self.zoom = 1.0
        self.zoom_mode = "fit"
        self.show_overlay = tk.BooleanVar(value=True)
        self._photo: ImageTk.PhotoImage | None = None
        self._page_image: Image.Image | None = None
        self._origin = (0, 0)
        self._overlay_boxes: list[tuple[int, tuple[float, float, float, float]]] = []
        self._render_job: str | None = None
        self._resize_job: str | None = None
        self._size_job: str | None = None
        self._suppress = False
        self._drag: dict | None = None
        self._undo: list[list[core.CheckMark]] = []
        self._last_auto_target = ""
        self._save_queue: queue.Queue | None = None
        self._busy: tk.Toplevel | None = None
        self._last_output: str | None = None
        self._logo_photo: ImageTk.PhotoImage | None = None
        self._place_mode = False
        self.current_colour: tuple[float, float, float] = core.BLACK

        self._build_window()
        self._build_header()
        self._build_body()
        self._bind_keys()
        self._select_swatch(self.current_colour)
        self._show_empty_state()

    # ------------------------------------------------------------------ UI
    def _build_window(self):
        self.root.title(APP_NAME)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = max(1000, min(1360, screen_w - 50))
        height = max(650, min(880, screen_h - 90))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1100, width), min(700, height))
        self.root.configure(bg=BG)
        try:
            self.root.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass
        try:
            icon = Image.open(resource_path(os.path.join("assets", "app_256.png")))
            self._window_icon = ImageTk.PhotoImage(icon)
            self.root.iconphoto(True, self._window_icon)
        except Exception:
            pass
        self._configure_styles()

    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=(FONT, 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=(FONT, 10))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=(FONT_SEMI, 10))
        style.configure("TButton", font=(FONT, 9), padding=(12, 7), borderwidth=1)
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
            padding=(16, 9),
            font=(FONT_SEMI, 9),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#93B4F5")],
            foreground=[("disabled", "#FFFFFF")],
        )
        style.configure(
            "Secondary.TButton",
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(12, 7),
            font=(FONT, 9),
        )
        style.map("Secondary.TButton", background=[("active", "#F8FAFC")])
        style.configure(
            "Ghost.TButton",
            background=CARD,
            foreground=MUTED,
            borderwidth=0,
            padding=(6, 4),
            font=(FONT, 9),
        )
        style.map("Ghost.TButton", background=[("active", "#F2F5FA")], foreground=[("active", TEXT)])
        style.configure("Tiny.TButton", font=(FONT, 8), padding=(5, 2), borderwidth=0)
        style.map("Tiny.TButton", background=[("active", "#E8EEF7")])
        style.configure(
            "TEntry",
            fieldbackground="#FFFFFF",
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(7, 6),
        )
        style.configure(
            "TSpinbox",
            fieldbackground="#FFFFFF",
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowsize=12,
            padding=(6, 5),
        )
        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            rowheight=25,
            borderwidth=0,
            font=(FONT, 9),
        )
        style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", TEXT)])
        style.configure(
            "Treeview.Heading",
            background="#F8FAFC",
            foreground=MUTED,
            relief="flat",
            font=(FONT_SEMI, 8),
            padding=(5, 4),
        )
        style.map("Treeview.Heading", background=[("active", "#F1F5F9")])
        style.configure("TCheckbutton", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Horizontal.TProgressbar", troughcolor="#E5EAF2", background=ACCENT)

    def _build_header(self):
        header = tk.Frame(self.root, bg=NAVY, height=80)
        header.pack(fill="x")
        header.pack_propagate(False)

        left = tk.Frame(header, bg=NAVY)
        left.pack(side="left", fill="y", padx=(20, 0))
        try:
            logo = Image.open(resource_path(os.path.join("assets", "app_256.png"))).resize(
                (42, 42), Image.Resampling.LANCZOS
            )
            self._logo_photo = ImageTk.PhotoImage(logo)
            tk.Label(left, image=self._logo_photo, bg=NAVY).pack(side="left", pady=18)
        except Exception:
            pass
        title_box = tk.Frame(left, bg=NAVY)
        title_box.pack(side="left", padx=(12, 0), pady=(16, 0))
        tk.Label(
            title_box,
            text=APP_NAME,
            bg=NAVY,
            fg="#FFFFFF",
            font=(FONT_SEMI, 17),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="Copy Fill & Sign checks to any page range — entirely on your PC.",
            bg=NAVY,
            fg="#94A3B8",
            font=(FONT, 9),
        ).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(header, bg=NAVY)
        right.pack(side="right", fill="y", padx=(0, 20))
        self.open_btn = ttk.Button(
            right, text="Open PDF", style="Accent.TButton", command=self.open_pdf_dialog, cursor="hand2"
        )
        self.open_btn.pack(side="right", pady=22)
        help_btn = tk.Button(
            right,
            text="?",
            bg=NAVY,
            fg="#CBD5E1",
            activebackground=NAVY_SOFT,
            activeforeground="#FFFFFF",
            bd=0,
            highlightthickness=0,
            font=(FONT_SEMI, 12),
            width=3,
            cursor="hand2",
            command=self.show_help,
        )
        help_btn.pack(side="right", padx=(0, 10), pady=22)
        Tooltip(help_btn, "How it works")

        accent_line = tk.Frame(self.root, bg=ACCENT, height=2)
        accent_line.pack(fill="x")

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, minsize=365)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        side_col = tk.Frame(body, bg=BG)
        side_col.grid(row=0, column=0, sticky="nsew", padx=(18, 8), pady=16)
        side_col.grid_rowconfigure(0, weight=1)
        side_col.grid_columnconfigure(0, weight=1)
        self.sidebar = ScrollFrame(side_col, width=365)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.action_side = tk.Frame(side_col, bg=BG)
        self.action_side.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._build_sidebar(self.sidebar.inner, self.action_side)

        self.preview_card = tk.Frame(body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        self.preview_card.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=16)
        self.preview_card.grid_columnconfigure(0, weight=1)
        self.preview_card.grid_rowconfigure(1, weight=1)
        self._build_preview(self.preview_card)

    # ---------------------------------------------------------------- sidebar
    def _card(self, parent, number: str, title: str) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, 10))
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(
            head,
            text=number,
            bg=ACCENT_SOFT,
            fg=ACCENT,
            font=(FONT_SEMI, 8),
            padx=6,
            pady=1,
        ).pack(side="left")
        tk.Label(head, text=title.upper(), bg=CARD, fg=MUTED, font=(FONT_SEMI, 8)).pack(
            side="left", padx=(8, 0)
        )
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=14, pady=(0, 13))
        return body

    def _build_sidebar(self, parent, action_parent):
        # Document card
        doc_card = self._card(parent, "01", "PDF document")
        self.file_name_label = tk.Label(
            doc_card,
            text="No PDF open",
            bg=CARD,
            fg=TEXT,
            font=(FONT_SEMI, 10),
            anchor="w",
            justify="left",
            wraplength=300,
        )
        self.file_name_label.pack(fill="x")
        self.file_meta_label = tk.Label(
            doc_card, text="Open a PDF to begin.", bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w"
        )
        self.file_meta_label.pack(fill="x", pady=(3, 1))

        # Source card
        src_card = self._card(parent, "02", "Source page")
        page_row = tk.Frame(src_card, bg=CARD)
        page_row.pack(fill="x")
        tk.Label(page_row, text="Page", bg=CARD, fg=TEXT, font=(FONT, 9)).pack(side="left")
        self.page_var = tk.StringVar(value="1")
        self.page_spin = ttk.Spinbox(
            page_row,
            from_=1,
            to=1,
            width=5,
            textvariable=self.page_var,
            command=self._on_source_page_change,
            justify="center",
        )
        self.page_spin.pack(side="left", padx=(8, 4))
        self.page_total_label = tk.Label(page_row, text="of 1", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.page_total_label.pack(side="left")
        self.rescan_btn = ttk.Button(
            page_row, text="Rescan", style="Ghost.TButton", command=self.scan_current_page, cursor="hand2"
        )
        self.rescan_btn.pack(side="right")
        self.page_spin.bind("<Return>", lambda _e: self._on_source_page_change())
        self.page_spin.bind("<FocusOut>", lambda _e: self._on_source_page_change())

        self.scan_status = tk.Label(
            src_card,
            text="Waiting for a PDF.",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
            anchor="w",
            justify="left",
            wraplength=310,
        )
        self.scan_status.pack(fill="x", pady=(8, 0))

        # Marks card
        marks_card = self._card(parent, "03", "Check marks")
        tree_wrap = tk.Frame(marks_card, bg=BG, highlightbackground=BORDER, highlightthickness=1)
        tree_wrap.pack(fill="x")
        columns = ("on", "num", "pos", "size", "source")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings", height=4, selectmode="browse")
        headings = {
            "on": ("", 24, "center"),
            "num": ("#", 24, "center"),
            "pos": ("X, Y", 78, "w"),
            "size": ("Size", 42, "center"),
            "source": ("Found as", 105, "w"),
        }
        for col, (text, width, anchor) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=width, anchor=anchor, stretch=False)
        self.tree.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure("off", foreground="#A0A8B5")
        self.tree.tag_configure("image", foreground="#1D4ED8")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double)

        info_row = tk.Frame(marks_card, bg=CARD)
        info_row.pack(fill="x", pady=(6, 6))
        self.marks_info = tk.Label(
            info_row, text="No marks yet", bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w"
        )
        self.marks_info.pack(side="left")

        actions = tk.Frame(marks_card, bg=CARD)
        actions.pack(fill="x")
        self.place_btn = tk.Button(
            actions,
            text="Place checks",
            bg="#F8FAFC",
            fg=TEXT,
            activebackground="#EEF2F7",
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=9,
            pady=5,
            font=(FONT, 8),
            cursor="hand2",
            command=self.toggle_place_mode,
        )
        self.place_btn.pack(side="left")
        self.remove_btn = ttk.Button(
            actions, text="Remove", style="Ghost.TButton", command=self.remove_selected, cursor="hand2"
        )
        self.remove_btn.pack(side="left", padx=(5, 0))
        self.clear_btn = ttk.Button(
            actions, text="Clear", style="Ghost.TButton", command=self.clear_marks, cursor="hand2"
        )
        self.clear_btn.pack(side="right")

        controls = tk.Frame(marks_card, bg=CARD)
        controls.pack(fill="x", pady=(8, 0))
        tk.Label(controls, text="Colour", bg=CARD, fg=MUTED, font=(FONT, 8)).pack(side="left")
        swatch_row = tk.Frame(controls, bg=CARD)
        swatch_row.pack(side="left", padx=(5, 0))
        self.swatches: list[ColourSwatch] = []
        for name, colour in SWATCHES:
            sw = ColourSwatch(swatch_row, colour, self._choose_colour, size=21)
            sw.pack(side="left", padx=(0, 4))
            self.swatches.append(sw)
            Tooltip(sw, name)
        custom = tk.Button(
            swatch_row,
            text="+",
            bg=CARD,
            fg=MUTED,
            activebackground="#F2F5FA",
            relief="flat",
            bd=0,
            font=(FONT_SEMI, 9),
            width=1,
            cursor="hand2",
            command=self._choose_custom_colour,
        )
        custom.pack(side="left")
        Tooltip(custom, "Custom colour")

        tk.Label(controls, text="Size", bg=CARD, fg=MUTED, font=(FONT, 8)).pack(side="left", padx=(7, 3))
        self.size_var = tk.IntVar(value=12)
        self.size_spin = ttk.Spinbox(
            controls,
            from_=4,
            to=72,
            width=3,
            textvariable=self.size_var,
            command=self._schedule_size_change,
            justify="center",
        )
        self.size_spin.pack(side="left")
        tk.Label(controls, text="pt", bg=CARD, fg=MUTED, font=(FONT, 8)).pack(side="left", padx=(2, 0))
        self.size_spin.bind("<Return>", lambda _e: self._apply_size_now())
        self.size_spin.bind("<FocusOut>", lambda _e: self._apply_size_now())
        Tooltip(self.size_spin, "Size of new or selected vector marks")

        tk.Label(
            marks_card,
            text="Double-click to enable/disable · the source is never edited.",
            bg=CARD,
            fg=FAINT,
            font=(FONT, 7),
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

        # Target + save live in a fixed footer so the primary action is always visible.
        action_card = self._card(action_parent, "04", "Copy & save")
        self.target_var = tk.StringVar(value="")
        entry = ttk.Entry(action_card, textvariable=self.target_var)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _e: self._targets_changed())
        entry.bind("<FocusOut>", lambda _e: self._targets_changed())
        self.target_entry = entry

        status_row = tk.Frame(action_card, bg=CARD)
        status_row.pack(fill="x", pady=(3, 0))
        self.target_count_label = tk.Label(
            status_row,
            text="Enter 2-10, 12",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8),
            anchor="w",
        )
        self.target_count_label.pack(side="left", fill="x", expand=True)
        ttk.Button(
            status_row, text="After source", style="Ghost.TButton", command=lambda: self._quick_range("after"), cursor="hand2"
        ).pack(side="right")
        ttk.Button(
            status_row, text="All", style="Ghost.TButton", command=lambda: self._quick_range("all"), cursor="hand2"
        ).pack(side="right", padx=(2, 0))

        tk.Frame(action_card, bg=BORDER, height=1).pack(fill="x", pady=(6, 6))
        path_row = tk.Frame(action_card, bg=CARD)
        path_row.pack(fill="x")
        tk.Label(path_row, text="Save to", bg=CARD, fg=FAINT, font=(FONT, 7)).pack(side="left", anchor="n")
        self.output_label = tk.Label(
            path_row,
            text="Open a PDF first",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8),
            anchor="w",
            justify="left",
            wraplength=215,
        )
        self.output_label.pack(side="left", fill="x", expand=True, padx=(6, 4))
        ttk.Button(
            path_row, text="Change", style="Ghost.TButton", command=self.choose_output, cursor="hand2"
        ).pack(side="right", anchor="n")

        self.save_btn = ttk.Button(
            action_card,
            text="Save a copy",
            style="Accent.TButton",
            command=self.save_copy,
            cursor="hand2",
            state="disabled",
        )
        self.save_btn.pack(fill="x", pady=(8, 5))
        bottom = tk.Frame(action_card, bg=CARD)
        bottom.pack(fill="x")
        self.open_output_btn = tk.Button(
            bottom,
            text="Open output",
            bg=CARD,
            fg=ACCENT,
            disabledforeground=FAINT,
            activebackground=CARD,
            activeforeground=ACCENT_DARK,
            relief="flat",
            bd=0,
            font=(FONT, 8),
            cursor="hand2",
            state="disabled",
            command=self.open_last_output,
        )
        self.open_output_btn.pack(side="left")
        tk.Label(
            bottom,
            text="Original unchanged  ·  local only",
            bg=CARD,
            fg=FAINT,
            font=(FONT, 7),
            anchor="e",
        ).pack(side="right")

    # --------------------------------------------------------------- preview
    def _build_preview(self, parent):
        toolbar = tk.Frame(parent, bg=CARD, height=56)
        toolbar.grid(row=0, column=0, columnspan=3, sticky="ew")
        toolbar.pack_propagate(False)
        tk.Frame(toolbar, bg=BORDER, height=1).pack(side="bottom", fill="x")

        seg = tk.Frame(toolbar, bg="#F1F5F9")
        seg.pack(side="left", padx=(14, 0), pady=12)
        self.source_seg = tk.Button(
            seg,
            text="Source",
            bg=ACCENT,
            fg="#FFFFFF",
            activebackground=ACCENT,
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=12,
            pady=4,
            font=(FONT_SEMI, 8),
            cursor="hand2",
            command=lambda: self.set_preview_mode("source"),
        )
        self.source_seg.pack(side="left")
        self.result_seg = tk.Button(
            seg,
            text="Result",
            bg="#F1F5F9",
            fg=MUTED,
            activebackground="#E2E8F0",
            activeforeground=TEXT,
            relief="flat",
            bd=0,
            padx=12,
            pady=4,
            font=(FONT_SEMI, 8),
            cursor="hand2",
            command=lambda: self.set_preview_mode("result"),
        )
        self.result_seg.pack(side="left")

        nav = tk.Frame(toolbar, bg=CARD)
        nav.pack(side="left", padx=(18, 0), pady=12)
        self.prev_btn = tk.Button(
            nav,
            text="‹",
            bg=CARD,
            fg=MUTED,
            activebackground="#F2F5FA",
            relief="flat",
            bd=0,
            font=(FONT, 15),
            width=2,
            cursor="hand2",
            command=lambda: self.step_page(-1),
        )
        self.prev_btn.pack(side="left")
        self.nav_label = tk.Label(
            nav, text="Page 1 of 1", bg=CARD, fg=TEXT, font=(FONT_SEMI, 8), width=22
        )
        self.nav_label.pack(side="left", padx=2)
        self.next_btn = tk.Button(
            nav,
            text="›",
            bg=CARD,
            fg=MUTED,
            activebackground="#F2F5FA",
            relief="flat",
            bd=0,
            font=(FONT, 15),
            width=2,
            cursor="hand2",
            command=lambda: self.step_page(1),
        )
        self.next_btn.pack(side="left")

        right = tk.Frame(toolbar, bg=CARD)
        right.pack(side="right", padx=(0, 12), pady=12)
        self.overlay_check = tk.Checkbutton(
            right,
            text="Boxes",
            variable=self.show_overlay,
            command=self._draw_overlays,
            indicatoron=False,
            bg=CARD,
            fg=MUTED,
            activebackground="#F2F5FA",
            activeforeground=TEXT,
            selectcolor=ACCENT_SOFT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=3,
            font=(FONT, 8),
            cursor="hand2",
        )
        self.overlay_check.pack(side="right", padx=(10, 0))
        self.zoom_label = tk.Label(right, text="100%", bg=CARD, fg=MUTED, font=(FONT, 8), width=5)
        self.zoom_label.pack(side="right", padx=(4, 0))
        self.zoom_in_btn = tk.Button(
            right,
            text="+",
            bg=CARD,
            fg=MUTED,
            activebackground="#F2F5FA",
            relief="flat",
            bd=0,
            font=(FONT, 12),
            width=2,
            cursor="hand2",
            command=lambda: self.zoom_by(1.2),
        )
        self.zoom_in_btn.pack(side="right")
        self.fit_btn = ttk.Button(
            right, text="Fit", style="Ghost.TButton", command=self.fit_page, cursor="hand2"
        )
        self.fit_btn.pack(side="right", padx=(2, 2))
        self.zoom_out_btn = tk.Button(
            right,
            text="−",
            bg=CARD,
            fg=MUTED,
            activebackground="#F2F5FA",
            relief="flat",
            bd=0,
            font=(FONT, 12),
            width=2,
            cursor="hand2",
            command=lambda: self.zoom_by(1 / 1.2),
        )
        self.zoom_out_btn.pack(side="right")

        canvas_wrap = tk.Frame(parent, bg="#E8EDF3")
        canvas_wrap.grid(row=1, column=0, columnspan=3, sticky="nsew")
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        canvas_wrap.grid_rowconfigure(0, weight=1)
        canvas_wrap.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            canvas_wrap,
            bg="#E8EDF3",
            highlightthickness=0,
            xscrollincrement=20,
            yscrollincrement=20,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.hbar = ttk.Scrollbar(canvas_wrap, orient="horizontal", command=self.canvas.xview)
        self.hbar.grid(row=1, column=0, sticky="ew")
        self.vbar = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.canvas.yview)
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)
        self.hbar.grid_remove()
        self.vbar.grid_remove()

        self.canvas.bind("<Button-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<MouseWheel>", self._on_canvas_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_canvas_shift_wheel)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Motion>", self._on_canvas_motion)

        footer = tk.Frame(parent, bg=CARD, height=30)
        footer.grid(row=2, column=0, columnspan=3, sticky="ew")
        footer.pack_propagate(False)
        tk.Frame(footer, bg=BORDER, height=1).pack(side="top", fill="x")
        self.footer_left = tk.Label(
            footer, text="No PDF open", bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w"
        )
        self.footer_left.pack(side="left", padx=14)
        self.footer_right = tk.Label(
            footer, text="", bg=CARD, fg=FAINT, font=(FONT, 8), anchor="e"
        )
        self.footer_right.pack(side="right", padx=14)

        # Empty state overlay
        self.empty_frame = tk.Frame(self.canvas, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        tk.Label(
            self.empty_frame,
            text="Open a PDF to begin",
            bg=CARD,
            fg=TEXT,
            font=(FONT_SEMI, 14),
        ).pack(padx=38, pady=(28, 6))
        tk.Label(
            self.empty_frame,
            text="Pick the page with your check marks, choose the pages to copy to,\nand save a new PDF.",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
            justify="center",
        ).pack(padx=30)
        ttk.Button(
            self.empty_frame,
            text="Choose a PDF",
            style="Accent.TButton",
            command=self.open_pdf_dialog,
            cursor="hand2",
        ).pack(pady=(16, 26))

    # ------------------------------------------------------------- key/mouse
    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda _e: self.open_pdf_dialog())
        self.root.bind("<Control-s>", lambda _e: self.save_copy())
        self.root.bind("<Control-z>", lambda _e: self.undo())
        self.root.bind("<Left>", self._key_page)
        self.root.bind("<Right>", self._key_page)
        self.root.bind("<Delete>", self._key_delete)
        self.root.bind("<Escape>", self._key_escape)
        self.root.bind("<Control-plus>", lambda _e: self.zoom_by(1.2))
        self.root.bind("<Control-minus>", lambda _e: self.zoom_by(1 / 1.2))
        self.root.bind("<Control-0>", lambda _e: self.fit_page())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _typing(self) -> bool:
        widget = self.root.focus_get()
        return isinstance(widget, (tk.Entry, ttk.Entry, ttk.Spinbox, tk.Text))

    def _key_page(self, event):
        if self._typing():
            return
        self.step_page(-1 if event.keysym == "Left" else 1)

    def _key_delete(self, _event):
        if self._typing():
            return
        self.remove_selected()

    def _key_escape(self, _event):
        if self._place_mode:
            self.toggle_place_mode(False)
        elif self._drag:
            self._drag = None
        else:
            self.canvas.focus_set()

    # --------------------------------------------------------------- lifecycle
    def _show_empty_state(self):
        self.empty_frame.place(relx=0.5, rely=0.5, anchor="center")
        if hasattr(self, "hbar"):
            self.hbar.grid_remove()
            self.vbar.grid_remove()
        self.footer_left.configure(text="No PDF open")
        self.footer_right.configure(text="")
        self.nav_label.configure(text="No document")
        self._update_nav_buttons()

    def _hide_empty_state(self):
        self.empty_frame.place_forget()

    def open_pdf_dialog(self):
        path = filedialog.askopenfilename(
            title="Open a PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            parent=self.root,
        )
        if path:
            self.load_pdf(path)

    def load_pdf(self, path: str):
        try:
            doc = fitz.open(path)
        except Exception as exc:
            LOG.exception("open failed: %s", path)
            messagebox.showerror(APP_NAME, f"Could not open this PDF.\n\n{exc}", parent=self.root)
            return

        password = None
        if doc.needs_pass:
            password = simpledialog.askstring(
                "Password required",
                f"“{os.path.basename(path)}” is protected.\nEnter the password:",
                show="*",
                parent=self.root,
            )
            if password is None:
                doc.close()
                return
            if not doc.authenticate(password):
                doc.close()
                messagebox.showerror(APP_NAME, "That password did not work.", parent=self.root)
                return

        if self.doc is not None:
            try:
                self.doc.close()
            except Exception:
                pass

        self.doc = doc
        self.pdf_path = path
        self.password = password
        self.page_index = 0
        self.marks = []
        self.selected = None
        self._undo.clear()
        self._last_output = None
        self.open_output_btn.configure(state="disabled")
        self.save_btn.configure(state="normal")
        self.page_var.set("1")
        self.page_spin.configure(to=max(1, doc.page_count))
        self.page_total_label.configure(text=f"of {doc.page_count}")
        size_mb = os.path.getsize(path) / (1024 * 1024)
        self.file_name_label.configure(text=os.path.basename(path))
        self.file_meta_label.configure(text=f"{doc.page_count} pages  ·  {size_mb:.1f} MB")
        self.root.title(f"{APP_NAME} — {os.path.basename(path)}")
        self.output_var = self._default_output(path)
        self._update_output_label()
        self._hide_empty_state()
        self._set_auto_target()
        self.preview_mode = "source"
        self._update_mode_buttons()
        self.zoom_mode = "fit"
        self.scan_current_page()
        self.canvas.focus_set()
        LOG.info("Opened %s (%d pages)", path, doc.page_count)

    def _on_close(self):
        try:
            if self.doc is not None:
                self.doc.close()
        except Exception:
            pass
        self.root.destroy()

    # --------------------------------------------------------------- scanning
    def _on_source_page_change(self):
        if not self.doc:
            return
        try:
            n = int(self.page_var.get())
        except Exception:
            n = self.page_index + 1
        n = max(1, min(self.doc.page_count, n))
        if n != self.page_index + 1:
            self.page_index = n - 1
            self.page_var.set(str(n))
            if self._auto_target_active():
                self._set_auto_target()
            self.scan_current_page()
        else:
            self.page_var.set(str(n))

    def scan_current_page(self):
        if not self.doc:
            return
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            page = self.doc[self.page_index]
            self.marks = core.detect_checks(page)
            self.selected = None
            self._undo.clear()
            self._rebuild_tree()
            self._targets_changed()
            enabled = sum(1 for m in self.marks if m.enabled)
            if self.marks:
                kinds = {m.source for m in self.marks}
                extra = " · includes annotation appearance" if any(m.kind == "image" for m in self.marks) else ""
                self.scan_status.configure(
                    text=f"●  {len(self.marks)} check mark(s) found  ·  {enabled} enabled{extra}",
                    fg=GREEN,
                )
                LOG.info("Page %d: %d marks (%s)", self.page_index + 1, len(self.marks), ", ".join(sorted(kinds)))
            else:
                self.scan_status.configure(
                    text="No checks found automatically. Use “Place checks” to click them.",
                    fg=AMBER,
                )
                LOG.info("Page %d: no marks", self.page_index + 1)
            self._update_counts()
            self._update_nav()
            self.request_render()
        except Exception as exc:
            LOG.exception("scan failed")
            messagebox.showerror(APP_NAME, f"Could not scan this page.\n\n{exc}", parent=self.root)
        finally:
            self.root.configure(cursor="")

    # --------------------------------------------------------------- mark list
    def _rebuild_tree(self):
        selected = self.selected
        self.tree.delete(*self.tree.get_children())
        for i, m in enumerate(self.marks):
            values = (
                "✓" if m.enabled else "—",
                str(i + 1),
                f"{m.rect.x0:.0f}, {m.rect.y0:.0f}",
                f"{m.size:.1f}",
                m.source.replace(" annotation", "").replace("Form field", "Field"),
            )
            tags = []
            if not m.enabled:
                tags.append("off")
            if m.kind == "image":
                tags.append("image")
            self.tree.insert("", "end", iid=str(i), values=values, tags=tuple(tags))
        if selected is not None and 0 <= selected < len(self.marks):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))
        self._update_mark_info()

    def _update_mark_info(self):
        if not self.marks:
            self.marks_info.configure(text="No marks on this page")
            return
        on = sum(1 for m in self.marks if m.enabled)
        images = sum(1 for m in self.marks if m.kind == "image")
        text = f"{on} of {len(self.marks)} enabled"
        if images:
            text += f"  ·  {images} image"
        self.marks_info.configure(text=text)

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            self.selected = None
        else:
            try:
                self.selected = int(sel[0])
            except Exception:
                self.selected = None
        self._sync_colour_from_selection()
        self._draw_overlays()

    def _on_tree_double(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        idx = int(row)
        if 0 <= idx < len(self.marks):
            self._snapshot()
            self.marks[idx].enabled = not self.marks[idx].enabled
            self._rebuild_tree()
            self._draw_overlays()
            self._update_counts()
            self._update_target_count()

    def _sync_colour_from_selection(self):
        if self.selected is None or not (0 <= self.selected < len(self.marks)):
            return
        mark = self.marks[self.selected]
        if mark.kind == "vector":
            self.current_colour = mark.color
            self._select_swatch(mark.color)

    def remove_selected(self):
        if self.selected is None or not (0 <= self.selected < len(self.marks)):
            return
        self._snapshot()
        del self.marks[self.selected]
        self.selected = None
        self._rebuild_tree()
        self._draw_overlays()
        self._update_counts()
        self._update_target_count()
        self._refresh_result_if_visible()

    def clear_marks(self):
        if not self.marks:
            return
        if not messagebox.askyesno(APP_NAME, "Remove all check marks from this page?", parent=self.root):
            return
        self._snapshot()
        self.marks = []
        self.selected = None
        self._rebuild_tree()
        self._draw_overlays()
        self._update_counts()
        self._update_target_count()
        self._refresh_result_if_visible()
        self.scan_status.configure(text="All marks cleared. Use “Place checks” to add them.", fg=MUTED)

    def _snapshot(self):
        self._undo.append([m.clone() for m in self.marks])
        if len(self._undo) > 30:
            self._undo.pop(0)
        self._refresh_undo()

    def undo(self):
        if not self._undo:
            return
        self.marks = [m.clone() for m in self._undo.pop()]
        self.selected = None
        self._rebuild_tree()
        self._draw_overlays()
        self._update_counts()
        self._update_target_count()
        self._refresh_result_if_visible()

    def _refresh_undo(self):
        # Reserved for an optional visible undo control.
        pass

    # --------------------------------------------------------------- colours
    def _select_swatch(self, colour):
        for sw in self.swatches:
            try:
                same = max(abs(a - b) for a, b in zip(sw.colour, colour)) < 0.01
            except Exception:
                same = False
            sw.set_selected(same)

    def _choose_colour(self, colour):
        if self._suppress:
            return
        self.current_colour = colour
        self._select_swatch(colour)
        if self.selected is not None and 0 <= self.selected < len(self.marks):
            mark = self.marks[self.selected]
            if mark.kind == "vector":
                self._snapshot()
                mark.color = tuple(colour)
                self._draw_overlays()
                self._refresh_result_if_visible()
            else:
                self.scan_status.configure(
                    text="This mark is an image annotation; it keeps its original pixels.", fg=MUTED
                )

    def _choose_custom_colour(self):
        initial = core.colour_to_hex(self.current_colour)
        _rgb, hex_colour = colorchooser.askcolor(color=initial, parent=self.root, title="Check colour")
        if hex_colour:
            self._choose_colour(core.hex_to_colour(hex_colour))

    # ------------------------------------------------------------------ size
    def _schedule_size_change(self):
        if self._suppress:
            return
        if self._size_job:
            try:
                self.root.after_cancel(self._size_job)
            except Exception:
                pass
        self._size_job = self.root.after(220, self._apply_size_now)

    def _apply_size_now(self):
        self._size_job = None
        if self._suppress:
            return
        try:
            target = float(self.size_var.get())
        except Exception:
            return
        target = max(4.0, min(72.0, target))
        if self.selected is None or not (0 <= self.selected < len(self.marks)):
            return
        mark = self.marks[self.selected]
        if mark.kind != "vector" or mark.size <= 0:
            return
        if abs(mark.size - target) < 0.05:
            return
        self._snapshot()
        scale = target / mark.size
        c = mark.center
        mark.points = tuple((c.x + (p[0] - c.x) * scale, c.y + (p[1] - c.y) * scale) for p in mark.points)
        mark.width *= scale
        mark.rect = fitz.Rect(
            min(p[0] for p in mark.points),
            min(p[1] for p in mark.points),
            max(p[0] for p in mark.points),
            max(p[1] for p in mark.points),
        )
        self._rebuild_tree()
        self._draw_overlays()
        self._refresh_result_if_visible()

    # ------------------------------------------------------------- placement
    def toggle_place_mode(self, force: bool | None = None):
        if not self.doc:
            return
        new_state = (not self._place_mode) if force is None else bool(force)
        self._place_mode = new_state
        if new_state:
            if self.preview_mode != "source":
                self.set_preview_mode("source")
            self.place_btn.configure(
                text="Place checks  ·  ON",
                bg=ACCENT,
                fg="#FFFFFF",
                activebackground=ACCENT_DARK,
                activeforeground="#FFFFFF",
                highlightbackground=ACCENT,
            )
            self.canvas.configure(cursor="crosshair")
            self.scan_status.configure(text="Click the page to place a check. Esc to finish.", fg=ACCENT)
        else:
            self.place_btn.configure(
                text="Place checks",
                bg="#F8FAFC",
                fg=TEXT,
                activebackground="#EEF2F7",
                activeforeground=TEXT,
                highlightbackground=BORDER,
            )
            self.canvas.configure(cursor="")
            if self.marks:
                self.scan_status.configure(
                    text=f"●  {len(self.marks)} check mark(s) on this page.", fg=GREEN
                )
            else:
                self.scan_status.configure(text="No check marks yet.", fg=MUTED)

    def _add_mark_at(self, page_point: fitz.Point):
        size = max(4.0, float(self.size_var.get() or 12))
        self._snapshot()
        mark = core.adobe_check_mark(page_point, size=size, color=self.current_colour)
        # Keep the mark on the page if the click is near an edge.
        page = self.doc[self.page_index]
        pr = core.unrotated_page_rect(page)
        if mark.rect.x0 < pr.x0:
            mark.move_by(pr.x0 - mark.rect.x0, 0)
        if mark.rect.y0 < pr.y0:
            mark.move_by(0, pr.y0 - mark.rect.y0)
        if mark.rect.x1 > pr.x1:
            mark.move_by(pr.x1 - mark.rect.x1, 0)
        if mark.rect.y1 > pr.y1:
            mark.move_by(0, pr.y1 - mark.rect.y1)
        self.marks.append(mark)
        self.selected = len(self.marks) - 1
        self._rebuild_tree()
        self._draw_overlays()
        self._update_counts()
        self._update_target_count()
        self._refresh_result_if_visible()
        self.scan_status.configure(text="Placed. Click again, or press Esc to finish.", fg=ACCENT)

    # ------------------------------------------------------------- target/pages
    def _default_output(self, pdf_path: str) -> str:
        folder = os.path.dirname(pdf_path)
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        candidate = os.path.join(folder, f"{stem} - checkmarks.pdf")
        n = 2
        while os.path.exists(candidate):
            candidate = os.path.join(folder, f"{stem} - checkmarks ({n}).pdf")
            n += 1
        return candidate

    def _update_output_label(self):
        if not hasattr(self, "output_var"):
            self.output_label.configure(text="Open a PDF first")
            return
        path = self.output_var
        folder, name = os.path.split(path)
        self.output_label.configure(text=name if name else path)
        self.output_label.bind("<Button-1>", lambda _e: self.choose_output())

    def choose_output(self):
        if not self.pdf_path:
            return
        path = filedialog.asksaveasfilename(
            title="Save a copy",
            defaultextension=".pdf",
            initialfile=os.path.basename(self.output_var) if hasattr(self, "output_var") else "checkmarks.pdf",
            initialdir=os.path.dirname(self.output_var) if hasattr(self, "output_var") else os.path.dirname(self.pdf_path),
            filetypes=[("PDF files", "*.pdf")],
            parent=self.root,
        )
        if path:
            self.output_var = path
            self._update_output_label()

    def open_last_output(self):
        if self._last_output and os.path.exists(self._last_output):
            try:
                os.startfile(self._last_output)  # type: ignore[attr-defined]
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Could not open the output.\n\n{exc}", parent=self.root)

    def _auto_target_active(self) -> bool:
        return self.target_var.get().strip() == self._last_auto_target

    def _set_auto_target(self):
        if not self.doc:
            return
        n = self.doc.page_count
        src = self.page_index + 1
        if n <= 1:
            value = "1"
        elif src >= n:
            value = f"1-{n - 1}"
        else:
            value = f"{src + 1}-end"
        self._last_auto_target = value
        self.target_var.set(value)
        self._targets_changed()

    def _quick_range(self, kind: str):
        if not self.doc:
            return
        n = self.doc.page_count
        src = self.page_index + 1
        if kind == "after":
            if src < n:
                value = f"{src + 1}-end"
            else:
                value = f"1-{max(1, n - 1)}"
        else:
            if src > 1 and src < n:
                value = f"1-{src - 1},{src + 1}-end"
            elif src == 1:
                value = f"2-{n}"
            else:
                value = f"1-{n - 1}"
        self.target_var.set(value)
        self._last_auto_target = ""
        self._targets_changed()

    def _targets_changed(self, show_error: bool = False):
        if not self.doc:
            self.target_pages = []
            return []
        self._update_nav_buttons()
        try:
            pages = core.parse_page_range(self.target_var.get(), self.doc.page_count)
        except Exception as exc:
            self.target_pages = []
            self.target_count_label.configure(
                text="Check the page range",
                fg=RED,
            )
            self._update_nav()
            if show_error:
                messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return []
        source_1 = self.page_index + 1
        pages = [p for p in pages if p != source_1]
        self.target_pages = [p - 1 for p in pages]
        if not self.target_pages:
            self.target_count_label.configure(
                text="Pick other pages", fg=AMBER
            )
        else:
            enabled = sum(1 for m in self.marks if m.enabled)
            total = enabled * len(self.target_pages)
            self.target_count_label.configure(
                text=f"{total} stamps  ·  {len(self.target_pages)} pages",
                fg=GREEN if total else MUTED,
            )
        self.result_pos = min(self.result_pos, max(0, len(self.target_pages) - 1))
        self._update_nav()
        if self.preview_mode == "result":
            self.request_render()
        return self.target_pages

    def _update_counts(self):
        self._update_mark_info()
        self._update_target_count()

    def _update_target_count(self):
        if not self.doc:
            return
        # Refresh the count line without showing a modal for partial input.
        try:
            pages = core.parse_page_range(self.target_var.get(), self.doc.page_count)
            pages = [p for p in pages if p != self.page_index + 1]
            self.target_pages = [p - 1 for p in pages]
            enabled = sum(1 for m in self.marks if m.enabled)
            if self.target_pages:
                self.target_count_label.configure(
                    text=f"{enabled * len(self.target_pages)} stamps  ·  {len(self.target_pages)} pages",
                    fg=GREEN if enabled else MUTED,
                )
            else:
                self.target_count_label.configure(
                    text="Pick other pages", fg=AMBER
                )
        except Exception:
            pass
        self._update_nav_buttons()

    # --------------------------------------------------------------- preview
    def set_preview_mode(self, mode: str):
        if mode == self.preview_mode:
            return
        self.preview_mode = mode
        self._update_mode_buttons()
        self._update_nav()
        self.request_render()

    def _update_mode_buttons(self):
        if self.preview_mode == "source":
            self.source_seg.configure(bg=ACCENT, fg="#FFFFFF", activebackground=ACCENT)
            self.result_seg.configure(bg="#F1F5F9", fg=MUTED, activebackground="#E2E8F0")
        else:
            self.source_seg.configure(bg="#F1F5F9", fg=MUTED, activebackground="#E2E8F0")
            self.result_seg.configure(bg=ACCENT, fg="#FFFFFF", activebackground=ACCENT)
        self.canvas.configure(cursor="crosshair" if self._place_mode else "")

    def _current_page(self):
        if not self.doc:
            return None
        if self.preview_mode == "source":
            return self.doc[self.page_index]
        if not self.target_pages:
            return None
        pos = max(0, min(self.result_pos, len(self.target_pages) - 1))
        self.result_pos = pos
        return self.doc[self.target_pages[pos]]

    def _update_nav(self):
        if not self.doc:
            self.nav_label.configure(text="No document")
            self._update_nav_buttons()
            return
        if self.preview_mode == "source":
            self.nav_label.configure(text=f"Page {self.page_index + 1} of {self.doc.page_count}")
        else:
            if self.target_pages:
                pos = max(0, min(self.result_pos, len(self.target_pages) - 1))
                self.result_pos = pos
                self.nav_label.configure(
                    text=f"Target {pos + 1}/{len(self.target_pages)}  ·  p.{self.target_pages[pos] + 1}"
                )
            else:
                self.nav_label.configure(text="No target pages")
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        if not hasattr(self, "prev_btn"):
            return
        if not self.doc:
            state_prev = state_next = "disabled"
        elif self.preview_mode == "source":
            state_prev = "normal" if self.page_index > 0 else "disabled"
            state_next = "normal" if self.page_index < self.doc.page_count - 1 else "disabled"
        else:
            state_prev = "normal" if self.result_pos > 0 else "disabled"
            state_next = (
                "normal" if self.target_pages and self.result_pos < len(self.target_pages) - 1 else "disabled"
            )
        for btn, state in ((self.prev_btn, state_prev), (self.next_btn, state_next)):
            btn.configure(state=state, fg=MUTED if state == "normal" else "#CBD5E1", cursor="hand2" if state == "normal" else "")

    def step_page(self, delta: int):
        if not self.doc:
            return
        if self.preview_mode == "source":
            new = max(0, min(self.doc.page_count - 1, self.page_index + delta))
            if new != self.page_index:
                self.page_index = new
                self.page_var.set(str(new + 1))
                if self._auto_target_active():
                    self._set_auto_target()
                self.scan_current_page()
        else:
            if not self.target_pages:
                return
            new = max(0, min(len(self.target_pages) - 1, self.result_pos + delta))
            if new != self.result_pos:
                self.result_pos = new
                self._update_nav()
                self.request_render()

    def _on_canvas_configure(self, _event=None):
        if self._resize_job:
            try:
                self.root.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.root.after(120, self.request_render)

    def fit_page(self):
        self.zoom_mode = "fit"
        self.request_render()

    def zoom_by(self, factor: float):
        if not self.doc:
            return
        self.zoom_mode = "manual"
        self.zoom = max(0.15, min(4.0, self.zoom * factor))
        self.request_render()

    def request_render(self):
        if self._render_job:
            try:
                self.root.after_cancel(self._render_job)
            except Exception:
                pass
        self._render_job = self.root.after(10, self.render_preview)

    def _calc_fit_zoom(self, page) -> float:
        cw = max(100, self.canvas.winfo_width() - 56)
        ch = max(100, self.canvas.winfo_height() - 56)
        rect = page.rect
        return max(0.15, min(4.0, min(cw / rect.width, ch / rect.height)))

    def render_preview(self):
        self._render_job = None
        if not self.doc:
            return
        if self.canvas.winfo_width() < 30 or self.canvas.winfo_height() < 30:
            self.request_render()
            return
        page = self._current_page()
        if page is None:
            self._render_empty_result()
            return
        if self.zoom_mode == "fit":
            self.zoom = self._calc_fit_zoom(page)
        zoom = self.zoom
        try:
            if self.preview_mode == "source":
                image = core.render_page(page, zoom)
            else:
                image = core.composite_result_preview(
                    page, self.doc[self.page_index], self.marks, zoom, "proportional"
                )
        except Exception as exc:
            LOG.exception("preview render failed")
            self._render_message(f"Could not render this page.\n{exc}")
            return

        self._page_image = image
        self._photo = ImageTk.PhotoImage(image)
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        iw, ih = image.size
        if iw > cw + 1:
            self.hbar.grid()
        else:
            self.hbar.grid_remove()
        if ih > ch + 1:
            self.vbar.grid()
        else:
            self.vbar.grid_remove()
        if iw <= cw and ih <= ch:
            ox = (cw - iw) / 2
            oy = (ch - ih) / 2
            self._origin = (ox, oy)
            self.canvas.configure(scrollregion=(0, 0, cw, ch))
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
        else:
            ox = oy = 22
            self._origin = (ox, oy)
            self.canvas.configure(scrollregion=(0, 0, iw + 44, ih + 44))
        self._draw_page_canvas()
        self._update_nav()
        self.zoom_label.configure(text=f"{zoom * 100:.0f}%")
        self.footer_left.configure(text=self._footer_text(page))
        self.footer_right.configure(
            text=f"{page.rect.width:.0f} × {page.rect.height:.0f} pt   ·   {'Fit' if self.zoom_mode == 'fit' else 'Custom'}"
        )

    def _draw_page_canvas(self):
        self.canvas.delete("all")
        if not self._photo:
            return
        ox, oy = self._origin
        iw, ih = self._page_image.size if self._page_image else (0, 0)
        # Soft drop shadow / page edge.
        self.canvas.create_rectangle(ox + 5, oy + 6, ox + iw + 5, oy + ih + 6, fill="#D5DCE7", outline="")
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        self.canvas.create_rectangle(ox, oy, ox + iw, oy + ih, outline="#C9D2DF", width=1)
        self._draw_overlays()

    def _footer_text(self, page) -> str:
        if self.preview_mode == "source":
            n = sum(1 for m in self.marks if m.enabled)
            return f"Page {page.number + 1}  ·  {n} of {len(self.marks)} marks enabled  ·  original page"
        return f"Preview of page {page.number + 1}  ·  {sum(1 for m in self.marks if m.enabled)} marks projected"

    def _draw_overlays(self):
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("overlay")
        self.canvas.delete("markshape")
        self._overlay_boxes = []
        if not self.doc or not self._page_image:
            return
        src_page = self.doc[self.page_index]
        page = self._current_page()
        if page is None:
            return
        ox, oy = self._origin
        zoom = self.zoom
        marks = self.marks

        # Show the actual vector strokes on the source preview.  The page
        # bitmap already contains detected marks; drawing them again also makes
        # newly placed or nudged marks visible immediately.
        if self.preview_mode == "source":
            for mark in marks:
                if not mark.enabled or mark.kind != "vector" or len(mark.points) < 2:
                    continue
                pts = []
                for p in mark.points:
                    rp = core.point_to_rotated(page, p)
                    pts.extend((ox + rp.x * zoom, oy + rp.y * zoom))
                self.canvas.create_line(
                    *pts,
                    fill=core.colour_to_hex(mark.color),
                    width=max(1, mark.width * zoom),
                    capstyle=tk.ROUND,
                    joinstyle=tk.MITER,
                    tags=("markshape",),
                )

        if not self.show_overlay.get():
            return
        for idx, mark in enumerate(marks):
            if self.preview_mode == "source":
                rect = core.rect_to_rotated_bbox(page, mark.rect)
            else:
                mapped = core.project_mark(mark, src_page, page, "proportional")
                rect = core.rect_to_rotated_bbox(page, mapped.rect)
            pad = 4
            x0 = ox + rect.x0 * zoom - pad
            y0 = oy + rect.y0 * zoom - pad
            x1 = ox + rect.x1 * zoom + pad
            y1 = oy + rect.y1 * zoom + pad
            if x1 < 0 or y1 < 0 or x0 > self.canvas.winfo_width() or y0 > self.canvas.winfo_height():
                continue
            selected = idx == self.selected
            if not mark.enabled:
                outline = "#94A3B8"
                dash = (4, 3)
                width = 1
            elif selected:
                outline = ACCENT
                dash = ()
                width = 2
            else:
                outline = "#F59E0B"
                dash = ()
                width = 1
            self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=outline, width=width, dash=dash, tags=("overlay",)
            )
            # Only the selected mark gets a number badge; the rest stay clean.
            if selected:
                badge_x = max(ox + 3, x0)
                badge_y = max(oy + 3, y0 - 18)
                self.canvas.create_rectangle(
                    badge_x,
                    badge_y,
                    badge_x + 16,
                    badge_y + 15,
                    fill=outline,
                    outline="",
                    tags=("overlay",),
                )
                self.canvas.create_text(
                    badge_x + 8,
                    badge_y + 7.5,
                    text=str(idx + 1),
                    fill="#FFFFFF",
                    font=(FONT_SEMI, 8),
                    tags=("overlay",),
                )
            self._overlay_boxes.append((idx, (x0 - 6, y0 - 20, x1 + 6, y1 + 6)))

    def _render_empty_result(self):
        self._page_image = None
        self._photo = None
        self.hbar.grid_remove()
        self.vbar.grid_remove()
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas.winfo_width() / 2,
            self.canvas.winfo_height() / 2,
            text="Enter a valid target page range to preview the result.",
            fill=MUTED,
            font=(FONT, 11),
            justify="center",
        )
        self.footer_left.configure(text="No target pages")
        self.footer_right.configure(text="")
        self._update_nav()

    def _render_message(self, text: str):
        self.canvas.delete("all")
        self.canvas.create_text(
            self.canvas.winfo_width() / 2,
            self.canvas.winfo_height() / 2,
            text=text,
            fill=MUTED,
            font=(FONT, 10),
            justify="center",
            width=max(200, self.canvas.winfo_width() - 80),
        )

    # ---------------------------------------------------------- canvas events
    def _canvas_to_unrotated(self, x: float, y: float, page) -> fitz.Point | None:
        rx = (x - self._origin[0]) / self.zoom
        ry = (y - self._origin[1]) / self.zoom
        if rx < 0 or ry < 0 or rx > page.rect.width or ry > page.rect.height:
            return None
        return core.point_to_unrotated(page, (rx, ry))

    def _hit_mark(self, x: float, y: float) -> int | None:
        for idx, (bx0, by0, bx1, by1) in reversed(self._overlay_boxes):
            if bx0 <= x <= bx1 and by0 <= y <= by1:
                return idx
        return None

    def _on_canvas_press(self, event):
        if not self.doc:
            return
        self.canvas.focus_set()
        page = self._current_page()
        if page is None:
            return
        if self._place_mode:
            point = self._canvas_to_unrotated(event.x, event.y, page)
            if point is not None:
                self._add_mark_at(point)
            return
        hit = self._hit_mark(event.x, event.y)
        if hit is None:
            self.selected = None
            if self.tree.selection():
                self.tree.selection_remove(*self.tree.selection())
            self._draw_overlays()
            return
        if self.selected != hit:
            self.selected = hit
            self.tree.selection_set(str(hit))
            self.tree.see(str(hit))
            self._sync_colour_from_selection()
        point = self._canvas_to_unrotated(event.x, event.y, page)
        if point is not None:
            self._drag = {
                "index": hit,
                "start": point,
                "mark": self.marks[hit].clone(),
                "moved": False,
            }
        self._draw_overlays()

    def _on_canvas_drag(self, event):
        if not self._drag or not self.doc:
            return
        page = self._current_page()
        if page is None:
            return
        point = self._canvas_to_unrotated(event.x, event.y, page)
        if point is None:
            return
        dx = point.x - self._drag["start"].x
        dy = point.y - self._drag["start"].y
        if abs(dx) < 0.2 and abs(dy) < 0.2:
            return
        if not self._drag["moved"]:
            self._snapshot()
            self._drag["moved"] = True
        idx = self._drag["index"]
        if not (0 <= idx < len(self.marks)):
            return
        original = self._drag["mark"]
        mark = original.clone()
        mark.move_by(dx, dy)
        self.marks[idx] = mark
        self.selected = idx
        self._draw_overlays()
        self._update_tree_position(idx)

    def _on_canvas_release(self, _event):
        if self._drag and self._drag.get("moved"):
            self._rebuild_tree()
            self._update_target_count()
            self._refresh_result_if_visible()
            self.scan_status.configure(text="Check position updated.", fg=ACCENT)
        self._drag = None

    def _update_tree_position(self, idx: int):
        if not (0 <= idx < len(self.marks)):
            return
        try:
            m = self.marks[idx]
            self.tree.item(
                str(idx),
                values=(
                    "✓" if m.enabled else "—",
                    str(idx + 1),
                    f"{m.rect.x0:.0f}, {m.rect.y0:.0f}",
                    f"{m.size:.1f}",
                    m.source.replace(" annotation", "").replace("Form field", "Field"),
                ),
            )
        except Exception:
            pass

    def _on_canvas_motion(self, event):
        if not self.doc or self._place_mode:
            return
        hit = self._hit_mark(event.x, event.y)
        self.canvas.configure(cursor="hand2" if hit is not None else "")

    def _on_canvas_wheel(self, event):
        if event.state & 0x0004:
            self.zoom_by(1.12 if event.delta > 0 else 1 / 1.12)
            return "break"
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _on_canvas_shift_wheel(self, event):
        self.canvas.xview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _refresh_result_if_visible(self):
        if self.preview_mode == "result":
            self.request_render()

    # ------------------------------------------------------------------- save
    def save_copy(self):
        if not self.doc or not self.pdf_path:
            return
        if not hasattr(self, "output_var") or not self.output_var:
            self.choose_output()
            if not hasattr(self, "output_var") or not self.output_var:
                return
        targets = self._targets_changed()
        enabled = [m for m in self.marks if m.enabled]
        if not enabled:
            messagebox.showwarning(APP_NAME, "There are no enabled check marks to copy.", parent=self.root)
            return
        if not targets:
            messagebox.showwarning(
                APP_NAME,
                "Enter a valid target page range, such as 2-10 or 3, 5, 8.",
                parent=self.root,
            )
            self.target_entry.focus_set()
            return
        if os.path.abspath(self.output_var) == os.path.abspath(self.pdf_path):
            messagebox.showerror(APP_NAME, "Choose a different output file. The original is never modified.", parent=self.root)
            return
        if os.path.exists(self.output_var):
            if not messagebox.askyesno(
                APP_NAME,
                f"“{os.path.basename(self.output_var)}” already exists.\nReplace it?",
                parent=self.root,
            ):
                return
        self._start_save(targets, [m.clone() for m in enabled])

    def _start_save(self, targets: list[int], marks: list[core.CheckMark]):
        self._show_busy("Saving your copy…")
        self.save_btn.configure(state="disabled")
        self._save_queue = queue.Queue()
        q = self._save_queue
        pdf_path = self.pdf_path
        output = self.output_var
        source_index = self.page_index
        password = self.password

        def worker():
            try:
                stats = core.write_copy(
                    pdf_path,
                    output,
                    source_index,
                    targets,
                    marks,
                    mode="proportional",
                    password=password,
                )
                q.put(("ok", stats))
            except Exception as exc:
                LOG.exception("save failed")
                q.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(80, self._poll_save)

    def _poll_save(self):
        if not self._save_queue:
            return
        try:
            kind, payload = self._save_queue.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_save)
            return
        self._close_busy()
        self.save_btn.configure(state="normal")
        if kind == "error":
            messagebox.showerror(
                APP_NAME,
                f"Could not save the copy.\n\n{payload}\n\nIf the output is open in another app, close it and try again.",
                parent=self.root,
            )
            return
        stats: core.CopyStats = payload
        self._last_output = stats.output_path
        self.open_output_btn.configure(state="normal")
        self.show_success(stats)

    def _show_busy(self, text: str):
        self._close_busy()
        top = tk.Toplevel(self.root)
        top.title(APP_NAME)
        top.configure(bg=CARD)
        top.resizable(False, False)
        top.transient(self.root)
        try:
            top.grab_set()
        except Exception:
            pass
        frame = tk.Frame(top, bg=CARD, padx=28, pady=22)
        frame.pack()
        tk.Label(frame, text="Saving a copy", bg=CARD, fg=TEXT, font=(FONT_SEMI, 12)).pack(anchor="w")
        tk.Label(frame, text=text, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(3, 12))
        bar = ttk.Progressbar(frame, mode="indeterminate", length=300)
        bar.pack(fill="x")
        bar.start(12)
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - top.winfo_height()) // 2
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._busy = top

    def _close_busy(self):
        if self._busy:
            try:
                self._busy.grab_release()
            except Exception:
                pass
            try:
                self._busy.destroy()
            except Exception:
                pass
            self._busy = None

    def show_success(self, stats: core.CopyStats):
        top = tk.Toplevel(self.root)
        top.title(APP_NAME)
        top.configure(bg=CARD)
        top.resizable(False, False)
        top.transient(self.root)
        try:
            top.grab_set()
        except Exception:
            pass
        outer = tk.Frame(top, bg=CARD, padx=26, pady=22)
        outer.pack()
        tk.Label(outer, text="Copy complete", bg=CARD, fg=TEXT, font=(FONT_SEMI, 15)).pack(anchor="w")
        tk.Label(
            outer,
            text=f"{stats.stamp_count} check mark(s) added across {stats.page_count} page(s).",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 9),
        ).pack(anchor="w", pady=(4, 10))
        path_label = tk.Label(
            outer,
            text=stats.output_path,
            bg="#F8FAFC",
            fg=TEXT,
            font=(FONT, 8),
            padx=10,
            pady=8,
            anchor="w",
            justify="left",
            wraplength=390,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        path_label.pack(fill="x", pady=(0, 14))
        buttons = tk.Frame(outer, bg=CARD)
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            text="Open PDF",
            style="Accent.TButton",
            command=lambda: (top.destroy(), self.open_last_output()),
            cursor="hand2",
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="Show folder",
            style="Secondary.TButton",
            command=lambda: self._show_folder(stats.output_path),
            cursor="hand2",
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="Close", style="Ghost.TButton", command=top.destroy, cursor="hand2"
        ).pack(side="right")
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - top.winfo_height()) // 2
        top.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _show_folder(self, path: str):
        try:
            os.startfile(os.path.dirname(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    # ------------------------------------------------------------------- help
    def show_help(self):
        top = tk.Toplevel(self.root)
        top.title("How it works")
        top.configure(bg=CARD)
        top.resizable(False, False)
        top.transient(self.root)
        frame = tk.Frame(top, bg=CARD, padx=26, pady=22)
        frame.pack()
        tk.Label(frame, text="How Checkmark Copier works", bg=CARD, fg=TEXT, font=(FONT_SEMI, 15)).pack(anchor="w")
        steps = [
            ("1", "Open the PDF you already signed or checked."),
            ("2", "Choose the source page that contains the checks."),
            ("3", "Review the marks found. Place any that were missed."),
            ("4", "Enter the pages to copy to, then save a copy."),
        ]
        for num, text in steps:
            row = tk.Frame(frame, bg=CARD)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=num, bg=ACCENT_SOFT, fg=ACCENT, font=(FONT_SEMI, 8), width=2).pack(side="left")
            tk.Label(row, text=text, bg=CARD, fg=TEXT, font=(FONT, 9), anchor="w").pack(side="left", padx=(8, 0))
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", pady=12)
        tk.Label(
            frame,
            text=(
                "The scanner recognises vector check marks and small PDF form/stamp annotations. "
                "Scanned pages contain pixels, not paths, so use “Place checks” and click the marks there. "
                "Placed marks use the same three-point geometry, round caps and 1/20 stroke weight as "
                "Adobe Fill & Sign. Everything stays on this PC."
            ),
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8),
            justify="left",
            wraplength=500,
        ).pack(anchor="w")
        ttk.Button(frame, text="Got it", style="Accent.TButton", command=top.destroy, cursor="hand2").pack(
            anchor="e", pady=(16, 0)
        )
        top.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - top.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - top.winfo_height()) // 2
        top.geometry(f"+{max(0, x)}+{max(0, y)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pdf", help="Open this PDF at startup")
    parser.add_argument("--demo", action="store_true", help="Open the bundled demo PDF")
    parser.add_argument("--demo-preview", choices=["source", "result"], default="source")
    parser.add_argument("--screenshot", help="Capture the window to this PNG (development aid)")
    parser.add_argument("--smoke-test", action="store_true", help="Close automatically (development aid)")
    args, _unknown = parser.parse_known_args(argv)
    return args


def centre(root: tk.Tk):
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 20)}")


def capture_window(root: tk.Tk, path: str):
    try:
        root.update_idletasks()
        x = root.winfo_rootx()
        y = root.winfo_rooty()
        w = root.winfo_width()
        h = root.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        image.save(path)
        LOG.info("screenshot saved to %s (%dx%d)", path, w, h)
    except Exception:
        LOG.exception("screenshot failed")


def main(argv=None):
    args = parse_args(argv)
    enable_dpi_awareness()
    root = tk.Tk()

    def report_callback_exception(exc_type, exc_value, exc_tb):
        LOG.error("Unhandled Tk callback exception", exc_info=(exc_type, exc_value, exc_tb))
        try:
            messagebox.showerror(
                APP_NAME,
                "Something went wrong while updating the window.\n\n"
                f"{exc_value}\n\nThe log is in %LOCALAPPDATA%\\CheckmarkCopier\\app.log.",
                parent=root,
            )
        except Exception:
            pass

    root.report_callback_exception = report_callback_exception
    app = CheckmarkCopierApp(root)
    centre(root)

    demo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "demo.pdf")
    if args.demo and os.path.exists(demo_path):
        app.load_pdf(demo_path)
        app._quick_range("after")
        if args.demo_preview == "result":
            app.set_preview_mode("result")
    elif args.pdf and os.path.exists(args.pdf):
        app.load_pdf(args.pdf)

    if args.screenshot:
        delay = 900

        def do_capture():
            root.update_idletasks()
            root.update()
            time.sleep(0.25)
            capture_window(root, args.screenshot)
            if args.smoke_test:
                root.after(200, root.destroy)

        root.after(delay, do_capture)
    elif args.smoke_test:
        root.after(1200, root.destroy)

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
