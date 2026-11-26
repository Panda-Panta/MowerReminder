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
import json
import re
import logging
import urllib.request
import urllib.error
import urllib.parse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('mower_reminder.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 尝试导入 websocket-client（用于网址版）
try:
    import websocket
    has_websocket = True
except ImportError:
    has_websocket = False
    logger.warning("警告: 未安装 websocket-client 库。网址模式将不可用。请运行 'pip install websocket-client'")

# 尝试导入 winotify 用于 Windows 原生通知
try:
    from winotify import Notification, audio
    has_winotify = True
except ImportError:
    has_winotify = False
    logger.warning("警告: 未安装 winotify 库。Windows 原生通知功能将受限。请运行 'pip install winotify'")

# 仅在Windows上导入win32gui、win32con、win32event、win32api和winreg用于窗口操作和互斥体
if platform.system() == "Windows":
    import winreg
    try:
        import win32gui
        import win32con
        import win32event
        import win32api
        import winerror
    except ImportError:
        logger.warning("警告: 未安装 pywin32 库。Windows 单实例、窗口激活功能可能受限。请运行 'pip install pywin32'")
        win32gui = None
        win32con = None
        win32event = None
        win32api = None
        winerror = None
else:
    winreg = None

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
                logger.error(f"设置深色标题栏时出错: {e}")
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
        'auto_start': 'False',
        'log_start_marker': '到',
        'log_end_marker': '开始工作',
        'notify_minutes_a': '5',
        'notify_minutes_b': '2',
        'notify_minutes_c': '1',
        'notify_enabled_a': 'True',
        'notify_enabled_b': 'True',
        'notify_enabled_c': 'True'
    }
}

APP_TITLE = "Mower Reminder"
ICON_FILE = "mower.ico"
SINGLE_INSTANCE_PORT = 12345
AUTO_START_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTO_START_VALUE_NAME = "MowerReminder"

# --- 颜色配置 ---
COLOR_NORMAL_BG = "#1e1e1e"
COLOR_ALERT_BG = "red"
COLOR_TEXT = "white"
COLOR_BUTTON_BG = "#3e3e3e"
COLOR_BUTTON_FG = "white"
COLOR_BUTTON_ACTIVE_BG = "#505050"
COLOR_FRAME_BG = "#2e2e2e"

LOG_TIMESTAMP_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
REST_UNTIL_RE = re.compile(
    r'休息[^，,\n]*[，,]\s*至\s*(?:(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})\s+)?(\d{2}:\d{2}:\d{2})(?:[^「\n]*随后执行「([^」]+)」)?'
)
NEXT_EXEC_UNTIL_RE = re.compile(
    r'至\s*(?:(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})\s+)?(\d{2}:\d{2}:\d{2})\s*[，,]\s*随后执行「([^」]+)」'
)
NEXT_TASK_AT_RE = re.compile(
    r'下次任务在\s*(?:(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})\s+)?(\d{2}:\d{2}:\d{2})'
)

MOWER_STATUS_LABELS = {
    'stopped': 'Mower 已停止',
    'starting': 'Mower 启动中...',
    'recovering': '等待设备恢复...',
    'sleeping': '休眠中...',
    'running': '任务进行中...',
}


