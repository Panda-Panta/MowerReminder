# -*- coding: utf-8 -*-

#======================================================================================================================
# Mower Reminder - 整合版（已移除开机自启动功能）
# 支持两种模式：
#   1. 网址版：通过 WebSocket 实时接收日志，解析任务时间。
#   2. 本地版：读取本地日志文件，周期性扫描解析任务时间。
# 可在设置界面自由切换模式，相关配置项会动态显示/隐藏。
#======================================================================================================================

import tkinter as tk
from datetime import datetime, timedelta
import os
import sys
import platform
import ctypes
import configparser
from pystray import MenuItem as item, Menu, Icon
from PIL import Image
import tkinter.messagebox
import tkinter.filedialog
import socket
import threading
import time
import json

# 尝试导入 websocket-client（用于网址版）
try:
    import websocket
    has_websocket = True
except ImportError:
    has_websocket = False
    print("警告: 未安装 websocket-client 库。网址模式将不可用。请运行 'pip install websocket-client'")

# 尝试导入 winotify 用于 Windows 原生通知
try:
    from winotify import Notification, audio
    has_winotify = True
except ImportError:
    has_winotify = False
    print("警告: 未安装 winotify 库。Windows 原生通知功能将受限。请运行 'pip install winotify'")

# 仅在Windows上导入win32gui、win32con、win32event、win32api和winreg用于窗口操作和互斥体
if platform.system() == "Windows":
    try:
        import win32gui
        import win32con
        import win32event
        import win32api
        import winerror
        # 注意：不再需要 winreg，因为已移除开机自启动
    except ImportError:
        print("警告: 未安装 pywin32 库。Windows 单实例、窗口激活功能可能受限。请运行 'pip install pywin32'")
        win32gui = None
        win32con = None
        win32event = None
        win32api = None
        winerror = None

# 在Windows上设置标题栏深色模式
try:
    if platform.system() == "Windows":
        from ctypes import windll
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        def set_dark_title_bar(window_handle):
            try:
                windll.dwmapi.DwmSetWindowAttribute(
                    window_handle,
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(ctypes.c_int(1)),
                    ctypes.sizeof(ctypes.c_int)
                )
            except Exception as e:
                print(f"设置深色标题栏时出错: {e}")
except (ImportError, OSError):
    pass

# --- 配置 ---
CONFIG_FILE = 'mower_config.ini'

DEFAULT_CONFIG = {
    'Settings': {
        'mode': 'local',
        'log_file_path': '',
        'scan_interval_seconds': '10',
        'websocket_host': '',
        'websocket_port': '',
        'websocket_token': '',
        'alert_seconds': '120',
        'update_interval_ms': '500',
        'window_width': '360',
        'window_height': '60',
        'font_family': 'Segoe UI',
        'font_size': '24',
        'font_weight': 'bold',
        'start_minimized': 'False',
        'log_start_marker': '到',
        'log_end_marker': '开始工作',
        'notify_minutes_a': '5',
        'notify_minutes_b': '2',
        'notify_minutes_c': '1',
        'notify_enabled_a': 'True',
        'notify_enabled_b': 'True',
        'notify_enabled_c': 'True',
        'notify_enabled_task_completed': 'True'
    }
}

APP_TITLE = "Mower Reminder"
ICON_FILE = "mower.ico"
SINGLE_INSTANCE_PORT = 12345

# --- 颜色配置 ---
COLOR_NORMAL_BG = "#1e1e1e"
COLOR_ALERT_BG = "red"
COLOR_TEXT = "white"
COLOR_BUTTON_BG = "#3e3e3e"
COLOR_BUTTON_FG = "white"
COLOR_BUTTON_ACTIVE_BG = "#505050"
COLOR_FRAME_BG = "#2e2e2e"

def get_resource_path(relative_path):
    try:
        base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

