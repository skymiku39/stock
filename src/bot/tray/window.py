"""系統匣的小狀態視窗（tkinter）：系統、要注意的事、持股、最近通知、ETF 關注名單。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from bot.tray import status as tray_status

FONT = ("Microsoft JhengHei UI", 10)
FONT_BOLD = ("Microsoft JhengHei UI", 10, "bold")
FONT_TITLE = ("Microsoft JhengHei UI", 12, "bold")
BG, CARD, MUTED = "#f4f5f7", "#ffffff", "#6b7280"
WIDTH, HEIGHT = 520, 640
STATUS_COLOR = {tray_status.OK: "#2e9e5b", tray_status.WARN: "#b97f00",
                tray_status.FAIL: "#d64545", tray_status.UNKNOWN: "#8a8f98"}


class StatusWindow:
    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.tk)
        self.top.title("Stock Lab")
        self.top.configure(bg=BG)
        self.top.attributes("-topmost", True)
        self.top.protocol("WM_DELETE_WINDOW", self.hide)
        self.top.bind("<Escape>", lambda _e: self.hide())
        self.visible = False

        header = tk.Frame(self.top, bg=BG)
        header.pack(fill="x", padx=14, pady=(12, 6))
        self.dot = tk.Canvas(header, width=18, height=18, bg=BG, highlightthickness=0)
        self.dot.pack(side="left")
        self.title = tk.Label(header, font=FONT_TITLE, bg=BG, anchor="w")
        self.title.pack(side="left", padx=(6, 0))
        self.updated = tk.Label(header, font=FONT, bg=BG, fg=MUTED)
        self.updated.pack(side="right")
        self.headline = tk.Label(self.top, font=FONT, bg=BG, anchor="w", justify="left",
                                 wraplength=WIDTH - 28)
        self.headline.pack(fill="x", padx=14)

        buttons = tk.Frame(self.top, bg=BG)
        buttons.pack(side="bottom", fill="x", padx=14, pady=10)
        for text, command in (("儀表板", app.open_dashboard),
                              ("ETF 關注名單", lambda: app.open_path("data/etf_focus/latest.md")),
                              ("重新整理", app.refresh)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="關閉", command=self.hide).pack(side="right")

        # 內容可捲動：警告多的時候不會把按鈕擠出視窗
        self.canvas = tk.Canvas(self.top, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.top, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=BG)
        self.body.bind("<Configure>",
                       lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.body, anchor="nw", width=WIDTH - 30)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", pady=8)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=8)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event) -> None:
        if self.visible:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _place(self) -> None:
        """靠主螢幕右下角、工作列上方。"""
        self.top.update_idletasks()
        x = self.top.winfo_screenwidth() - WIDTH - 16
        y = self.top.winfo_screenheight() - HEIGHT - 72
        self.top.geometry(f"{WIDTH}x{HEIGHT}+{max(0, x)}+{max(0, y)}")

    def show(self) -> None:
        self.visible = True
        self.render(self.app.snapshot)
        self._place()
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def hide(self) -> None:
        self.visible = False
        self.top.withdraw()

    # ---- 內容 --------------------------------------------------------------
    def _section(self, title: str) -> tk.Frame:
        tk.Label(self.body, text=title, font=FONT_BOLD, bg=BG, fg=MUTED,
                 anchor="w").pack(fill="x", pady=(10, 3))
        card = tk.Frame(self.body, bg=CARD, padx=10, pady=8)
        card.pack(fill="x")
        return card

    def _row(self, card, text: str, color: str = "#111827", bold: bool = False) -> None:
        tk.Label(card, text=text, font=FONT_BOLD if bold else FONT, bg=CARD, fg=color,
                 anchor="w", justify="left", wraplength=WIDTH - 60).pack(fill="x", pady=1)

    def render(self, snap) -> None:
        color = STATUS_COLOR[snap.level]
        self.dot.delete("all")
        self.dot.create_oval(2, 2, 16, 16, fill=color, outline=color)
        self.title.configure(text=tray_status.LEVEL_LABEL[snap.level], fg=color)
        self.updated.configure(text=f"更新於 {snap.read_at[11:19]}")
        self.headline.configure(text=self.app._busy or snap.headline)
        for child in self.body.winfo_children():
            child.destroy()

        card = self._section("持股")
        if snap.holdings:
            for h in snap.holdings:
                self._row(card, f"{h.symbol}　{h.lots} 張、均價 {h.avg_price:.2f}　現價 {h.price:.2f}",
                          bold=True)
                self._row(card, f"損益 {h.pnl_twd:+,.0f} 元（{h.pnl_pct:+.1f}%）",
                          color="#d64545" if h.pnl_twd < 0 else "#2e9e5b")
                detail = f"損益兩平 {h.breakeven_price:.2f}"
                if h.stop_price is not None:
                    detail += f"　停損價 {h.stop_price:.2f}（還差 {h.stop_gap_pct:.1f}%）"
                self._row(card, detail, color=MUTED)
                if h.signals:
                    self._row(card, "訊號：" + "、".join(h.signals), color="#d64545", bold=True)
                if h.stale:
                    self._row(card, f"報價時間 {h.ts[:16].replace('T', ' ')}，不是今天的", color=MUTED)
        else:
            self._row(card, "沒有盯盤中的持股", color=MUTED)

        problems = snap.problems()
        card = self._section(f"要注意（{len(problems)}）" if problems else "系統")
        if problems:
            for check in problems:
                self._row(card, check.name, color=STATUS_COLOR[check.status], bold=True)
                self._row(card, check.detail, color=MUTED)
        else:
            self._row(card, f"{len(snap.checks)} 項檢查都正常", color=STATUS_COLOR[tray_status.OK])
        beat = snap.heartbeat_age_seconds
        self._row(card, "排程器：" + (snap.scheduler_state or "未知")
                  + ("" if beat is None else f"，{beat:.0f} 秒前有心跳")
                  + ("；執行中：" + "、".join(snap.running_jobs) if snap.running_jobs else ""),
                  color=MUTED)

        card = self._section("最近通知")
        if snap.notifications:
            for row in snap.notifications:
                when = str(row.get("ts", ""))[5:16].replace("T", " ")
                self._row(card, f"{when}　{row.get('title', '')}")
        else:
            self._row(card, "還沒有通知", color=MUTED)

        focus = snap.focus
        card = self._section(f"ETF 關注名單（持股到 {focus.as_of}）" if focus.as_of else "ETF 關注名單")
        if focus.as_of:
            self._row(card, f"關注 {focus.focus_count} 檔、留意減碼 {focus.caution_count} 檔", color=MUTED)
            if focus.top_adds:
                self._row(card, "主動式加碼最多：" + "、".join(focus.top_adds))
            if focus.top_reduces:
                self._row(card, "主動式減碼最多：" + "、".join(focus.top_reduces))
        else:
            self._row(card, "還沒有產生", color=MUTED)
        self.canvas.yview_moveto(0)
