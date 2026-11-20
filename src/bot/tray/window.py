"""系統匣的主視窗（customtkinter）：系統、要注意的事、持股、最近通知、ETF 關注名單。

外觀對齊 Hello Streamer（深色主題、同一組配色與按鈕樣式）。按視窗的 X 預設收進系統匣，
程式繼續執行；關掉「關閉時最小化到系統匣」開關後，按 X 才會結束系統匣程式。
"""
from __future__ import annotations

import dataclasses

import customtkinter as ctk

from bot.tray import settings as tray_settings
from bot.tray import status as tray_status

FONT_FAMILY = "Microsoft JhengHei UI"
# 與 Hello Streamer 的 stream_monitor/app_ui.py 相同的配色
CLR_BG = "#1a1a2e"
CLR_CARD = "#16213e"
CLR_ACCENT = "#0f3460"
CLR_ACCENT_HOVER = "#1a4a7a"
CLR_PANEL_BORDER = "#3a3a55"
CLR_TEXT = "#e6eaf2"
CLR_TEXT_SECONDARY = "#9aa0b4"
CLR_TEXT_MUTED = "#7f8499"
CLR_GAIN = "#00e676"
CLR_LOSS = "#ef5350"
STATUS_COLOR = {tray_status.OK: "#00e676", tray_status.WARN: "#ffb300",
                tray_status.FAIL: "#ef5350", tray_status.UNKNOWN: "#666677"}
BUTTON_STYLE = {"height": 34, "corner_radius": 8, "fg_color": "#263653", "hover_color": "#354866",
                "border_width": 1, "border_color": "#465570", "text_color": CLR_TEXT}
WIDTH, HEIGHT, MIN_HEIGHT = 560, 680, 420
WRAP = WIDTH - 96  # 卡片內文字的換行寬度


def _font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def content_signature(snap, busy: str = "") -> tuple:
    """畫面內容有沒有變。讀取時間與心跳秒數每輪都不同，另外就地更新，不算在內。"""
    data = dataclasses.asdict(snap)
    data.pop("read_at", None)
    data.pop("heartbeat_age_seconds", None)
    return busy, repr(data)


def scheduler_line(snap) -> str:
    beat = snap.heartbeat_age_seconds
    return ("排程器：" + (snap.scheduler_state or "未知")
            + ("" if beat is None else f"，{beat:.0f} 秒前有心跳")
            + ("；執行中：" + "、".join(snap.running_jobs) if snap.running_jobs else ""))