class CountdownApp:
    def __init__(self, root):
        self.root = root
        self.config = configparser.ConfigParser()
        self.load_config()

        self.icon_file_name = ICON_FILE

        # 读取通用配置
        self.mode = self.config.get('Settings', 'mode')  # 'local' 或 'web'
        self.alert_seconds = int(self.config.get('Settings', 'alert_seconds'))
        self.update_interval_ms = int(self.config.get('Settings', 'update_interval_ms'))
        self.window_width = int(self.config.get('Settings', 'window_width'))
        self.window_height = int(self.config.get('Settings', 'window_height'))
        self.font_family = self.config.get('Settings', 'font_family')
        self.font_size = int(self.config.get('Settings', 'font_size'))
        self.font_weight = self.config.get('Settings', 'font_weight')
        self.start_minimized = self.config.getboolean('Settings', 'start_minimized')
        self.log_start_marker = self.config.get('Settings', 'log_start_marker')
        self.log_end_marker = self.config.get('Settings', 'log_end_marker')
        self.notify_minutes_a = int(self.config.get('Settings', 'notify_minutes_a'))
        self.notify_minutes_b = int(self.config.get('Settings', 'notify_minutes_b'))
        self.notify_minutes_c = int(self.config.get('Settings', 'notify_minutes_c'))
        self.notify_enabled_a = self.config.getboolean('Settings', 'notify_enabled_a')
        self.notify_enabled_b = self.config.getboolean('Settings', 'notify_enabled_b')
        self.notify_enabled_c = self.config.getboolean('Settings', 'notify_enabled_c')
        self.notify_enabled_task_completed = self.config.getboolean('Settings', 'notify_enabled_task_completed')

        # 本地版配置
        self.log_file_path = self.config.get('Settings', 'log_file_path')
        self.scan_interval_seconds = int(self.config.get('Settings', 'scan_interval_seconds'))

        # 网址版配置
        self.websocket_host = self.config.get('Settings', 'websocket_host')
        self.websocket_port = self.config.get('Settings', 'websocket_port')
        self.websocket_token = self.config.get('Settings', 'websocket_token')

        # 状态变量
        self.next_task_time = None
        self._previous_task_time_state = None
        self.flash_on = False
        self.is_always_on_top = True

        # 通知标志
        self.notified_a = False
        self.notified_b = False
        self.notified_c = False
        self.notified_new_task_start = False

        # 本地版扫描相关
        self.last_log_file_mtime = 0
        self.next_scan_time = datetime.min
        self.log_file_error_shown = False
        self.log_parse_error_shown = False

        # 网址版 WebSocket 相关
        self.ws_thread = None
        self.ws_stop_event = threading.Event()

        # --- 窗口设置 ---
        self.root.overrideredirect(True)
        self.root.title(APP_TITLE)
        self.root.geometry(f"{self.window_width}x{self.window_height}")
        self.root.resizable(False, False)

        icon_path = get_resource_path(self.icon_file_name)
        if os.path.exists(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except tk.TclError as e:
                print(f"警告: 设置 Tkinter 窗口图标时出错 ({e})")
        else:
            print("警告: 找不到图标文件 mower.ico。")

        if platform.system() == "Windows" and win32gui and win32con and win32api:
            self.root.after(100, lambda: self._set_windows_taskbar_icon(get_resource_path(self.icon_file_name)))

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width // 2) - (self.window_width // 2)
        y = (screen_height // 2) - (self.window_height // 2)
        self.root.geometry(f"+{x}+{y}")

        self.root.attributes("-topmost", self.is_always_on_top)

        if platform.system() == "Windows":
            self.root.after(1, lambda: set_dark_title_bar(self.root.winfo_id()))

        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        if self.start_minimized:
            self.root.withdraw()

        # --- 主内容控件 ---
        main_content_frame = tk.Frame(self.root, bg=COLOR_NORMAL_BG)
        main_content_frame.pack(expand=True, fill="both")

        info_frame = tk.Frame(main_content_frame, bg=COLOR_NORMAL_BG)
        info_frame.pack(side="top", fill="x", padx=5, pady=5)

        self.settings_btn = tk.Button(info_frame,
                                      text="⚙️",
                                      command=self.show_settings_window,
                                      font=("Segoe UI", 10),
                                      bg=COLOR_BUTTON_BG,
                                      fg=COLOR_BUTTON_FG,
                                      activebackground=COLOR_BUTTON_ACTIVE_BG,
                                      bd=0, padx=5, pady=2, relief='flat')
        self.settings_btn.pack(side="right", padx=(0, 5))

        self.topmost_btn = tk.Button(info_frame,
                                     text="📍" if self.is_always_on_top else "📌",
                                     command=self.toggle_topmost,
                                     font=("Segoe UI", 10),
                                     bg=COLOR_BUTTON_BG,
                                     fg=COLOR_BUTTON_FG,
                                     activebackground=COLOR_BUTTON_ACTIVE_BG,
                                     bd=0, padx=5, pady=2, relief='flat')
        self.topmost_btn.pack(side="right")

        self.label = tk.Label(info_frame, text="正在初始化...", 
                              font=(self.font_family, self.font_size, self.font_weight), 
                              bg=COLOR_NORMAL_BG, fg=COLOR_TEXT)
        self.label.pack(side="left", expand=True, fill="x")

        # 拖拽功能
        self._x_offset = 0
        self._y_offset = 0
        self.root.bind("<Button-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._do_drag)

        # 创建任务栏图标
        self.create_tray_icon(get_resource_path(self.icon_file_name))

        # 根据模式启动数据源
        self._start_data_source()

        # 启动主更新循环
        self.update_countdown()

    # ---------- 拖拽 ----------
    def _start_drag(self, event):
        self._x_offset = event.x
        self._y_offset = event.y

    def _do_drag(self, event):
        x = self.root.winfo_x() + event.x - self._x_offset
        y = self.root.winfo_y() + event.y - self._y_offset
        self.root.geometry(f"+{x}+{y}")

    # ---------- 任务栏图标 ----------
    def _set_windows_taskbar_icon(self, icon_path):
        hwnd = self.root.winfo_id()
        if not hwnd:
            return
        try:
            icon_big = win32gui.LoadImage(
                win32api.GetModuleHandle(None),
                icon_path,
                win32con.IMAGE_ICON,
                0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE | win32con.LR_SHARED
            )
            icon_small = win32gui.LoadImage(
                win32api.GetModuleHandle(None),
                icon_path,
                win32con.IMAGE_ICON,
                0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE | win32con.LR_SHARED
            )
            win32api.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_BIG, icon_big)
            win32api.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, icon_small)
        except Exception as e:
            print(f"设置任务栏图标时出错: {e}")

    # ---------- 配置操作 ----------
    def load_config(self):
        self.config.read(CONFIG_FILE, encoding='utf-8')
        settings_changed = False
        if not self.config.has_section('Settings'):
            self.config.add_section('Settings')
            settings_changed = True
        for key, value in DEFAULT_CONFIG['Settings'].items():
            if not self.config.has_option('Settings', key):
                self.config.set('Settings', key, value)
                settings_changed = True
        if settings_changed:
            self.save_config()

    def save_config(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
        except Exception as e:
            print(f"保存配置文件时出错: {e}")
            tk.messagebox.showerror("保存错误", f"保存配置文件时出错:\n{e}", parent=self.root)

    # ---------- 界面控制 ----------
    def toggle_topmost(self):
        self.is_always_on_top = not self.is_always_on_top
        self.root.attributes("-topmost", self.is_always_on_top)
        self.topmost_btn.config(text="📍" if self.is_always_on_top else "📌")

    def hide_window(self):
        self.root.withdraw()

    def show_window(self, icon=None, item=None):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ---------- 托盘图标 ----------
    def create_tray_icon(self, icon_path):
        image = None
        try:
            image = Image.open(icon_path)
        except FileNotFoundError:
            print(f"错误: 任务栏图标文件 '{icon_path}' 未找到。")
        except Exception as e:
            print(f"错误: 加载任务栏图标 '{icon_path}' 时出错: {e}。")
        if image is None:
            image = Image.new('RGB', (64, 64), (255, 255, 255))

        menu = Menu(
            item('显示', lambda: self.root.after_idle(self.show_window)),
            item('隐藏', lambda: self.root.after_idle(self.hide_window)),
            item('设置', lambda: self.root.after_idle(self.show_settings_window)),
            item('退出', lambda: self.root.after_idle(self.quit_app))
        )
        self.icon = Icon('MowerCounter', image, APP_TITLE, menu)
        self.icon_thread = threading.Thread(target=self.icon.run, args=(self.setup_tray_icon,), daemon=True)
        self.icon_thread.start()

    def setup_tray_icon(self, icon):
        icon.visible = True
        icon.menu = Menu(
            item('显示', lambda: self.root.after_idle(self.show_window)),
            item('隐藏', lambda: self.root.after_idle(self.hide_window)),
            item('设置', lambda: self.root.after_idle(self.show_settings_window)),
            item('退出', lambda: self.root.after_idle(self.quit_app))
        )
        icon.update_menu()
        def on_tray_click(icon, item):
            if self.root.winfo_ismapped():
                self.root.after_idle(self.hide_window)
            else:
                self.root.after_idle(self.show_window)
            return 0
        self.icon.activate = on_tray_click

    # ---------- 通知 ----------
    def _send_notification(self, message, title):
        if has_winotify:
            icon_path = get_resource_path(self.icon_file_name)
            try:
                toast = Notification(
                    app_id=title,
                    title=title,
                    msg=message,
                    icon=icon_path,
                    duration="long"
                )
                toast.set_audio(audio.Default, loop=False)
                toast.show()
            except Exception as e:
                print(f"winotify 通知失败: {e}")
                if hasattr(self, 'icon') and self.icon:
                    self.icon.notify(message, title=title)
        elif hasattr(self, 'icon') and self.icon:
            self.icon.notify(message, title=title)
        else:
            print(f"通知: {title} - {message}")

    # ---------- 数据源管理 ----------
    def _start_data_source(self):
        if self.mode == 'web':
            self._stop_data_source()
            if has_websocket:
                self.start_websocket()
            else:
                self.label.config(text="websocket-client 未安装")
        else:  # local
            self._stop_data_source()
            self.label.config(text="本地模式，等待扫描...")
            self.scan_log_file()

    def _stop_data_source(self):
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_stop_event.set()
            self.ws_thread.join(timeout=0.3)
            if self.ws_thread.is_alive():
                print("[WS] WebSocket 线程未能在 0.3 秒内结束")
        self.ws_stop_event.clear()
        
    def restart_data_source(self):
        def _restart_worker():
            self._stop_data_source()
            self.root.after(0, self._start_data_source)
        threading.Thread(target=_restart_worker, daemon=True).start()

    # ---------- 本地版：日志文件扫描 ----------
    def scan_log_file(self):
        if not os.path.exists(self.log_file_path):
            if not hasattr(self, 'settings_window') or not self.settings_window.winfo_exists():
                if not self.log_file_error_shown:
                    self.log_file_error_shown = True
                    tk.messagebox.showerror("文件错误", f"日志文件不存在:\n{self.log_file_path}\n请检查设置中的文件路径。", parent=self.root)
                self.next_task_time = None
                return
        else:
            self.log_file_error_shown = False
        try:
            current_mtime = os.path.getmtime(self.log_file_path)
            if current_mtime == self.last_log_file_mtime and self.next_task_time is not None:
                return
            self.last_log_file_mtime = current_mtime
        except Exception as e:
            print(f"获取日志文件修改时间时出错: {e}")
            self.last_log_file_mtime = 0

        now = datetime.now()
        closest_future_time = None

        try:
            with open(self.log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                for line in reversed(lines):
                    try:
                        if len(line) < 19:
                            continue
                        timestamp_str = line[0:19]
                        log_datetime = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")

                        start_pos = line.find(self.log_start_marker)
                        if start_pos == -1:
                            continue
                        end_pos = line.find(self.log_end_marker, start_pos)
                        if end_pos == -1:
                            continue

                        time_part_str = line[start_pos + len(self.log_start_marker):end_pos].strip()
                        parsed_time = datetime.strptime(time_part_str, "%H:%M:%S").time()
                        potential_task_time = datetime.combine(log_datetime.date(), parsed_time)

                        if potential_task_time < log_datetime:
                            potential_task_time += timedelta(days=1)

                        if potential_task_time > now:
                            if closest_future_time is None or potential_task_time < closest_future_time:
                                closest_future_time = potential_task_time
                    except (ValueError, IndexError):
                        continue

            self.next_task_time = closest_future_time
            self.log_parse_error_shown = False
        except Exception as e:
            if not self.log_parse_error_shown:
                tk.messagebox.showerror("文件读取错误", f"读取日志文件时出错:\n{e}\n请检查文件权限或内容。", parent=self.root)
                self.log_parse_error_shown = True
            self.next_task_time = None

    # ---------- 网址版：WebSocket 连接 ----------
    def start_websocket(self):
        if not has_websocket:
            self.label.config(text="websocket-client 未安装")
            return
        self.ws_stop_event.clear()
        self.ws_thread = threading.Thread(target=self._ws_run, daemon=True)
        self.ws_thread.start()
        self.label.config(text="正在连接 WebSocket...")

    def _ws_run(self):
        while not self.ws_stop_event.is_set():
            try:
                host = self.websocket_host.replace('http://', '').replace('https://', '').strip('/')
                ws_url = f"ws://{host}:{self.websocket_port}/log?token={self.websocket_token}"
                origin = f"http://{host}:{self.websocket_port}"
                headers = {
                    'Origin': origin,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                print(f"[WS] 尝试连接: {ws_url}")
                ws = websocket.WebSocketApp(
                    ws_url,
                    header=headers,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"[WS] 连接异常: {e}")
            if not self.ws_stop_event.is_set():
                print("[WS] 10秒后重连...")
                time.sleep(10)

    def _on_ws_open(self, ws):
        print("[WS] 连接成功！")
        self.root.after(0, self.label.config, {"text": "已连接"})

    def _on_ws_error(self, ws, error):
        print(f"[WS] 错误: {error}")
        self.root.after(0, self.label.config, {"text": f"连接错误: {str(error)[:30]}"})

    def _on_ws_close(self, ws, close_status_code, close_msg):
        print(f"[WS] 连接关闭: {close_status_code} {close_msg}")
        self.root.after(0, self.label.config, {"text": "连接关闭，重连中..."})

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            log_line = data.get('data', '')
            if not log_line:
                return
            for single_line in log_line.strip().split('\n'):
                task_time = self._parse_task_time_from_line(single_line)
                if task_time:
                    self.root.after(0, self._set_next_task_time, task_time)
        except json.JSONDecodeError as e:
            print(f"[WS] JSON 解析失败: {e}")
        except Exception as e:
            print(f"[WS] 处理消息异常: {e}")

    def _parse_task_time_from_line(self, line):
        try:
            if len(line) < 19:
                return None
            timestamp_str = line[0:19]
            log_datetime = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")

            start_pos = line.find(self.log_start_marker)
            if start_pos == -1:
                return None
            end_pos = line.find(self.log_end_marker, start_pos)
            if end_pos == -1:
                return None

            time_part_str = line[start_pos + len(self.log_start_marker):end_pos].strip()
            parsed_time = datetime.strptime(time_part_str, "%H:%M:%S").time()
            potential_task_time = datetime.combine(log_datetime.date(), parsed_time)

            if potential_task_time < log_datetime:
                potential_task_time += timedelta(days=1)
            return potential_task_time
        except (ValueError, IndexError):
            return None

    def _set_next_task_time(self, new_time):
        if new_time and new_time > datetime.now():
            if self.next_task_time is None or self.next_task_time <= datetime.now():
                self.notified_new_task_start = False
                self.notified_a = False
                self.notified_b = False
                self.notified_c = False
            self.next_task_time = new_time

    # ---------- 设置窗口 ----------
    def show_settings_window(self, icon=None, item=None):
        if hasattr(self, 'settings_window') and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("设置")
        self.settings_window.resizable(False, True)
        self.settings_window.attributes("-topmost", True)

        canvas = tk.Canvas(self.settings_window, bg=COLOR_FRAME_BG, highlightthickness=0, width=500, height=500)
        scrollbar = tk.Scrollbar(self.settings_window, orient="vertical", command=canvas.yview, width=20)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_FRAME_BG)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1*(event.delta/120)), "units"))
        canvas.bind("<Button-4>", lambda event: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda event: canvas.yview_scroll(1, "units"))

        frame = scrollable_frame

        def toggle_entry_state(entry_widget, checkbox_var):
            if checkbox_var.get():
                entry_widget.config(state="normal")
            else:
                entry_widget.config(state="disabled")

        # 模式选择
        tk.Label(frame, text="运行模式", font=("Segoe UI", 12, "bold"), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=0, column=0, columnspan=3, sticky="w", pady=(10, 5))
        self.mode_var = tk.StringVar(value=self.mode)
        mode_local_rb = tk.Radiobutton(frame, text="读取本地日志文件", variable=self.mode_var, value="local",
                                       bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                                       command=self._toggle_mode_config_visibility)
        mode_web_rb = tk.Radiobutton(frame, text="读取网页的实时日志", variable=self.mode_var, value="web",
                                     bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                                     command=self._toggle_mode_config_visibility)
        mode_local_rb.grid(row=1, column=0, columnspan=3, sticky="w", pady=2, padx=20)
        mode_web_rb.grid(row=2, column=0, columnspan=3, sticky="w", pady=2, padx=20)

        tk.Frame(frame, height=2, bg=COLOR_BUTTON_BG).grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)

        # 本地版配置区域
        self.local_frame = tk.Frame(frame, bg=COLOR_FRAME_BG)
        self.local_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=5)
        tk.Label(self.local_frame, text="日志文件路径:", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", pady=5)
        self.log_path_entry = tk.Entry(self.local_frame, width=40, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.log_path_entry.insert(0, self.log_file_path)
        self.log_path_entry.grid(row=0, column=1, sticky="ew", pady=5, padx=5)
        tk.Button(self.local_frame, text="浏览...", command=self.browse_log_file,
                  bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, activebackground=COLOR_BUTTON_ACTIVE_BG,
                  bd=0, padx=5, pady=2, relief='flat').grid(row=0, column=2, sticky="w", pady=5)

        tk.Label(self.local_frame, text="日志扫描间隔 (秒):", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=1, column=0, sticky="w", pady=5)
        self.scan_interval_entry = tk.Entry(self.local_frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.scan_interval_entry.insert(0, str(self.scan_interval_seconds))
        self.scan_interval_entry.grid(row=1, column=1, sticky="w", pady=5, padx=5)

        # 网址版配置区域
        self.web_frame = tk.Frame(frame, bg=COLOR_FRAME_BG)
        self.web_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=5)
        tk.Label(self.web_frame, text="网址 (Host):", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", pady=5)
        self.ws_host_entry = tk.Entry(self.web_frame, width=30, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.ws_host_entry.insert(0, self.websocket_host)
        self.ws_host_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5, padx=5)

        tk.Label(self.web_frame, text="端口 (Port):", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=1, column=0, sticky="w", pady=5)
        self.ws_port_entry = tk.Entry(self.web_frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.ws_port_entry.insert(0, self.websocket_port)
        self.ws_port_entry.grid(row=1, column=1, sticky="w", pady=5, padx=5)

        tk.Label(self.web_frame, text="Token:", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=2, column=0, sticky="w", pady=5)
        self.ws_token_entry = tk.Entry(self.web_frame, width=30, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT, show="*")
        self.ws_token_entry.insert(0, self.websocket_token)
        self.ws_token_entry.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5, padx=5)

        self._toggle_mode_config_visibility()

        tk.Frame(frame, height=2, bg=COLOR_BUTTON_BG).grid(row=6, column=0, columnspan=3, sticky="ew", pady=10)

        # 通用配置区域
        tk.Label(frame, text="通用设置", font=("Segoe UI", 12, "bold"), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=7, column=0, columnspan=3, sticky="w", pady=(5, 5))

        tk.Label(frame, text="开始闪烁倒计时 (秒):", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=8, column=0, sticky="w", pady=5)
        self.alert_seconds_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.alert_seconds_entry.insert(0, str(self.alert_seconds))
        self.alert_seconds_entry.grid(row=8, column=1, sticky="w", pady=5, padx=5)

        tk.Label(frame, text="UI更新间隔 (毫秒):", font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=9, column=0, sticky="w", pady=5)
        self.update_interval_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.update_interval_entry.insert(0, str(self.update_interval_ms))
        self.update_interval_entry.grid(row=9, column=1, sticky="w", pady=5, padx=5)

        self.start_minimized_var = tk.BooleanVar(value=self.start_minimized)
        tk.Checkbutton(frame, text="启动时最小化到托盘", variable=self.start_minimized_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG).grid(row=10, column=0, columnspan=2, sticky="w", pady=5)

        # 注意：开机自启动选项已移除

        # 通知设置
        tk.Label(frame, text="通知设置", font=("Segoe UI", 12, "bold"), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 5))

        self.notify_enabled_a_var = tk.BooleanVar(value=self.notify_enabled_a)
        tk.Checkbutton(frame, text="启用通知A (分钟)", variable=self.notify_enabled_a_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                       command=lambda: toggle_entry_state(self.notify_a_entry, self.notify_enabled_a_var)).grid(row=12, column=0, sticky="w", pady=5)
        self.notify_a_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.notify_a_entry.insert(0, str(self.notify_minutes_a))
        self.notify_a_entry.grid(row=12, column=1, sticky="w", pady=5, padx=5)
        toggle_entry_state(self.notify_a_entry, self.notify_enabled_a_var)

        self.notify_enabled_b_var = tk.BooleanVar(value=self.notify_enabled_b)
        tk.Checkbutton(frame, text="启用通知B (分钟)", variable=self.notify_enabled_b_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                       command=lambda: toggle_entry_state(self.notify_b_entry, self.notify_enabled_b_var)).grid(row=13, column=0, sticky="w", pady=5)
        self.notify_b_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.notify_b_entry.insert(0, str(self.notify_minutes_b))
        self.notify_b_entry.grid(row=13, column=1, sticky="w", pady=5, padx=5)
        toggle_entry_state(self.notify_b_entry, self.notify_enabled_b_var)

        self.notify_enabled_c_var = tk.BooleanVar(value=self.notify_enabled_c)
        tk.Checkbutton(frame, text="启用通知C (分钟)", variable=self.notify_enabled_c_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                       command=lambda: toggle_entry_state(self.notify_c_entry, self.notify_enabled_c_var)).grid(row=14, column=0, sticky="w", pady=5)
        self.notify_c_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.notify_c_entry.insert(0, str(self.notify_minutes_c))
        self.notify_c_entry.grid(row=14, column=1, sticky="w", pady=5, padx=5)
        toggle_entry_state(self.notify_c_entry, self.notify_enabled_c_var)

        self.notify_enabled_task_completed_var = tk.BooleanVar(value=self.notify_enabled_task_completed)
        tk.Checkbutton(frame, text="启用任务结束/休息开始通知", variable=self.notify_enabled_task_completed_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG).grid(row=15, column=0, columnspan=2, sticky="w", pady=5)

        save_button = tk.Button(frame, text="保存设置", command=self.save_settings,
                                font=("Segoe UI", 12, "bold"),
                                bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG,
                                activebackground=COLOR_BUTTON_ACTIVE_BG,
                                bd=0, padx=10, pady=5, relief='flat')
        save_button.grid(row=16, column=0, columnspan=3, pady=15)

        frame.grid_columnconfigure(1, weight=1)

        self.settings_window.update_idletasks()
        screen_width = self.settings_window.winfo_screenwidth()
        screen_height = self.settings_window.winfo_screenheight()
        settings_width = self.settings_window.winfo_width()
        settings_height = self.settings_window.winfo_height()
        main_x = self.root.winfo_x()
        main_y = self.root.winfo_y()
        main_width = self.root.winfo_width()
        main_height = self.root.winfo_height()
        x = main_x + (main_width // 2) - (settings_width // 2)
        y = main_y + (main_height // 2) - (settings_height // 2)
        if x < 0: x = 0
        if x + settings_width > screen_width: x = max(0, screen_width - settings_width)
        if y < 0: y = 0
        if y + settings_height > screen_height: y = max(0, screen_height - settings_height)
        self.settings_window.geometry(f"+{x}+{y}")

    def _toggle_mode_config_visibility(self):
        if hasattr(self, 'local_frame') and hasattr(self, 'web_frame'):
            if self.mode_var.get() == 'local':
                self.local_frame.grid()
                self.web_frame.grid_remove()
            else:
                self.local_frame.grid_remove()
                self.web_frame.grid()

    def browse_log_file(self):
        file_path = tkinter.filedialog.askopenfilename(
            parent=self.settings_window,
            title="选择日志文件",
            filetypes=[("日志文件", "*.log"), ("所有文件", "*.*")]
        )
        if file_path:
            self.log_path_entry.delete(0, tk.END)
            self.log_path_entry.insert(0, file_path)

    def save_settings(self):
        new_mode = self.mode_var.get()
        settings_changed = False

        if new_mode != self.mode:
            self.mode = new_mode
            self.config['Settings']['mode'] = new_mode
            settings_changed = True

        new_log_path = self.log_path_entry.get().strip()
        if new_log_path != self.log_file_path:
            self.log_file_path = new_log_path
            self.config['Settings']['log_file_path'] = new_log_path
            settings_changed = True
            self.log_file_error_shown = False

        try:
            new_scan_interval = int(self.scan_interval_entry.get().strip())
            if new_scan_interval > 0 and new_scan_interval != self.scan_interval_seconds:
                self.scan_interval_seconds = new_scan_interval
                self.config['Settings']['scan_interval_seconds'] = str(new_scan_interval)
                settings_changed = True
            elif new_scan_interval <= 0:
                tk.messagebox.showwarning("设置错误", "日志扫描间隔必须大于0。", parent=self.settings_window)
                return
        except ValueError:
            tk.messagebox.showwarning("设置错误", "请输入有效的数字作为日志扫描间隔。", parent=self.settings_window)
            return

        new_ws_host = self.ws_host_entry.get().strip()
        new_ws_port = self.ws_port_entry.get().strip()
        new_ws_token = self.ws_token_entry.get().strip()
        if new_ws_host != self.websocket_host:
            self.websocket_host = new_ws_host
            self.config['Settings']['websocket_host'] = new_ws_host
            settings_changed = True
        if new_ws_port != self.websocket_port:
            self.websocket_port = new_ws_port
            self.config['Settings']['websocket_port'] = new_ws_port
            settings_changed = True
        if new_ws_token != self.websocket_token:
            self.websocket_token = new_ws_token
            self.config['Settings']['websocket_token'] = new_ws_token
            settings_changed = True

        try:
            new_alert_seconds = int(self.alert_seconds_entry.get().strip())
            if new_alert_seconds >= 0 and new_alert_seconds != self.alert_seconds:
                self.alert_seconds = new_alert_seconds
                self.config['Settings']['alert_seconds'] = str(new_alert_seconds)
                settings_changed = True
            elif new_alert_seconds < 0:
                tk.messagebox.showwarning("设置错误", "警报时间不能为负数。", parent=self.settings_window)
                return
        except ValueError:
            tk.messagebox.showwarning("设置错误", "请输入有效的数字作为警报时间。", parent=self.settings_window)
            return

        try:
            new_update_interval = int(self.update_interval_entry.get().strip())
            if new_update_interval > 0 and new_update_interval != self.update_interval_ms:
                self.update_interval_ms = new_update_interval
                self.config['Settings']['update_interval_ms'] = str(new_update_interval)
                settings_changed = True
            elif new_update_interval <= 0:
                tk.messagebox.showwarning("设置错误", "UI更新间隔必须大于0。", parent=self.settings_window)
                return
        except ValueError:
            tk.messagebox.showwarning("设置错误", "请输入有效的数字作为UI更新间隔。", parent=self.settings_window)
            return

        if self.start_minimized_var.get() != self.start_minimized:
            self.start_minimized = self.start_minimized_var.get()
            self.config['Settings']['start_minimized'] = str(self.start_minimized)
            settings_changed = True

        # 注意：已移除开机自启动相关代码

        def save_notify_value(entry, attr, config_key):
            nonlocal settings_changed
            try:
                new_val = int(entry.get().strip())
                old_val = getattr(self, attr)
                if new_val != old_val and new_val >= 0:
                    setattr(self, attr, new_val)
                    self.config['Settings'][config_key] = str(new_val)
                    settings_changed = True
                elif new_val < 0:
                    tk.messagebox.showwarning("设置错误", f"{config_key} 不能为负数。", parent=self.settings_window)
                    return False
            except ValueError:
                tk.messagebox.showwarning("设置错误", f"请输入有效的数字作为 {config_key}。", parent=self.settings_window)
                return False
            return True

        if not save_notify_value(self.notify_a_entry, 'notify_minutes_a', 'notify_minutes_a'):
            return
        if not save_notify_value(self.notify_b_entry, 'notify_minutes_b', 'notify_minutes_b'):
            return
        if not save_notify_value(self.notify_c_entry, 'notify_minutes_c', 'notify_minutes_c'):
            return

        def save_notify_enabled(var, attr, config_key):
            nonlocal settings_changed
            new_val = var.get()
            old_val = getattr(self, attr)
            if new_val != old_val:
                setattr(self, attr, new_val)
                self.config['Settings'][config_key] = str(new_val)
                settings_changed = True

        save_notify_enabled(self.notify_enabled_a_var, 'notify_enabled_a', 'notify_enabled_a')
        save_notify_enabled(self.notify_enabled_b_var, 'notify_enabled_b', 'notify_enabled_b')
        save_notify_enabled(self.notify_enabled_c_var, 'notify_enabled_c', 'notify_enabled_c')
        save_notify_enabled(self.notify_enabled_task_completed_var, 'notify_enabled_task_completed', 'notify_enabled_task_completed')

        if settings_changed:
            self.save_config()
            self.settings_window.destroy()
            self.restart_data_source()
            tk.messagebox.showinfo("设置", "设置已成功保存！", parent=self.root)
        else:
            self.settings_window.destroy()

    # ---------- 退出 ----------
    def quit_app(self, icon=None, item=None):
        print("正在退出应用程序...")
        self._stop_data_source()
        if hasattr(self, 'icon') and self.icon:
            self.icon.stop()
        self.root.quit()
        self.root.destroy()
        global _single_instance_socket
        if _single_instance_socket:
            _single_instance_socket.close()
        global _mutex_handle
        if platform.system() == "Windows" and _mutex_handle:
            try:
                win32api.ReleaseMutex(_mutex_handle)
                win32api.CloseHandle(_mutex_handle)
            except:
                pass
        print("应用程序已退出。")
        sys.exit(0)

    # ---------- 倒计时更新 ----------
    def update_countdown(self):
        if not self.root.winfo_exists():
            return

        now = datetime.now()

        if self.mode == 'local' and now >= self.next_scan_time:
            self.scan_log_file()
            self.next_scan_time = now + timedelta(seconds=self.scan_interval_seconds)

        display_text = "任务进行中..."
        is_alert_mode = False

        if self.next_task_time is None or self.next_task_time <= now:
            display_text = "任务进行中..."
            if self.next_task_time is not None and self.next_task_time <= now:
                self.notified_a = False
                self.notified_b = False
                self.notified_c = False
                self.notified_new_task_start = False
                self.next_task_time = None
        else:
            delta = self.next_task_time - now
            total_seconds = int(delta.total_seconds())

            if (self._previous_task_time_state is None or self._previous_task_time_state <= now) and \
               self.notify_enabled_task_completed and not self.notified_new_task_start:
                new_minutes = total_seconds // 60
                message = f"休息约{new_minutes}分钟后进行下次任务" if new_minutes > 0 else "休息时间即将结束"
                self._send_notification(message, title=APP_TITLE)
                self.notified_new_task_start = True
                self.notified_a = False
                self.notified_b = False
                self.notified_c = False

            if self.notify_enabled_a and total_seconds <= self.notify_minutes_a * 60 and total_seconds > self.notify_minutes_a * 60 - self.update_interval_ms / 1000 and not self.notified_a:
                self._send_notification(f"任务即将开始: 剩余{self.notify_minutes_a}分钟!", title=APP_TITLE)
                self.notified_a = True
            if self.notify_enabled_b and total_seconds <= self.notify_minutes_b * 60 and total_seconds > self.notify_minutes_b * 60 - self.update_interval_ms / 1000 and not self.notified_b:
                self._send_notification(f"任务即将开始: 剩余{self.notify_minutes_b}分钟!", title=APP_TITLE)
                self.notified_b = True
            if self.notify_enabled_c and total_seconds <= self.notify_minutes_c * 60 and total_seconds > self.notify_minutes_c * 60 - self.update_interval_ms / 1000 and not self.notified_c:
                self._send_notification(f"任务即将开始: 剩余{self.notify_minutes_c}分钟!", title=APP_TITLE)
                self.notified_c = True

            if delta < timedelta(seconds=self.alert_seconds):
                is_alert_mode = True

            days, remainder = divmod(total_seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            if days > 0:
                display_text = f"{days}天 {hours:02}:{minutes:02}:{seconds:02}"
            else:
                display_text = f"{hours:02}:{minutes:02}:{seconds:02}"

        self._previous_task_time_state = self.next_task_time

        current_bg = COLOR_NORMAL_BG
        if is_alert_mode:
            self.flash_on = not self.flash_on
            if self.flash_on:
                current_bg = COLOR_ALERT_BG

        self.label.config(text=display_text, bg=current_bg)
        self.root.after(self.update_interval_ms, self.update_countdown)


# --- 单实例检查 ---
_mutex_handle = None
_single_instance_socket = None

def check_single_instance():
    global _mutex_handle
    if platform.system() == "Windows" and win32gui and win32con and win32event and win32api and winerror:
        try:
            _mutex_handle = win32event.CreateMutex(None, 1, APP_TITLE + "_Mutex")
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                print("程序已在运行。尝试激活现有窗口。")
                win32api.CloseHandle(_mutex_handle)
                _mutex_handle = None
                try:
                    found_hwnd = win32gui.FindWindow(None, APP_TITLE)
                    if not found_hwnd:
                        top_windows = []
                        win32gui.EnumWindows(lambda hwnd, lp: top_windows.append((hwnd, win32gui.GetWindowText(hwnd))), None)
                        for hwnd, title in top_windows:
                            if APP_TITLE in title:
                                found_hwnd = hwnd
                                break
                    if found_hwnd:
                        if win32gui.IsIconic(found_hwnd):
                            win32gui.ShowWindow(found_hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(found_hwnd)
                except Exception as e:
                    print(f"激活窗口时出错: {e}")
                sys.exit(0)
            else:
                return True
        except Exception as e:
            print(f"互斥体创建失败，回退到套接字检查: {e}")
            return _check_single_instance_socket()
    else:
        return _check_single_instance_socket()

def _check_single_instance_socket():
    global _single_instance_socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
        _single_instance_socket = s
        return True
    except socket.error:
        print("程序已在运行 (套接字检测)。")
        sys.exit(0)

if __name__ == "__main__":
    if check_single_instance():
        root = tk.Tk()
        app = CountdownApp(root)
        root.mainloop()
        if hasattr(app, 'icon') and app.icon:
            app.icon.stop()
        if _single_instance_socket:
            _single_instance_socket.close()
        if platform.system() == "Windows" and _mutex_handle:
            try:
                win32api.ReleaseMutex(_mutex_handle)
                win32api.CloseHandle(_mutex_handle)
            except:
                pass