def parse_server_datetime(value):
    """解析服务端返回的时间字符串（支持标准格式和 ISO8601，带时区时转为本地无时区时间）。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    normalized = text.replace(',', '.')
    if normalized.endswith('Z'):
        normalized = normalized[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None

def get_resource_path(relative_path):
    try:
        base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

class CountdownApp:
    def __init__(self, root, single_instance):
        self.root = root
        self._single_instance = single_instance
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
        self.auto_start = self.config.getboolean('Settings', 'auto_start')
        self.log_start_marker = self.config.get('Settings', 'log_start_marker')
        self.log_end_marker = self.config.get('Settings', 'log_end_marker')
        self.notify_minutes_a = int(self.config.get('Settings', 'notify_minutes_a'))
        self.notify_minutes_b = int(self.config.get('Settings', 'notify_minutes_b'))
        self.notify_minutes_c = int(self.config.get('Settings', 'notify_minutes_c'))
        self.notify_enabled_a = self.config.getboolean('Settings', 'notify_enabled_a')
        self.notify_enabled_b = self.config.getboolean('Settings', 'notify_enabled_b')
        self.notify_enabled_c = self.config.getboolean('Settings', 'notify_enabled_c')

        # 本地版配置
        self.log_file_path = self.config.get('Settings', 'log_file_path')
        self.scan_interval_seconds = int(self.config.get('Settings', 'scan_interval_seconds'))

        # 网址版配置
        self.websocket_host = self.config.get('Settings', 'websocket_host')
        self.websocket_port = self.config.get('Settings', 'websocket_port')
        self.websocket_token = self.config.get('Settings', 'websocket_token')

        # 状态变量
        self.next_task_time = None
        self.next_task_name = None
        self._previous_task_time_state = None
        self.flash_on = False
        self.is_always_on_top = True

        # 通知标志
        self.notified_a = False
        self.notified_b = False
        self.notified_c = False
        self.notified_new_task_start = False

        # UI 缓存：避免无变化的重绘
        self._prev_display_text = None
        self._prev_bg = None

        # 本地版扫描相关
        self.last_log_file_mtime = 0
        self.next_scan_time = datetime.min
        self.log_file_error_shown = False
        self.log_parse_error_shown = False

        # 网址版 WebSocket 与 HTTP /status 相关
        self.ws_thread = None
        self.ws_app = None
        self.api_thread = None
        self.ws_stop_event = threading.Event()
        self.ws_connected = False
        self.ws_status_text = "正在连接服务器..."
        self.ws_stream_id = None
        self.ws_last_seq = 0
        self.api_connected = False
        self.api_supported = True
        self.api_auth_failed = False
        self.api_status_text = ""
        self.mower_status = None
        self.mower_maintenance_pause = None
        self.status_poll_interval_seconds = 5
        self.has_received_logs = False
        self.has_finished_countdown = False

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
                logger.warning(f"警告: 设置 Tkinter 窗口图标时出错 ({e})")
        else:
            logger.warning("警告: 找不到图标文件 mower.ico。")

        if platform.system() == "Windows" and win32gui and win32con and win32api:
            self.root.after(100, lambda: self._set_windows_taskbar_icon(get_resource_path(self.icon_file_name)))

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width // 2) - (self.window_width // 2)
        y = (screen_height // 2) - (self.window_height // 2)
        self.root.geometry(f"+{x}+{y}")

        # 初始设置置顶
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

        # 添加定期检查和恢复置顶状态的机制
        self.enforce_topmost()

    # ---------- 强制置顶机制 ----------
    def enforce_topmost(self):
        """定期检查并强制保持窗口置顶状态"""
        if self.is_always_on_top:
            try:
                if not self.root.attributes("-topmost"):
                    self.root.attributes("-topmost", True)
                    if platform.system() == "Windows":
                        hwnd = self.root.winfo_id()
                        if hwnd:
                            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
            except:
                pass
        self.root.after(10000, self.enforce_topmost)

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
            logger.error(f"设置任务栏图标时出错: {e}")

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
            logger.error(f"保存配置文件时出错: {e}")
            tk.messagebox.showerror("保存错误", f"保存配置文件时出错:\n{e}", parent=self.root)

    # ---------- 开机自启动 ----------
    def get_startup_command(self):
        if getattr(sys, 'frozen', False):
            return f'"{sys.executable}"'
        script_path = os.path.abspath(sys.argv[0])
        pythonw_path = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        if os.path.exists(pythonw_path):
            return f'"{pythonw_path}" "{script_path}"'
        return f'"{sys.executable}" "{script_path}"'

    def set_auto_start(self, enable):
        if platform.system() != "Windows" or winreg is None:
            return False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTO_START_RUN_KEY, 0, winreg.KEY_SET_VALUE)
            if enable:
                winreg.SetValueEx(key, AUTO_START_VALUE_NAME, 0, winreg.REG_SZ, self.get_startup_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTO_START_VALUE_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
            return True
        except Exception as e:
            logger.error(f"设置开机自启动时出错: {e}")
            return False

    # ---------- 界面控制 ----------
    def toggle_topmost(self):
        self.is_always_on_top = not self.is_always_on_top
        self.root.attributes("-topmost", self.is_always_on_top)
        self.topmost_btn.config(text="📍" if self.is_always_on_top else "📌")
        
        # 如果启用置顶，立即执行强制置顶
        if self.is_always_on_top:
            self.enforce_topmost()

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
            logger.error(f"错误: 任务栏图标文件 '{icon_path}' 未找到。")
        except Exception as e:
            logger.error(f"错误: 加载任务栏图标 '{icon_path}' 时出错: {e}。")
        if image is None:
            image = Image.new('RGB', (64, 64), (255, 255, 255))

        self.icon = Icon('MowerCounter', image, APP_TITLE)
        self.icon_thread = threading.Thread(target=self.icon.run, args=(self.setup_tray_icon,), daemon=True)
        self.icon_thread.start()

    def setup_tray_icon(self, icon):
        icon.visible = True
        icon.menu = Menu(
            item('显示', lambda: self.root.after(0, self.show_window)),
            item('隐藏', lambda: self.root.after(0, self.hide_window)),
            item('设置', lambda: self.root.after(0, self.show_settings_window)),
            item('退出', lambda: self.root.after(0, self.quit_app))
        )
        icon.update_menu()
        def on_tray_click(icon, item):
            if self.root.winfo_ismapped():
                self.root.after(0, self.hide_window)
            else:
                self.root.after(0, self.show_window)
            return 0
        self.icon.activate = on_tray_click

    # ---------- 通知 ----------
    def _set_label_text(self, text):
        """设置标签文字并强制下一轮 update_countdown 刷新"""
        self._prev_display_text = None
        self._prev_bg = None
        self.label.config(text=text)

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
                logger.error(f"winotify 通知失败: {e}")
                if hasattr(self, 'icon') and self.icon:
                    self.icon.notify(message, title=title)
        elif hasattr(self, 'icon') and self.icon:
            self.icon.notify(message, title=title)
        else:
            logger.info(f"通知: {title} - {message}")

    # ---------- 数据源管理 ----------
    def _start_data_source(self):
        if self.mode == 'web':
            self._stop_data_source()
            self.next_task_time = None
            self.next_task_name = None
            self.has_finished_countdown = False
            self.start_web_sources()
        else:  # local
            self._stop_data_source()
            self.next_task_time = None
            self.next_task_name = None
            self._set_label_text("本地模式，等待扫描...")
            self.scan_log_file()

    def _stop_data_source(self):
        self.ws_connected = False
        self.api_connected = False
        self.api_auth_failed = False
        self.mower_status = None
        self.mower_maintenance_pause = None
        self.ws_stop_event.set()
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
            self.ws_app = None
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=1.0)
            if self.ws_thread.is_alive():
                logger.info("[WS] WebSocket 线程未能在 1.0 秒内结束")
        if self.api_thread and self.api_thread.is_alive():
            self.api_thread.join(timeout=1.0)
            if self.api_thread.is_alive():
                logger.info("[API] 状态轮询线程未能在 1.0 秒内结束")
        self.ws_stop_event.clear()
        
    def restart_data_source(self):
        def _restart_worker():
            self._stop_data_source()
            self.root.after(0, self._start_data_source)
        threading.Thread(target=_restart_worker, daemon=True).start()

    # ---------- 本地版：日志文件扫描 ----------
    def _read_log_tail(self, tail_bytes=65536):
        try:
            file_size = os.path.getsize(self.log_file_path)
            with open(self.log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                if file_size <= tail_bytes:
                    f.seek(0)
                else:
                    f.seek(file_size - tail_bytes)
                    f.readline()
                return f.readlines()
        except Exception:
            return []

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
            logger.error(f"获取日志文件修改时间时出错: {e}")
            self.last_log_file_mtime = 0

        now = datetime.now()
        closest_future_time = None
        closest_task_name = None

        try:
            lines = self._read_log_tail()
            for line in reversed(lines):
                potential_task_time, task_name = self._parse_task_info_from_line(line)
                if potential_task_time and potential_task_time > now:
                    if closest_future_time is None or potential_task_time < closest_future_time:
                        closest_future_time = potential_task_time
                        closest_task_name = task_name
            self.next_task_time = closest_future_time
            self.next_task_name = closest_task_name
            self.log_parse_error_shown = False
        except Exception as e:
            if not self.log_parse_error_shown:
                tk.messagebox.showerror("文件读取错误", f"读取日志文件时出错:\n{e}\n请检查文件权限或内容。", parent=self.root)
                self.log_parse_error_shown = True
            self.next_task_time = None
            self.next_task_name = None

    # ---------- 网址版：HTTP /status + WebSocket /log ----------
    def _build_web_endpoints(self):
        """解析配置中的网址、端口和 Token，返回 (http_base, ws_url, effective_token)。"""
        raw = (self.websocket_host or "").strip()
        if not raw:
            return None, None, ""
        if "://" not in raw:
            raw = "http://" + raw
        parsed = urllib.parse.urlparse(raw)
        http_scheme = "https" if parsed.scheme.lower() in ("https", "wss") else "http"
        ws_scheme = "wss" if http_scheme == "https" else "ws"

        hostname = parsed.hostname or ""
        if not hostname:
            return None, None, ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"

        port = parsed.port
        if port is None:
            cfg_port = str(self.websocket_port or "").strip()
            if cfg_port:
                port = cfg_port

        base_path = (parsed.path or "").rstrip("/")
        if base_path.endswith("/log"):
            base_path = base_path[:-4]

        netloc = f"{hostname}:{port}" if port else hostname
        http_base = f"{http_scheme}://{netloc}{base_path}"
        ws_url = f"{ws_scheme}://{netloc}{base_path}/log"

        token = (self.websocket_token or "").strip()
        if not token and parsed.query:
            qs = urllib.parse.parse_qs(parsed.query)
            token = (qs.get("token") or [""])[0].strip()

        return http_base, ws_url, token

    def start_web_sources(self):
        http_base, ws_url, _ = self._build_web_endpoints()
        if not http_base:
            self.ws_connected = False
            self.api_connected = False
            self.ws_status_text = "未配置服务器网址"
            self.api_status_text = "未配置服务器网址"
            self._prev_display_text = None
            return

        self.ws_stop_event.clear()
        self.ws_connected = False
        self.api_connected = False
        self.api_supported = True
        self.api_auth_failed = False
        self.mower_status = None
        self.mower_maintenance_pause = None
        self.ws_stream_id = None
        self.ws_last_seq = 0
        self.has_received_logs = False
        self.ws_status_text = "正在连接服务器..."
        self.api_status_text = "正在获取状态..."
        self._prev_display_text = None

        # 启动 HTTP /status 轮询线程（最新 Mower 权威状态接口）
        self.api_thread = threading.Thread(target=self._status_poll_run, daemon=True)
        self.api_thread.start()

        # 启动 WebSocket /log 实时推送线程（实时日志与旧版兼容）
        if has_websocket:
            self.ws_thread = threading.Thread(target=self._ws_run, daemon=True)
            self.ws_thread.start()
        else:
            self.ws_status_text = "未安装 websocket-client"

    def start_websocket(self):
        """保留兼容入口，统一启动网页数据源。"""
        self.start_web_sources()

    def _http_get_json(self, url, token, timeout=5):
        headers = {}
        if token:
            headers['token'] = token
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            return json.loads(raw)

    def _status_poll_run(self):
        while not self.ws_stop_event.is_set():
            if not self.api_supported:
                return
            http_base, _, token = self._build_web_endpoints()
            if not http_base:
                self.api_connected = False
                self.api_status_text = "未配置服务器网址"
                self.ws_stop_event.wait(self.status_poll_interval_seconds)
                continue

            status_url = f"{http_base}/status"
            try:
                data = self._http_get_json(status_url, token, timeout=5)
                if isinstance(data, dict):
                    # 若服务端处于休眠但 /status 未直接提供时间，兜底查询 /task
                    if (
                        str(data.get('status') or '').lower() == 'sleeping'
                        and not data.get('next_task_time')
                        and data.get('remaining_seconds') is None
                    ):
                        try:
                            tasks = self._http_get_json(f"{http_base}/task", token, timeout=5)
                            if isinstance(tasks, list):
                                data['_fallback_tasks'] = tasks
                        except Exception:
                            pass
                    self.root.after(0, self._apply_status_data, data)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    logger.error(f"[API] /status 鉴权失败 (HTTP {e.code})")
                    self.api_connected = False
                    self.api_auth_failed = True
                    self.api_status_text = "Token 鉴权失败"
                    self._prev_display_text = None
                elif e.code == 404:
                    logger.info("[API] 服务端不支持 /status 接口，回退至纯 WebSocket 日志模式")
                    self.api_connected = False
                    self.api_supported = False
                    return
                else:
                    logger.error(f"[API] /status 请求失败: HTTP {e.code}")
                    self.api_connected = False
                    self.api_status_text = f"状态请求错误: HTTP {e.code}"
                    self._prev_display_text = None
            except Exception as e:
                logger.warning(f"[API] /status 连接失败: {e}")
                self.api_connected = False
                self.api_auth_failed = False
                self.api_status_text = "连接服务器失败，重连中..."
                self._prev_display_text = None

            self.ws_stop_event.wait(self.status_poll_interval_seconds)

    def _apply_status_data(self, data):
        now = datetime.now()
        prev_status = self.mower_status
        prev_pause = self.mower_maintenance_pause

        self.api_connected = True
        self.api_auth_failed = False
        self.api_status_text = ""

        status = str(data.get('status') or '').strip().lower()
        self.mower_status = status or None
        self.mower_maintenance_pause = data.get('maintenance_pause') if status != 'stopped' else None

        if status == 'sleeping':
            target_time = None
            task_name = None
            parsed_next = parse_server_datetime(data.get('next_task_time'))
            rem = data.get('remaining_seconds')
            has_rem = isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0

            if parsed_next and has_rem:
                from_rem = now + timedelta(seconds=float(rem))
                if abs((parsed_next - from_rem).total_seconds()) > 60:
                    # 存在跨时区或较大时钟偏差时，以服务端 remaining_seconds 为准并防抖
                    if (
                        self.next_task_time is not None
                        and abs((from_rem - self.next_task_time).total_seconds()) <= 3
                    ):
                        target_time = self.next_task_time
                    else:
                        target_time = from_rem
                else:
                    target_time = parsed_next
            elif parsed_next:
                target_time = parsed_next
            elif has_rem and rem > 0:
                from_rem = now + timedelta(seconds=float(rem))
                if (
                    self.next_task_time is not None
                    and abs((from_rem - self.next_task_time).total_seconds()) <= 3
                ):
                    target_time = self.next_task_time
                else:
                    target_time = from_rem

            # 若 /status 未提供时间，尝试从 /task 兜底获取最近未来任务
            if target_time is None and isinstance(data.get('_fallback_tasks'), list):
                for item in data['_fallback_tasks']:
                    if not isinstance(item, dict):
                        continue
                    t_dt = parse_server_datetime(item.get('time'))
                    if t_dt and t_dt > now:
                        if target_time is None or t_dt < target_time:
                            target_time = t_dt
                            pres = item.get('presentation')
                            task_name = (
                                (pres.get('title') if isinstance(pres, dict) else None)
                                or item.get('display_name')
                            )

            if target_time and target_time > now:
                self._set_next_task_time(target_time, task_name=task_name)
        elif status == 'stopped':
            scheduled = parse_server_datetime(data.get('scheduled_start_at'))
            if scheduled and scheduled > now:
                self._set_next_task_time(scheduled, task_name="定时启动")
            else:
                self._clear_next_task_time(finished=False)
        elif status in ('running', 'starting', 'recovering'):
            self._clear_next_task_time(finished=(status == 'running'))

        if prev_status != self.mower_status or prev_pause != self.mower_maintenance_pause:
            self._prev_display_text = None

    def _clear_next_task_time(self, finished=False):
        if self.next_task_time is not None:
            self.next_task_time = None
            self.next_task_name = None
            self.notified_a = False
            self.notified_b = False
            self.notified_c = False
            self.notified_new_task_start = False
            self.has_finished_countdown = finished
            self._prev_display_text = None

    def _ws_run(self):
        while not self.ws_stop_event.is_set():
            try:
                _, ws_url, _ = self._build_web_endpoints()
                if not ws_url:
                    self.ws_connected = False
                    self.ws_status_text = "未配置服务器网址"
                    self._prev_display_text = None
                    self.ws_stop_event.wait(5)
                    continue
                logger.info(f"[WS] 尝试连接: {ws_url}")
                self.ws_connected = False
                self.ws_status_text = "正在连接 WebSocket..."
                self._prev_display_text = None

                # 注意：不能添加自定义 Origin / User-Agent 请求头，否则服务端会判定为 Unauthorized
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close
                )
                self.ws_app = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"[WS] 连接异常: {e}")
                self.ws_connected = False
                self.ws_status_text = f"连接异常: {str(e)[:20]}"
                self._prev_display_text = None
            self.ws_app = None
            if not self.ws_stop_event.is_set():
                logger.info("[WS] 10秒后重连...")
                self.ws_connected = False
                self.ws_status_text = "连接断开，10秒后重连..."
                self._prev_display_text = None
                self.ws_stop_event.wait(10)

    def _on_ws_open(self, ws):
        logger.info("[WS] 连接成功！")
        self.ws_connected = True
        self.ws_status_text = "已连接，等待任务信号..."
        self._prev_display_text = None
        _, _, token = self._build_web_endpoints()
        if token:
            try:
                ws.send(json.dumps({'token': token}))
                logger.info("[WS] Token 认证消息已发送")
            except Exception as e:
                logger.error(f"[WS] 发送 Token 失败: {e}")

    def _on_ws_error(self, ws, error):
        logger.error(f"[WS] 错误: {error}")
        self.ws_connected = False
        self.ws_status_text = f"连接错误: {str(error)[:20]}"
        self._prev_display_text = None

    def _on_ws_close(self, ws, close_status_code, close_msg):
        logger.info(f"[WS] 连接关闭: {close_status_code} {close_msg}")
        self.ws_connected = False
        msg_str = close_msg.decode('utf-8', errors='replace') if isinstance(close_msg, bytes) else (close_msg or "")
        msg = f": {msg_str}" if msg_str else ""
        self.ws_status_text = f"连接断开{msg}，重连中..."
        self._prev_display_text = None

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            if not isinstance(data, dict):
                return
            msg_type = data.get('type')
            if msg_type and msg_type != 'log':
                return

            is_snapshot = bool(data.get('snapshot', False))
            stream_id = data.get('stream_id')
            if isinstance(stream_id, str) and stream_id and stream_id != self.ws_stream_id:
                self.ws_stream_id = stream_id
                self.ws_last_seq = 0

            now = datetime.now()
            found_time = None
            found_name = None

            # 优先解析新版结构化 records 数组
            records = data.get('records')
            if isinstance(records, list):
                if records:
                    self.has_received_logs = True
                for rec in reversed(records):
                    if not isinstance(rec, dict):
                        continue
                    seq = rec.get('seq')
                    if isinstance(seq, int) and seq > self.ws_last_seq:
                        self.ws_last_seq = seq
                    if found_time is None:
                        t_dt, t_name = self._parse_task_info_from_record(rec)
                        if t_dt and t_dt > now:
                            found_time = t_dt
                            found_name = t_name

            # 兜底解析传统纯文本 data 字段
            if found_time is None:
                log_line = data.get('data', '')
                if isinstance(log_line, str) and log_line:
                    self.has_received_logs = True
                    for single_line in reversed(log_line.strip().split('\n')):
                        t_dt, t_name = self._parse_task_info_from_line(single_line)
                        if t_dt and t_dt > now:
                            found_time = t_dt
                            found_name = t_name
                            break

            if found_time:
                self.root.after(0, self._handle_ws_task_signal, found_time, found_name, is_snapshot)
        except json.JSONDecodeError as e:
            logger.error(f"[WS] JSON 解析失败: {e}")
        except Exception as e:
            logger.error(f"[WS] 处理消息异常: {e}")

    def _handle_ws_task_signal(self, found_time, found_name, is_snapshot):
        # 若 /status 已确认 Mower 处于非休眠状态（如已停止或运行中），忽略历史 snapshot 中的旧休眠记录
        if (
            is_snapshot
            and self.api_connected
            and self.mower_status in ('stopped', 'running', 'starting', 'recovering')
        ):
            return
        self._set_next_task_time(found_time, task_name=found_name)

    def _parse_task_info_from_record(self, record):
        """解析新版 WS 结构化日志 record，返回 (datetime | None, task_name | None)。"""
        try:
            task = record.get('task')
            if isinstance(task, dict):
                task_name = task.get('title') if isinstance(task.get('title'), str) and task.get('title').strip() else None
                wait_seconds = task.get('wait_seconds')
                if isinstance(wait_seconds, (int, float)) and not isinstance(wait_seconds, bool) and wait_seconds > 0:
                    at_ms = record.get('at')
                    if isinstance(at_ms, (int, float)) and not isinstance(at_ms, bool) and at_ms > 0:
                        base_dt = datetime.fromtimestamp(float(at_ms) / 1000.0)
                    else:
                        base_dt = parse_server_datetime(record.get('t'))
                    if base_dt:
                        return base_dt + timedelta(seconds=float(wait_seconds)), task_name

                header = task.get('header')
                t_str = record.get('t') or ''
                if isinstance(header, str) and header and t_str:
                    dt, parsed_name = self._parse_task_info_from_line(f"{t_str} INFO {header}")
                    if dt:
                        return dt, task_name or parsed_name

            t_str = record.get('t') or ''
            level = record.get('level') or 'INFO'
            msg = record.get('msg') or ''
            if isinstance(msg, str) and msg:
                first_line = msg.split('\n', 1)[0]
                full_line = f"{t_str} {level} {first_line}" if t_str else first_line
                return self._parse_task_info_from_line(full_line)
        except Exception:
            pass
        return None, None

    def _resolve_log_target_time(self, log_datetime, time_part_str, date_part_str=None):
        parsed_time = datetime.strptime(time_part_str.strip(), "%H:%M:%S").time()
        if date_part_str:
            date_part_str = date_part_str.strip()
            if len(date_part_str) == 10:  # YYYY-MM-DD
                parsed_date = datetime.strptime(date_part_str, "%Y-%m-%d").date()
                return datetime.combine(parsed_date, parsed_time)
            if len(date_part_str) == 5:  # MM-DD
                month, day = map(int, date_part_str.split('-'))
                candidate = datetime(log_datetime.year, month, day, parsed_time.hour, parsed_time.minute, parsed_time.second)
                if candidate < log_datetime - timedelta(days=1):
                    candidate = datetime(log_datetime.year + 1, month, day, parsed_time.hour, parsed_time.minute, parsed_time.second)
                return candidate

        potential_task_time = datetime.combine(log_datetime.date(), parsed_time)
        if potential_task_time < log_datetime:
            potential_task_time += timedelta(days=1)
        return potential_task_time

    def _parse_task_info_from_line(self, line):
        """解析单行日志中的下次任务时间与任务名，兼容新旧 Mower 日志格式。"""
        try:
            m = LOG_TIMESTAMP_RE.match(line)
            if not m:
                return None, None
            log_datetime = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")

            # 1. 优先匹配用户配置的起止标记（兼容旧版“到...开始工作”及自定义配置）
            if self.log_start_marker and self.log_end_marker:
                start_pos = line.find(self.log_start_marker)
                if start_pos != -1:
                    end_pos = line.find(self.log_end_marker, start_pos + len(self.log_start_marker))
                    if end_pos != -1:
                        raw_time = line[start_pos + len(self.log_start_marker):end_pos].strip()
                        parts = raw_time.split()
                        if len(parts) == 2:
                            return self._resolve_log_target_time(log_datetime, parts[1], parts[0]), None
                        return self._resolve_log_target_time(log_datetime, raw_time), None

            # 2. 匹配新版 Mower 休眠日志：“休息 1 小时 6 分 15 秒，至 [MM-DD ]22:54:01，随后执行「跑单」”
            m_rest = REST_UNTIL_RE.search(line)
            if m_rest:
                date_part, time_part, task_name = m_rest.group(1), m_rest.group(2), m_rest.group(3)
                return self._resolve_log_target_time(log_datetime, time_part, date_part), task_name

            m_exec = NEXT_EXEC_UNTIL_RE.search(line)
            if m_exec:
                date_part, time_part, task_name = m_exec.group(1), m_exec.group(2), m_exec.group(3)
                return self._resolve_log_target_time(log_datetime, time_part, date_part), task_name

            # 3. 兼容“下次任务在[日期 ]HH:MM:SS”格式
            m_next = NEXT_TASK_AT_RE.search(line)
            if m_next:
                date_part, time_part = m_next.group(1), m_next.group(2)
                return self._resolve_log_target_time(log_datetime, time_part, date_part), None

            return None, None
        except (ValueError, IndexError):
            return None, None

    def _parse_task_time_from_line(self, line):
        task_time, _ = self._parse_task_info_from_line(line)
        return task_time

    def _set_next_task_time(self, new_time, task_name=None):
        if new_time:
            now = datetime.now()
            if new_time > now:
                is_new_cycle = (
                    self.next_task_time is None
                    or self.next_task_time <= now
                    or abs((new_time - self.next_task_time).total_seconds()) > 60
                )
                if is_new_cycle:
                    self.notified_new_task_start = False
                    self.notified_a = False
                    self.notified_b = False
                    self.notified_c = False
                    self.has_finished_countdown = False
                    self.next_task_name = task_name
                elif task_name:
                    self.next_task_name = task_name
                self.next_task_time = new_time
                self._prev_display_text = None

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
        self.settings_window.transient(self.root)
        self.settings_window.grab_set()

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

        self.auto_start_var = tk.BooleanVar(value=self.auto_start)
        tk.Checkbutton(frame, text="开机自启动", variable=self.auto_start_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG).grid(row=11, column=0, columnspan=2, sticky="w", pady=5)

        # 通知设置
        tk.Label(frame, text="通知设置", font=("Segoe UI", 12, "bold"), bg=COLOR_FRAME_BG, fg=COLOR_TEXT).grid(row=12, column=0, columnspan=3, sticky="w", pady=(10, 5))

        self.notify_enabled_a_var = tk.BooleanVar(value=self.notify_enabled_a)
        tk.Checkbutton(frame, text="启用通知A (分钟)", variable=self.notify_enabled_a_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                       command=lambda: toggle_entry_state(self.notify_a_entry, self.notify_enabled_a_var)).grid(row=13, column=0, sticky="w", pady=5)
        self.notify_a_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.notify_a_entry.insert(0, str(self.notify_minutes_a))
        self.notify_a_entry.grid(row=13, column=1, sticky="w", pady=5, padx=5)
        toggle_entry_state(self.notify_a_entry, self.notify_enabled_a_var)

        self.notify_enabled_b_var = tk.BooleanVar(value=self.notify_enabled_b)
        tk.Checkbutton(frame, text="启用通知B (分钟)", variable=self.notify_enabled_b_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                       command=lambda: toggle_entry_state(self.notify_b_entry, self.notify_enabled_b_var)).grid(row=14, column=0, sticky="w", pady=5)
        self.notify_b_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.notify_b_entry.insert(0, str(self.notify_minutes_b))
        self.notify_b_entry.grid(row=14, column=1, sticky="w", pady=5, padx=5)
        toggle_entry_state(self.notify_b_entry, self.notify_enabled_b_var)

        self.notify_enabled_c_var = tk.BooleanVar(value=self.notify_enabled_c)
        tk.Checkbutton(frame, text="启用通知C (分钟)", variable=self.notify_enabled_c_var,
                       font=("Segoe UI", 11), bg=COLOR_FRAME_BG, fg=COLOR_TEXT, selectcolor=COLOR_BUTTON_BG,
                       command=lambda: toggle_entry_state(self.notify_c_entry, self.notify_enabled_c_var)).grid(row=15, column=0, sticky="w", pady=5)
        self.notify_c_entry = tk.Entry(frame, width=10, font=("Segoe UI", 10), bg=COLOR_BUTTON_BG, fg=COLOR_BUTTON_FG, insertbackground=COLOR_TEXT)
        self.notify_c_entry.insert(0, str(self.notify_minutes_c))
        self.notify_c_entry.grid(row=15, column=1, sticky="w", pady=5, padx=5)
        toggle_entry_state(self.notify_c_entry, self.notify_enabled_c_var)

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
        self._settings_changed = False

        if new_mode != self.mode:
            self.mode = new_mode
            self.config['Settings']['mode'] = new_mode
            self._settings_changed = True

        new_log_path = self.log_path_entry.get().strip()
        if new_log_path != self.log_file_path:
            self.log_file_path = new_log_path
            self.config['Settings']['log_file_path'] = new_log_path
            self._settings_changed = True
            self.log_file_error_shown = False

        try:
            new_scan_interval = int(self.scan_interval_entry.get().strip())
            if new_scan_interval > 0 and new_scan_interval != self.scan_interval_seconds:
                self.scan_interval_seconds = new_scan_interval
                self.config['Settings']['scan_interval_seconds'] = str(new_scan_interval)
                self._settings_changed = True
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
            self._settings_changed = True
        if new_ws_port != self.websocket_port:
            self.websocket_port = new_ws_port
            self.config['Settings']['websocket_port'] = new_ws_port
            self._settings_changed = True
        if new_ws_token != self.websocket_token:
            self.websocket_token = new_ws_token
            self.config['Settings']['websocket_token'] = new_ws_token
            self._settings_changed = True

        try:
            new_alert_seconds = int(self.alert_seconds_entry.get().strip())
            if new_alert_seconds >= 0 and new_alert_seconds != self.alert_seconds:
                self.alert_seconds = new_alert_seconds
                self.config['Settings']['alert_seconds'] = str(new_alert_seconds)
                self._settings_changed = True
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
                self._settings_changed = True
            elif new_update_interval <= 0:
                tk.messagebox.showwarning("设置错误", "UI更新间隔必须大于0。", parent=self.settings_window)
                return
        except ValueError:
            tk.messagebox.showwarning("设置错误", "请输入有效的数字作为UI更新间隔。", parent=self.settings_window)
            return

        if self.start_minimized_var.get() != self.start_minimized:
            self.start_minimized = self.start_minimized_var.get()
            self.config['Settings']['start_minimized'] = str(self.start_minimized)
            self._settings_changed = True

        if self.auto_start_var.get() != self.auto_start:
            new_auto_start = self.auto_start_var.get()
            self.auto_start = new_auto_start
            self.config['Settings']['auto_start'] = str(new_auto_start)
            self._settings_changed = True
            if platform.system() == "Windows":
                if not self.set_auto_start(new_auto_start):
                    tk.messagebox.showwarning("设置错误", "设置开机自启动失败，请检查权限。", parent=self.settings_window)
            else:
                tk.messagebox.showwarning("提示", "当前系统不支持开机自启动。", parent=self.settings_window)

        if not self._save_int_setting(self.notify_a_entry, 'notify_minutes_a', 'notify_minutes_a'):
            return
        if not self._save_int_setting(self.notify_b_entry, 'notify_minutes_b', 'notify_minutes_b'):
            return
        if not self._save_int_setting(self.notify_c_entry, 'notify_minutes_c', 'notify_minutes_c'):
            return

        self._save_bool_setting(self.notify_enabled_a_var, 'notify_enabled_a', 'notify_enabled_a')
        self._save_bool_setting(self.notify_enabled_b_var, 'notify_enabled_b', 'notify_enabled_b')
        self._save_bool_setting(self.notify_enabled_c_var, 'notify_enabled_c', 'notify_enabled_c')

        if self._settings_changed:
            self.save_config()
            self.settings_window.destroy()
            self.restart_data_source()
            tk.messagebox.showinfo("设置", "设置已成功保存！", parent=self.root)
        else:
            self.settings_window.destroy()

    def _save_int_setting(self, entry, attr, config_key, min_val=0):
        try:
            new_val = int(entry.get().strip())
        except ValueError:
            tk.messagebox.showwarning("设置错误", f"请输入有效的数字作为 {config_key}。", parent=self.settings_window)
            return False
        if new_val < min_val:
            tk.messagebox.showwarning("设置错误", f"{config_key} 必须大于{min_val}。", parent=self.settings_window)
            return False
        old_val = getattr(self, attr)
        if new_val != old_val:
            setattr(self, attr, new_val)
            self.config['Settings'][config_key] = str(new_val)
            self._settings_changed = True
        return True

    def _save_bool_setting(self, var, attr, config_key):
        new_val = var.get()
        old_val = getattr(self, attr)
        if new_val != old_val:
            setattr(self, attr, new_val)
            self.config['Settings'][config_key] = str(new_val)
            self._settings_changed = True

    # ---------- 退出 ----------
    def quit_app(self, icon=None, item=None):
        logger.info("正在退出应用程序...")
        self._stop_data_source()
        if hasattr(self, 'icon') and self.icon:
            self.icon.stop()
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        self._single_instance.release()
        logger.info("应用程序已退出。")
        sys.exit(0)

    # ---------- 倒计时更新 ----------
    def update_countdown(self):
        if not self.root.winfo_exists():
            return

        now = datetime.now()

        if self.mode == 'local' and now >= self.next_scan_time:
            self.scan_log_file()
            self.next_scan_time = now + timedelta(seconds=self.scan_interval_seconds)

        display_text = ""
        is_alert_mode = False

        if self.next_task_time is None or self.next_task_time <= now:
            if self.next_task_time is not None and self.next_task_time <= now:
                self.notified_a = False
                self.notified_b = False
                self.notified_c = False
                self.notified_new_task_start = False
                self.next_task_time = None
                self.next_task_name = None
                self.has_finished_countdown = True

            if self.mode == 'web':
                if self.api_connected:
                    if self.mower_maintenance_pause:
                        display_text = "维护暂停中..."
                    else:
                        display_text = MOWER_STATUS_LABELS.get(self.mower_status, "任务进行中...")
                elif self.api_auth_failed:
                    display_text = self.api_status_text or "Token 鉴权失败"
                elif self.ws_connected:
                    if not self.has_received_logs and not getattr(self, 'has_finished_countdown', False):
                        display_text = "已连接，等待信号..."
                    else:
                        display_text = "任务进行中..."
                else:
                    display_text = self.api_status_text or self.ws_status_text or "未连接服务器"
            else:
                display_text = "任务进行中..."
        else:
            delta = self.next_task_time - now
            total_seconds = int(delta.total_seconds())
            task_label = f"「{self.next_task_name}」" if self.next_task_name else "下次任务"

            if self._previous_task_time_state is None or self._previous_task_time_state <= now:
                self.notified_a = False
                self.notified_b = False
                self.notified_c = False
                if not self.notified_new_task_start:
                    new_minutes = total_seconds // 60
                    if new_minutes > 0:
                        message = f"休息约{new_minutes}分钟后执行{task_label}"
                    else:
                        message = f"休息时间即将结束，准备执行{task_label}"
                    self._send_notification(message, title=APP_TITLE)
                    self.notified_new_task_start = True

            if self.notify_enabled_a and total_seconds <= self.notify_minutes_a * 60 and not self.notified_a:
                self._send_notification(f"{task_label}即将开始: 剩余{self.notify_minutes_a}分钟!", title=APP_TITLE)
                self.notified_a = True
            if self.notify_enabled_b and total_seconds <= self.notify_minutes_b * 60 and not self.notified_b:
                self._send_notification(f"{task_label}即将开始: 剩余{self.notify_minutes_b}分钟!", title=APP_TITLE)
                self.notified_b = True
            if self.notify_enabled_c and total_seconds <= self.notify_minutes_c * 60 and not self.notified_c:
                self._send_notification(f"{task_label}即将开始: 剩余{self.notify_minutes_c}分钟!", title=APP_TITLE)
                self.notified_c = True

            if total_seconds <= self.alert_seconds:
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

        # 根据文本长度动态适配字体大小，避免长状态文本被截断
        target_font_size = self.font_size if len(display_text) <= 10 else min(self.font_size, 13)
        if (display_text != self._prev_display_text or 
            current_bg != self._prev_bg or 
            getattr(self, '_prev_font_size', None) != target_font_size):
            self.label.config(
                text=display_text, 
                bg=current_bg, 
                font=(self.font_family, target_font_size, self.font_weight)
            )
            self._prev_display_text = display_text
            self._prev_bg = current_bg
            self._prev_font_size = target_font_size
        self.root.after(self.update_interval_ms, self.update_countdown)


# --- 单实例检查 ---
class SingleInstance:
    def __init__(self, app_title, port=12345):
        self._mutex_handle = None
        self._socket = None
        self._app_title = app_title
        self._port = port

    def try_acquire(self):
        if platform.system() == "Windows" and win32gui and win32con and win32event and win32api and winerror:
            try:
                self._mutex_handle = win32event.CreateMutex(None, 1, self._app_title + "_Mutex")
                if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                    logger.info("程序已在运行。尝试激活现有窗口。")
                    win32api.CloseHandle(self._mutex_handle)
                    self._mutex_handle = None
                    self._activate_existing_window()
                    return False
                return True
            except Exception as e:
                logger.error(f"互斥体创建失败，回退到套接字检查: {e}")
                return self._try_socket()
        else:
            return self._try_socket()

    def _try_socket(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', self._port))
            self._socket = s
            return True
        except socket.error:
            logger.info("程序已在运行 (套接字检测)。")
            return False

    def _activate_existing_window(self):
        try:
            found_hwnd = win32gui.FindWindow(None, self._app_title)
            if not found_hwnd:
                top_windows = []
                win32gui.EnumWindows(lambda hwnd, lp: top_windows.append((hwnd, win32gui.GetWindowText(hwnd))), None)
                for hwnd, title in top_windows:
                    if self._app_title in title:
                        found_hwnd = hwnd
                        break
            if found_hwnd:
                if win32gui.IsIconic(found_hwnd):
                    win32gui.ShowWindow(found_hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(found_hwnd)
        except Exception as e:
            logger.info(f"激活窗口时出错: {e}")

    def release(self):
        if self._socket:
            self._socket.close()
            self._socket = None
        if platform.system() == "Windows" and self._mutex_handle:
            try:
                win32api.ReleaseMutex(self._mutex_handle)
                win32api.CloseHandle(self._mutex_handle)
            except:
                pass
            self._mutex_handle = None


if __name__ == "__main__":
    if getattr(sys, 'frozen', False):
        script_dir = os.path.dirname(sys.executable)
    else:
        script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    if script_dir and os.path.isdir(script_dir):
        os.chdir(script_dir)

    single_instance = SingleInstance(APP_TITLE, SINGLE_INSTANCE_PORT)
    if single_instance.try_acquire():
        root = tk.Tk()
        app = CountdownApp(root, single_instance)
        root.mainloop()
        if hasattr(app, 'icon') and app.icon:
            app.icon.stop()
        single_instance.release()