class StatusWindow:
    """直接用 Tk 的根視窗當主視窗：工作列看得到、可以最小化，按 X 收進系統匣。"""

    def __init__(self, app):
        self.app = app
        self.top = app.tk
        self.top.title("Stock Lab")
        self.top.configure(fg_color=CLR_BG)
        self.top.minsize(WIDTH, MIN_HEIGHT)
        self.top.resizable(False, True)
        self.top.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self.top.bind("<Escape>", lambda _e: self.hide())
        self.visible = False
        self._placed = False
        self._signature = None
        self._scheduler_label = None

        outer = ctk.CTkFrame(self.top, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=16, pady=(12, 14))

        # ── 標題列 ──
        title_bar = ctk.CTkFrame(outer, fg_color="transparent")
        title_bar.pack(fill="x")
        self.dot = ctk.CTkLabel(title_bar, text="●", font=_font(18), width=22)
        self.dot.pack(side="left")
        ctk.CTkLabel(title_bar, text="Stock Lab", font=_font(18, "bold"),
                     text_color=CLR_TEXT).pack(side="left", padx=(4, 10))
        self.level = ctk.CTkLabel(title_bar, font=_font(13, "bold"))
        self.level.pack(side="left")
        self.minimize_var = ctk.BooleanVar(value=bool(app.settings.get("minimize_to_tray", True)))
        ctk.CTkSwitch(title_bar, text="關閉時最小化到系統匣", variable=self.minimize_var,
                      command=self._on_switch, font=_font(12),
                      text_color=CLR_TEXT_SECONDARY).pack(side="right")

        self.headline = ctk.CTkLabel(outer, font=_font(13), text_color=CLR_TEXT_SECONDARY,
                                     anchor="w", justify="left", wraplength=WIDTH - 40)
        self.headline.pack(fill="x", pady=(6, 8))

        # ── 下方控制列（先 pack 在底部，內容多的時候不會被擠出視窗）──
        panel = ctk.CTkFrame(outer, corner_radius=12, fg_color=CLR_CARD,
                             border_width=1, border_color=CLR_PANEL_BORDER)
        panel.pack(side="bottom", fill="x", pady=(10, 0))
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(row, text="儀表板", command=app.open_dashboard, font=_font(13, "bold"),
                      width=88, **{**BUTTON_STYLE, "fg_color": CLR_ACCENT,
                                   "hover_color": CLR_ACCENT_HOVER}).pack(side="left")
        ctk.CTkButton(row, text="ETF 關注名單", width=112, font=_font(13),
                      command=lambda: app.open_path("data/etf_focus/latest.md"),
                      **BUTTON_STYLE).pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="重新整理", width=88, font=_font(13), command=app.refresh,
                      **BUTTON_STYLE).pack(side="left", padx=(8, 0))
        self.updated = ctk.CTkLabel(row, font=_font(11), text_color=CLR_TEXT_MUTED)
        self.updated.pack(side="right")

        # ── 內容（可捲動）──
        container = ctk.CTkFrame(outer, corner_radius=12, fg_color=CLR_ACCENT)
        container.pack(fill="both", expand=True)
        self.body = ctk.CTkScrollableFrame(container, corner_radius=0, fg_color=CLR_ACCENT,
                                           scrollbar_button_color="#333355",
                                           scrollbar_button_hover_color="#444466")
        self.body.pack(fill="both", expand=True, padx=4, pady=8)

    # ---- 顯示、隱藏、關閉 --------------------------------------------------
    def _place(self) -> None:
        """第一次顯示時靠主螢幕右下角、工作列上方；之後留在使用者放的位置。"""
        try:
            scale = float(self.top._get_window_scaling())  # noqa: SLF001 - 位置要用實際像素
        except Exception:  # noqa: BLE001
            scale = 1.0
        x = self.top.winfo_screenwidth() - int(WIDTH * scale) - 24
        y = self.top.winfo_screenheight() - int(HEIGHT * scale) - 96
        self.top.geometry(f"{WIDTH}x{HEIGHT}+{max(0, x)}+{max(0, y)}")
        self._placed = True

    def show(self) -> None:
        self.visible = True
        self.render(self.app.snapshot)
        if not self._placed:
            self._place()
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def hide(self) -> None:
        """收進系統匣：視窗隱藏，程式繼續在系統匣執行。"""
        self.visible = False
        self.top.withdraw()

    def on_close_button(self) -> None:
        if self.minimize_var.get():
            self.hide()
        else:
            self.app.quit()

    def _on_switch(self) -> None:
        self.app.settings["minimize_to_tray"] = bool(self.minimize_var.get())
        self.app.save_settings()

    # ---- 內容 --------------------------------------------------------------
    def _section(self, title: str) -> ctk.CTkFrame:
        ctk.CTkLabel(self.body, text=title, font=_font(12, "bold"), text_color=CLR_TEXT_SECONDARY,
                     anchor="w", height=20).pack(fill="x", padx=8, pady=(8, 4))
        card = ctk.CTkFrame(self.body, corner_radius=10, fg_color=CLR_CARD,
                            border_width=1, border_color=CLR_PANEL_BORDER)
        card.pack(fill="x", padx=4)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)
        return inner

    def _row(self, card, text: str, color: str = CLR_TEXT, bold: bool = False,
             size: int = 13) -> ctk.CTkLabel:
        label = ctk.CTkLabel(card, text=text, font=_font(size, "bold" if bold else "normal"),
                             text_color=color, anchor="w", justify="left", wraplength=WRAP,
                             height=22)
        label.pack(fill="x", pady=1)
        return label

    def render(self, snap) -> None:
        color = STATUS_COLOR[snap.level]
        busy = self.app._busy  # noqa: SLF001
        self.dot.configure(text_color=color)
        self.level.configure(text=tray_status.LEVEL_LABEL[snap.level], text_color=color)
        self.updated.configure(text=f"更新於 {snap.read_at[11:19]}")
        self.headline.configure(text=busy or snap.headline)

        signature = content_signature(snap, busy)
        if signature == self._signature:
            # 內容沒變就不重畫（重建元件會閃一下），只更新心跳那一行
            if self._scheduler_label is not None:
                self._scheduler_label.configure(text=scheduler_line(snap))
            return
        self._signature = signature
        for child in self.body.winfo_children():
            child.destroy()

        card = self._section("持股")
        if snap.holdings:
            for h in snap.holdings:
                self._row(card, f"{h.symbol}　{h.lots} 張、均價 {h.avg_price:.2f}　現價 {h.price:.2f}",
                          bold=True, size=14)
                self._row(card, f"損益 {h.pnl_twd:+,.0f} 元（{h.pnl_pct:+.1f}%）",
                          color=CLR_LOSS if h.pnl_twd < 0 else CLR_GAIN, bold=True)
                detail = f"損益兩平 {h.breakeven_price:.2f}"
                if h.stop_price is not None:
                    detail += f"　停損價 {h.stop_price:.2f}（還差 {h.stop_gap_pct:.1f}%）"
                self._row(card, detail, color=CLR_TEXT_SECONDARY, size=12)
                if h.signals:
                    self._row(card, "訊號：" + "、".join(h.signals), color=CLR_LOSS, bold=True)
                if h.stale:
                    self._row(card, f"報價時間 {h.ts[:16].replace('T', ' ')}，不是今天的",
                              color=CLR_TEXT_MUTED, size=12)
        else:
            self._row(card, "沒有盯盤中的持股", color=CLR_TEXT_MUTED)

        problems = snap.problems()
        card = self._section(f"要注意（{len(problems)}）" if problems else "系統")
        if problems:
            for check in problems:
                self._row(card, check.name, color=STATUS_COLOR[check.status], bold=True)
                self._row(card, check.detail, color=CLR_TEXT_SECONDARY, size=12)
        else:
            self._row(card, f"{len(snap.checks)} 項檢查都正常", color=STATUS_COLOR[tray_status.OK])
        self._scheduler_label = self._row(card, scheduler_line(snap), color=CLR_TEXT_MUTED, size=12)

        card = self._section("最近通知")
        if snap.notifications:
            for row in snap.notifications:
                when = str(row.get("ts", ""))[5:16].replace("T", " ")
                self._row(card, f"{when}　{row.get('title', '')}")
        else:
            self._row(card, "還沒有通知", color=CLR_TEXT_MUTED)

        focus = snap.focus
        card = self._section(f"ETF 關注名單（持股到 {focus.as_of}）" if focus.as_of else "ETF 關注名單")
        if focus.as_of:
            self._row(card, f"關注 {focus.focus_count} 檔、留意減碼 {focus.caution_count} 檔",
                      color=CLR_TEXT_SECONDARY, size=12)
            if focus.top_adds:
                self._row(card, "主動式加碼最多：" + "、".join(focus.top_adds))
            if focus.top_reduces:
                self._row(card, "主動式減碼最多：" + "、".join(focus.top_reduces))
        else:
            self._row(card, "還沒有產生", color=CLR_TEXT_MUTED)
