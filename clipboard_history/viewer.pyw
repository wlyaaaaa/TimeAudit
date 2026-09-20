from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipboard_history.control import read_control_state, write_control_state
from clipboard_history.paths import runtime_paths
from clipboard_history.personal_access import SharedPersonalAccess, permitted
from clipboard_history.storage import ReadOnlyClipboardStore
from clipboard_history.win32_clipboard import restore_text


PAGE_SIZE = 50
CHINA_TZ = timezone(timedelta(hours=8), name="UTC+08:00")


def _date_bound(value: str, *, end: bool) -> str | None:
    if not value.strip():
        return None
    local_date = date.fromisoformat(value.strip())
    if end:
        local_date += timedelta(days=1)
    china = datetime.combine(local_date, datetime_time.min, tzinfo=CHINA_TZ)
    return china.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _display_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


class HistoryViewer(tk.Tk):
    def __init__(self, data_root: Path):
        super().__init__()
        self.title("TimeAudit 剪贴板历史")
        self.geometry("1120x760")
        self.minsize(860, 580)
        self.paths = runtime_paths(data_root)
        self.access = SharedPersonalAccess()
        self.access_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipboard-access")
        self.access_job = self.access_worker.submit(self.access.initialize)
        self.access_open = False
        self.history_job = None
        self.offset = 0
        self.rows: dict[str, dict[str, object]] = {}

        self.query_var = tk.StringVar()
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.type_var = tk.StringVar(value="全部")
        self.restore_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="正在读取采集状态…")
        self.page_var = tk.StringVar(value="第 1 页")
        self.access_var = tk.StringVar(value="正在确认个人资料访问状态…")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._check_access)
        self.after(500, self._refresh_status)

    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=28)
        access_bar = ttk.Frame(self, padding=(10, 8))
        access_bar.pack(fill=tk.X)
        ttk.Label(access_bar, textvariable=self.access_var).pack(side=tk.LEFT)
        self.unlock_button = ttk.Button(access_bar, text="解锁个人资料", command=self._unlock,
                                       state=tk.DISABLED)
        self.unlock_button.pack(side=tk.RIGHT)
        toolbar = ttk.Frame(self, padding=10)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="关键词").grid(row=0, column=0, sticky=tk.W)
        query = ttk.Entry(toolbar, textvariable=self.query_var, width=34)
        query.grid(row=0, column=1, padx=(6, 12), sticky=tk.EW)
        query.bind("<Return>", lambda _event: self.refresh())
        query.bind("<<Copy>>", self._copy_preview_guard)
        query.bind("<Control-c>", self._copy_preview_guard)
        ttk.Label(toolbar, text="开始日期").grid(row=0, column=2)
        ttk.Entry(toolbar, textvariable=self.from_var, width=12).grid(
            row=0, column=3, padx=(6, 12)
        )
        ttk.Label(toolbar, text="结束日期").grid(row=0, column=4)
        ttk.Entry(toolbar, textvariable=self.to_var, width=12).grid(
            row=0, column=5, padx=(6, 12)
        )
        ttk.Label(toolbar, text="类型").grid(row=0, column=6)
        ttk.Combobox(
            toolbar,
            textvariable=self.type_var,
            values=("全部", "文本", "URL", "文件路径"),
            state="readonly",
            width=10,
        ).grid(row=0, column=7, padx=(6, 12))
        ttk.Checkbutton(
            toolbar, text="显示再次复制事件", variable=self.restore_var
        ).grid(row=0, column=8, padx=(0, 12))
        ttk.Button(toolbar, text="搜索", command=self.refresh).grid(row=0, column=9)
        toolbar.columnconfigure(1, weight=1)

        splitter = ttk.Panedwindow(self, orient=tk.VERTICAL)
        splitter.pack(fill=tk.BOTH, expand=True, padx=10)
        list_frame = ttk.Frame(splitter)
        preview_frame = ttk.Labelframe(splitter, text="完整内容预览", padding=8)
        splitter.add(list_frame, weight=3)
        splitter.add(preview_frame, weight=2)

        columns = ("time", "type", "kind", "preview")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("time", text="时间（UTC+8）")
        self.tree.heading("type", text="类型")
        self.tree.heading("kind", text="来源")
        self.tree.heading("preview", text="内容预览")
        self.tree.column("time", width=165, stretch=False)
        self.tree.column("type", width=90, stretch=False)
        self.tree.column("kind", width=105, stretch=False)
        self.tree.column("preview", width=650)
        list_scroll = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=list_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._select)

        self.preview = tk.Text(preview_frame, wrap=tk.WORD, state=tk.DISABLED)
        preview_scroll = ttk.Scrollbar(
            preview_frame, orient=tk.VERTICAL, command=self.preview.yview
        )
        self.preview.configure(yscrollcommand=preview_scroll.set)
        self.preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.preview.bind("<<Copy>>", self._copy_preview_guard)
        self.preview.bind("<Control-c>", self._copy_preview_guard)
        preview_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        footer = ttk.Frame(self, padding=10)
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status_var).pack(side=tk.LEFT)
        self.pause_button = ttk.Button(
            footer, text="暂停采集", command=self._toggle_pause
        )
        self.pause_button.pack(side=tk.LEFT, padx=12)
        self.copy_button = ttk.Button(
            footer, text="再次复制", command=self._restore_selected, state=tk.DISABLED
        )
        self.copy_button.pack(side=tk.RIGHT)
        ttk.Button(footer, text="下一页", command=self._next_page).pack(
            side=tk.RIGHT, padx=6
        )
        ttk.Label(footer, textvariable=self.page_var).pack(side=tk.RIGHT, padx=6)
        ttk.Button(footer, text="上一页", command=self._previous_page).pack(
            side=tk.RIGHT, padx=6
        )

    def refresh(self, keep_offset: bool = False) -> None:
        if not self._require_access():
            return
        if not keep_offset:
            self.offset = 0
        type_map = {"文本": "text", "URL": "url", "文件路径": "file_paths"}
        try:
            query = dict(
                query=self.query_var.get(),
                date_from=_date_bound(self.from_var.get(), end=False),
                date_to=_date_bound(self.to_var.get(), end=True),
                payload_type=type_map.get(self.type_var.get()),
                include_restores=self.restore_var.get(),
                limit=PAGE_SIZE,
                offset=self.offset,
            )
        except ValueError as error:
            messagebox.showerror("筛选条件无效", str(error))
            return
        self._queue_history("search", query)

    def _show_rows(self, rows) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows = {str(row["event_id"]): row for row in rows}
        labels = {"text": "文本", "url": "URL", "file_paths": "文件路径"}
        for row in rows:
            event_id = str(row["event_id"])
            self.tree.insert(
                "",
                tk.END,
                iid=event_id,
                values=(
                    _display_time(str(row["observed_at_utc"])),
                    labels.get(row["payload_type"], row["payload_type"]),
                    "再次复制"
                    if row["observation_kind"] == "history_restore"
                    else "新复制",
                    row["preview"],
                ),
            )
        self.page_var.set(f"第 {self.offset // PAGE_SIZE + 1} 页")
        self._set_preview("")

    def _set_preview(self, text: str) -> None:
        self.preview.configure(state=tk.NORMAL)
        self.preview.delete("1.0", tk.END)
        self.preview.insert("1.0", text)
        self.preview.configure(state=tk.DISABLED)
        self.copy_button.configure(state=tk.NORMAL if text else tk.DISABLED)

    def _selected_event_id(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _select(self, _event=None) -> None:
        if not self._require_access():
            return
        event_id = self._selected_event_id()
        self._set_preview("")
        if event_id:
            self._queue_history("preview", event_id)

    def _restore_selected(self) -> None:
        if not self._require_access():
            return
        event_id = self._selected_event_id()
        if event_id:
            self._queue_history("copy", event_id)

    def _lease_identity(self):
        status = self.access.status()
        return (status.get("lock_generation"), status["privacy_access"]["expires_at_unix"]) if permitted(status) else None

    def _queue_history(self, kind, query) -> None:
        lease = self._lease_identity()
        if lease is None:
            self._clear_private_view()
            return
        if self.history_job is not None:
            self.history_job[2].cancel()
        self.history_job = (kind, lease, self.access_worker.submit(self._read_history, kind, query, lease))

    def _read_history(self, kind, query, lease):
        # Each SQLite connection belongs to this short worker call. A slow
        # query cannot block the UI timer that hides expired/locked content.
        if self._lease_identity() != lease:
            return None
        store = ReadOnlyClipboardStore(self.paths.database)
        try:
            if self._lease_identity() != lease:
                return None
            result = store.search(**query) if kind == "search" else store.get_event(query)
            return result if self._lease_identity() == lease else None
        finally:
            store.close()

    def _finish_history(self) -> None:
        if self.history_job is None or not self.history_job[2].done():
            return
        kind, lease, future = self.history_job
        self.history_job = None
        try:
            value = future.result()
            if self._lease_identity() != lease or value is None:
                self._require_access()
                return
            if kind == "search":
                self._show_rows(value)
            elif kind == "preview":
                self._set_preview(str(value["text"]) if value.get("text") else "")
            elif value.get("text"):
                restore_text(str(value["event_id"]), str(value["text"]))
                self.status_var.set("已再次复制；采集器会保留并标记这次恢复事件。")
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            self.status_var.set("历史读取或复制暂不可用，请稍后重试。")

    def _toggle_pause(self) -> None:
        current = read_control_state(self.paths.control)
        write_control_state(self.paths.control, paused=not current.paused)
        self._refresh_status()

    def _refresh_status(self) -> None:
        control = read_control_state(self.paths.control)
        state_text = "已暂停" if control.paused else "采集中"
        heartbeat_text = "心跳未知"
        try:
            heartbeat = json.loads(self.paths.heartbeat.read_text(encoding="utf-8"))
            updated = int(heartbeat["updated_at_unix_ms"]) / 1000
            age = max(0, int(datetime.now().timestamp() - updated))
            heartbeat_text = f"心跳 {age} 秒前"
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        self.status_var.set(f"{state_text} · {heartbeat_text}")
        self.pause_button.configure(text="继续采集" if control.paused else "暂停采集")
        self.after(2000, self._refresh_status)

    def _previous_page(self) -> None:
        self.offset = max(0, self.offset - PAGE_SIZE)
        self.refresh(keep_offset=True)

    def _next_page(self) -> None:
        self.offset += PAGE_SIZE
        self.refresh(keep_offset=True)

    def _require_access(self) -> bool:
        if permitted(self.access.status()):
            return True
        self._clear_private_view()
        return False

    def _clear_private_view(self) -> None:
        self.access_open = False
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self._set_preview("")
        self.query_var.set("")
        self.from_var.set("")
        self.to_var.set("")
        self.offset = 0
        self.page_var.set("第 1 页")
        if self.history_job is not None:
            self.history_job[2].cancel()
            self.history_job = None

    def _check_access(self) -> None:
        if self.access_job is not None and self.access_job.done():
            try:
                self.access_job.result()
            except Exception:
                self.access_var.set("个人资料状态暂不可用，历史内容已隐藏。")
            self.access_job = None
        status = self.access.status()
        if permitted(status):
            expiry = datetime.fromtimestamp(status["privacy_access"]["expires_at_unix"], CHINA_TZ)
            self.access_var.set(f"个人资料已解锁 · 共用截止 {expiry:%m-%d %H:%M:%S}（UTC+8）")
            self.unlock_button.configure(state=tk.DISABLED)
            if not self.access_open:
                self.access_open = True
                self.refresh()
            self._finish_history()
        else:
            self._clear_private_view()
            if self.access_job is not None:
                self.access_var.set("正在确认个人资料访问…")
            elif status.get("data_state") == "unknown":
                self.access_var.set("个人资料状态暂不可用，历史内容已隐藏。")
            else:
                self.access_var.set("个人资料未解锁，历史内容已隐藏。")
            self.unlock_button.configure(state=tk.NORMAL if self.access_job is None else tk.DISABLED)
        self.after(250, self._check_access)

    def _unlock(self) -> None:
        if self.access_job is not None:
            return
        if self._require_access():
            self.refresh()
            return
        self.unlock_button.configure(state=tk.DISABLED)
        self.access_var.set("请在个人资料验证窗口完成验证；取消后历史保持隐藏。")
        self.access_job = self.access_worker.submit(self.access.unlock)

    def _copy_preview_guard(self, _event=None):
        return None if self._require_access() else "break"

    def _close(self) -> None:
        if self.history_job is not None:
            self.history_job[2].cancel()
        self.access_worker.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    try:
        viewer = HistoryViewer(args.data_root)
    except (OSError, RuntimeError):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "剪贴板历史尚不可用",
            "私密历史库尚未建立或 FTS5 不可用。请确认采集器正在运行。",
        )
        root.destroy()
        return 2
    viewer.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
