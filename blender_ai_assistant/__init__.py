bl_info = {
    "name": "Blender AI Assistant",
    "author": "匹宙Plumb",
    "version": (9, 10, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar (N) > AI Assistant  |  3D View > 顶部菜单栏 > 自动化",
    "description": "高性能缓存架构 + 完整功能：多轮对话、视口截图、代码预览/编辑、预设库+预设组+导出导入、工具调用、顶部自动化菜单+组合预设。",
    "category": "3D View",
}

"""
CHANGELOG
---------
v9.10.0 (2026-07-14)
  - NEW: 指令+代码日志 — 若 .blend 已保存，每次对话自动生成文本日志
    - 日志文件: {blend目录}/blender_ai_logs/blender_ai_commands.log
    - 记录内容: 时间戳、输入指令、生成代码、执行状态
    - 触发点: 发送代码生成、运行代码、预设执行、保存预设
  - NEW: 预设自动跟随项目 — 保存 .blend 后预设文件自动关联到项目目录
    - _DataCache 支持检测 blend 文件变化，自动切换预设路径
    - 从 config 切换到项目目录时，自动迁移已有预设数据
  - NEW: 「同步预设到项目」按钮 — 手动将预设文件同步到当前 .blend 目录
    - 预设库、预设组、菜单预设、组合预设 一键同步
  - NEW: 「日志」按钮 — 在资源管理器中打开日志文件目录
  - 新增 Operator: sync_presets_to_project / open_log_folder
  - 新增函数: _get_project_blend_dir / _get_project_log_path / write_command_log

v9.9.0 (2026-07-14)
  - REMOVED: 动画关键帧采集功能（_capture_single, capture_animation_from_selected,
    6个Operator, Panel UI区块）— 使用频率低且易出错，精简插件核心
  - 恢复: bl_info description / CHANGELOG 中移除动画采集相关描述

v9.8.0 (2026-06-02)
  - UX: 指令不再丢失 — send 后不清空 prompt 输入框，可直接修改后重发
  - NEW: 指令历史 — 文件历史（~50条），↑↓ 按钮快速回填之前用过的指令
    - blender_ai_prompt_history.json 自动记录每次发送的指令（去重，最新在前）
  - NEW: 日志复制 — 「复制」按钮一键复制全部操作日志到剪贴板
  - 新增 Operator: copy_logs / prev_prompt / next_prompt / clear_prompt

v9.7.0 (2026-05-25)
  - ARCH: 引入 _DataCache / CacheManager 高性能缓存管理器（来自 vscode14 新框架）
    - 所有预设 IO 改为内存缓存+原子写入，彻底解决 draw() 频繁 IO 导致的界面卡顿
    - 原子写入（先临时文件，再 os.replace）防止崩溃时文件损坏
    - 兼容函数层（load_presets / save_presets / _load_menu_presets 等）保持接口不变
  - MIGRATE: 将 demo13.py（v9.5.0）全部 class 迁移至新框架
    - 所有直接 IO 调用替换为 CacheManager 兼容函数（移除 _menu_presets_path() 等旧接口）
    - execute_code 保留 v9.2.6 完整版（含 _find_view3d_context + temp_override，关键 bug 修复）
    - AIState 使用 demo13 完整版（含 MAX_LOGS/MAX_HISTORY_PAIRS/MAX_AUTO_FIX_ROUNDS/use_tool_mode）
  - KEEP: 保留 demo13 全部功能：截图多模态、Function Calling、错误自动修复、
    预设库+预设组、顶部菜单+组合预设、导出/导入

v9.5.0 (demo13): 预设导出/导入，_save_menu_presets 参数顺序 bug 修复
v9.4.0 (demo13): 菜单挂载点 VIEW3D_MT_editor_menus（多版本兼容）
v9.3.0 (demo13): 顶部自动化菜单、菜单预设、组合预设合并入主线
v9.2.6 (demo11): _find_view3d_context 修复 bpy.ops.transform.translate poll 失败
v9.2.5 (demo11): SYSTEM_PROMPT_CODE 禁止硬编码帧号
v9.2.0 (demo11): 预设组系统（预设库+预设组两层架构）
v9.0.0 (demo11): 视口截图多模态、Function Calling 14工具、错误自动修复(3轮)
"""

import bpy
import json
import os
import re
import uuid
import threading
import traceback
import urllib.request
import urllib.error
import base64
import tempfile
import ssl
import datetime
import mathutils
from bpy_extras.io_utils import ImportHelper, ExportHelper
from typing import List, Dict, Any, Optional, Tuple


# ============================================================
# 全局常量
# ============================================================
AI_TEXT_NAME = "AI_Generated_Code"
PRESETS_FILENAME = "blender_ai_assistant_presets.json"
GROUPS_FILENAME = "blender_ai_preset_groups.json"
MENU_PRESETS_FILENAME = "blender_ai_menu_presets.json"
COMBO_PRESETS_FILENAME = "blender_ai_combo_presets.json"

CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)```", re.DOTALL)

COMMAND_LOG_FILENAME = "blender_ai_commands.log"


def _get_project_blend_dir():
    """返回 .blend 文件所在目录，未保存则返回 None"""
    if bpy.data.filepath:
        d = os.path.dirname(bpy.data.filepath)
        if os.path.isdir(d):
            return d
    return None


def _get_project_log_path():
    """返回项目目录下的日志文件路径，未保存则返回 None"""
    blend_dir = _get_project_blend_dir()
    if not blend_dir:
        return None
    log_dir = os.path.join(blend_dir, "blender_ai_logs")
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, COMMAND_LOG_FILENAME)


def write_command_log(prompt="", code="", status="OK"):
    """将指令和代码写入项目目录的文本日志文件（仅在blend已保存时生效）"""
    log_path = _get_project_log_path()
    if not log_path:
        return
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"[{now}] ----------------------------------------\n")
            if prompt:
                f.write(f"指令: {prompt}\n")
            if code:
                f.write(f"代码:\n{code}\n")
            if status:
                f.write(f"状态: {status}\n")
            f.write("\n")
    except Exception as e:
        print(f"[AI Log] 写入日志失败: {e}")

SYSTEM_PROMPT_CODE = (
    "You are an expert Blender Python (bpy) assistant.\n"
    "- Output ONE Python code block wrapped in ```python ... ```.\n"
    "- The code must be complete and directly executable in Blender.\n"
    "- Prefer data-level access (bpy.data, scene.*) over bpy.ops when possible.\n"
    "- Never call destructive file/session ops (wm.save_*, wm.quit_*, wm.open_*).\n"
    "- Keep comments minimal; focus on the user's request.\n"
    "- Use the provided scene context (active object, selection, mode) when relevant.\n"
    "- CRITICAL: NEVER hardcode frame numbers. Always use bpy.context.scene.frame_current\n"
    "  for the current timeline position. The scene context provides frame info for\n"
    "  reference only — do NOT copy the frame number into your code."
)

MODEL_PRESETS = [
    ('deepseek-v4-flash',  "DeepSeek V4 Flash (默认)",    "速度快/成本低，日常首选"),
    ('deepseek-v4-pro',    "DeepSeek V4 Pro",            "复杂推理/代码，质量优先"),
    ('deepseek-chat',      "DeepSeek V3 (旧兼容)",       "别名即将弃用(2026-07)"),
    ('deepseek-reasoner',  "DeepSeek R1 (旧兼容)",       "别名即将弃用(2026-07)"),
    ('gpt-4o',             "OpenAI GPT-4o (多模态)",     ""),
    ('gpt-4o-mini',        "OpenAI GPT-4o-mini (多模态)", ""),
    ('claude-3-5-haiku',   "Claude 3.5 Haiku (多模态)", "Anthropic"),
    ('CUSTOM',             "自定义...",                   "手动输入模型名"),
]

MULTIMODAL_MODELS = {
    'gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo',
    'claude-3-5-sonnet', 'claude-3-5-haiku', 'claude-3-opus',
    'claude-3-sonnet', 'claude-3-haiku',
    'gemini-pro-vision', 'gemini-1.5-pro', 'gemini-1.5-flash',
}

CLAUDE_MODELS = {
    'claude-3-5-sonnet', 'claude-3-5-haiku', 'claude-3-opus',
    'claude-3-sonnet', 'claude-3-haiku',
}


# ============================================================
# 高性能缓存管理器（v9.7.0 新架构）
# 解决 draw() 中频繁 IO 导致界面卡顿的问题
# ============================================================
class _DataCache:
    def __init__(self):
        self.presets: List[Dict] = []
        self.groups: Dict = {"groups": [], "active_group_id": None}
        self.menu_presets: List[Dict] = []
        self.combo_presets: List[Dict] = []
        self._preset_path: str = ""
        self._group_path: str = ""
        self._menu_path: str = ""
        self._combo_path: str = ""
        self._initialized = False
        self._last_filepath: str = ""  # 跟踪 blend 文件变化，支持自动切换项目目录

    def init_paths(self):
        """延迟初始化路径。当 blend 文件从未保存→已保存 或 切换到不同文件时，
        自动从 config 目录迁移/切换到项目目录。"""
        current_filepath = bpy.data.filepath or ""

        # 如果 blend 文件变了（从未保存→保存、或切换到新文件），重新初始化
        if self._initialized and current_filepath != self._last_filepath:
            self._initialized = False

        if self._initialized:
            return

        blend_dir = os.path.dirname(current_filepath) if current_filepath else None
        config_dir = bpy.utils.user_resource('CONFIG')

        # 记录旧路径（用于迁移预设数据）
        old_preset_path = self._preset_path

        if blend_dir and os.path.isdir(blend_dir):
            self._preset_path = os.path.join(blend_dir, PRESETS_FILENAME)
            self._group_path  = os.path.join(blend_dir, GROUPS_FILENAME)
            self._menu_path   = os.path.join(blend_dir, MENU_PRESETS_FILENAME)
            self._combo_path  = os.path.join(blend_dir, COMBO_PRESETS_FILENAME)
        else:
            self._preset_path = os.path.join(config_dir, PRESETS_FILENAME)
            self._group_path  = os.path.join(config_dir, GROUPS_FILENAME)
            self._menu_path   = os.path.join(config_dir, MENU_PRESETS_FILENAME)
            self._combo_path  = os.path.join(config_dir, COMBO_PRESETS_FILENAME)

        self._last_filepath = current_filepath
        self._initialized = True

        # 如果从 config 目录迁移到项目目录，且项目目录下没有预设文件，
        # 则自动把 config 目录的预设复制过来
        if blend_dir and old_preset_path and os.path.exists(old_preset_path) and not os.path.exists(self._preset_path):
            import shutil
            try:
                shutil.copy2(old_preset_path, self._preset_path)
                if os.path.exists(os.path.join(config_dir, GROUPS_FILENAME)) and not os.path.exists(self._group_path):
                    shutil.copy2(os.path.join(config_dir, GROUPS_FILENAME), self._group_path)
                if os.path.exists(os.path.join(config_dir, MENU_PRESETS_FILENAME)) and not os.path.exists(self._menu_path):
                    shutil.copy2(os.path.join(config_dir, MENU_PRESETS_FILENAME), self._menu_path)
                if os.path.exists(os.path.join(config_dir, COMBO_PRESETS_FILENAME)) and not os.path.exists(self._combo_path):
                    shutil.copy2(os.path.join(config_dir, COMBO_PRESETS_FILENAME), self._combo_path)
                print(f"[AI Cache] 预设已自动迁移到项目目录: {blend_dir}")
            except Exception as e:
                print(f"[AI Cache] 迁移预设失败: {e}")

        self.reload_all()

    def reload_all(self):
        """从磁盘重新加载所有缓存（初始化或强制刷新时调用）"""
        self.presets       = self._load_json(self._preset_path, [])
        self.groups        = self._load_json(self._group_path,  {"groups": [], "active_group_id": None})
        self.menu_presets  = self._load_json(self._menu_path,   [])
        self.combo_presets = self._load_json(self._combo_path,  [])

    def _load_json(self, path: str, default: Any) -> Any:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[AI Cache] 读取失败 {path}: {e}")
        return default

    def _save_json_atomic(self, path: str, data: Any):
        """原子写入：先写临时文件，再 os.replace，防止崩溃导致文件损坏"""
        try:
            dir_name = os.path.dirname(path)
            if dir_name and not os.path.exists(dir_name):
                os.makedirs(dir_name, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name or ".", suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except PermissionError:
                        pass
                os.replace(tmp_path, path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except Exception as e:
            print(f"[AI Cache] 写入失败 {path}: {e}")
            raise

    # --- Presets ---
    def get_presets(self) -> List[Dict]:
        self.init_paths()
        return self.presets

    def save_presets(self, presets: List[Dict]):
        self.init_paths()
        self.presets = presets
        self._save_json_atomic(self._preset_path, presets)

    # --- Groups ---
    def get_groups(self) -> Dict:
        self.init_paths()
        return self.groups

    def save_groups(self, groups_data: Dict):
        self.init_paths()
        self.groups = groups_data
        self._save_json_atomic(self._group_path, groups_data)

    # --- Menu Presets ---
    def get_menu_presets(self) -> List[Dict]:
        self.init_paths()
        return self.menu_presets

    def save_menu_presets(self, items: List[Dict]):
        self.init_paths()
        self.menu_presets = items
        self._save_json_atomic(self._menu_path, items)

    # --- Combo Presets ---
    def get_combo_presets(self) -> List[Dict]:
        self.init_paths()
        return self.combo_presets

    def save_combo_presets(self, items: List[Dict]):
        self.init_paths()
        self.combo_presets = items
        self._save_json_atomic(self._combo_path, items)


# 全局缓存实例
CacheManager = _DataCache()

# 兼容旧代码的接口函数（Operators 内部无需修改）
def load_presets()                      : return CacheManager.get_presets()
def save_presets(data)                  : CacheManager.save_presets(data); return True
def load_groups()                       : return CacheManager.get_groups()
def save_groups(data)                   : CacheManager.save_groups(data); return True
def _load_menu_presets(path=None)       : return CacheManager.get_menu_presets()
def _save_menu_presets(items, path=None): CacheManager.save_menu_presets(items); return True
def _load_combo_presets()               : return CacheManager.get_combo_presets()
def _save_combo_presets(items)          : CacheManager.save_combo_presets(items); return True


def get_active_group():
    """获取当前激活的预设组（返回 dict 或 None）"""
    data = load_groups()
    aid = data["active_group_id"]
    for g in data["groups"]:
        if g.get("id") == aid:
            return g
    return None


# ============================================================
# 指令历史（v9.8.0 — Prompt 复用）
# ============================================================
_MAX_PROMPT_HISTORY = 50
_PROMPT_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "blender_ai_prompt_history.json",
)


def _load_prompt_history() -> List[str]:
    """加载指令历史（最多 50 条，最新在前）"""
    try:
        with open(_PROMPT_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [s for s in data if isinstance(s, str)][:_MAX_PROMPT_HISTORY]
    except Exception:
        pass
    return []


def _save_prompt_history(history: List[str]):
    """保存指令历史到文件"""
    try:
        with open(_PROMPT_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[:_MAX_PROMPT_HISTORY], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _add_to_prompt_history(prompt: str):
    """添加一条指令到历史（去重，最新在前）"""
    if not prompt.strip():
        return
    history = _load_prompt_history()
    # 去重：如果已存在相同指令，先移除旧条目
    history = [h for h in history if h.strip() != prompt.strip()]
    history.insert(0, prompt.strip())
    _save_prompt_history(history)


# ============================================================
# 全局状态（完整版，来自 demo13）
# ============================================================
class AIState:
    logs = []
    pending_code = ""
    pending_prompt = ""
    last_ok_code = ""
    last_ok_prompt = ""
    conversation = []
    is_processing = False
    cancel_flag = False
    use_tool_mode = False

    MAX_LOGS = 300
    MAX_HISTORY_PAIRS = 8
    MAX_AUTO_FIX_ROUNDS = 3

    @classmethod
    def log(cls, text):
        for line in str(text).splitlines() or [""]:
            cls.logs.append(line)
        if len(cls.logs) > cls.MAX_LOGS:
            cls.logs = cls.logs[-cls.MAX_LOGS:]

    @classmethod
    def clear_logs(cls):
        cls.logs = []

    @classmethod
    def push_message(cls, role, content):
        cls.conversation.append({"role": role, "content": content})
        limit = cls.MAX_HISTORY_PAIRS * 2
        if len(cls.conversation) > limit:
            cls.conversation = cls.conversation[-limit:]

    @classmethod
    def clear_history(cls):
        cls.conversation = []

    @classmethod
    def reset_pending(cls):
        cls.pending_code = ""
        cls.pending_prompt = ""

    @classmethod
    def reset_success(cls):
        cls.last_ok_code = ""
        cls.last_ok_prompt = ""


def _push_pair(user_content, reply):
    """安全地将 user+assistant 消息对写入对话历史（线程安全，仅由 run_in_main_thread 调用）"""
    AIState.push_message("user", user_content)
    AIState.push_message("assistant", reply)


# ============================================================
# 线程 / UI
# ============================================================
def run_in_main_thread(fn):
    def _wrap():
        try:
            fn()
        except Exception:
            traceback.print_exc()
        return None
    bpy.app.timers.register(_wrap, first_interval=0.01)


def redraw_ui():
    wm = bpy.context.window_manager
    if not wm:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ============================================================
# Text block
# ============================================================
def sync_code_to_text(code):
    t = bpy.data.texts.get(AI_TEXT_NAME)
    if t is None:
        t = bpy.data.texts.new(AI_TEXT_NAME)
    t.clear()
    t.write(code or "")
    return t


def read_code_from_text():
    t = bpy.data.texts.get(AI_TEXT_NAME)
    return t.as_string() if t else ""


def clear_code_text():
    t = bpy.data.texts.get(AI_TEXT_NAME)
    if t is not None:
        t.clear()


# ============================================================
# 视口截图（v9.0.0）
# ============================================================
def capture_viewport_screenshot(max_size=1024):
    path = os.path.join(tempfile.gettempdir(), "blender_ai_viewport_temp.png")
    scene = bpy.context.scene
    orig_fp = scene.render.filepath
    orig_fmt = scene.render.image_settings.file_format
    try:
        scene.render.image_settings.file_format = 'PNG'
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                w, h = area.width, area.height
                scale = min(1.0, max_size / max(w, h))
                scene.render.resolution_x = int(w * scale)
                scene.render.resolution_y = int(h * scale)
                scene.render.resolution_percentage = 100
                scene.render.filepath = path
                bpy.ops.render.opengl(write_still=True, view_context=True)
                break
        else:
            return None
        if not os.path.exists(path) or os.path.getsize(path) < 100:
            return None
        with open(path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        AIState.log(f"[Screenshot] 截图失败: {e}")
        return None
    finally:
        scene.render.filepath = orig_fp
        scene.render.image_settings.file_format = orig_fmt
        try:
            os.remove(path)
        except OSError:
            pass


# ============================================================
# 凭据 / 上下文 / 解析
# ============================================================
def get_credentials(context):
    scene = context.scene
    api_key  = (scene.ai_api_key   or "").strip()
    base_url = (scene.ai_base_url  or "").strip()
    model    = (scene.ai_model_name or "").strip()
    prefs_entry = context.preferences.addons.get(__name__)
    if prefs_entry is not None:
        p = prefs_entry.preferences
        if not api_key:  api_key  = (p.api_key    or "").strip()
        if not base_url: base_url = (p.base_url   or "").strip()
        if not model:    model    = (p.model_name or "").strip()
    base_url = base_url or "https://api.deepseek.com"
    model    = model    or "deepseek-v4-flash"
    ssl_verify = getattr(scene, "ai_ssl_verify", True)
    return api_key, base_url, model, ssl_verify


def get_scene_context(context):
    scene  = context.scene
    active = context.active_object
    selected = [o.name for o in context.selected_objects]
    parts = [
        f"Blender: {bpy.app.version_string}",
        f"Mode: {context.mode}",
        f"Selected ({len(selected)}): {selected[:6]}{'...' if len(selected) > 6 else ''}",
        f"Active: {active.name if active else 'None'} ({active.type if active else 'N/A'})",
        f"Frame: {scene.frame_current}/[{scene.frame_start}-{scene.frame_end}]",
        f"Scene objects: {len(scene.objects)}",
    ]
    if active and active.type == 'MESH' and active.data:
        m = active.data
        parts.append(f"Active mesh: {len(m.vertices)} verts, {len(m.polygons)} faces")
    if active:
        parts.append(f"Location: ({active.location.x:.2f}, {active.location.y:.2f}, {active.location.z:.2f})")
        parts.append(f"Rotation: ({active.rotation_euler.x:.2f}, {active.rotation_euler.y:.2f}, {active.rotation_euler.z:.2f})")
        parts.append(f"Scale: ({active.scale.x:.2f}, {active.scale.y:.2f}, {active.scale.z:.2f})")
        if active.type == 'MESH' and active.data.materials:
            mats = [m.name for m in active.data.materials if m]
            parts.append(f"Materials: {mats[:4]}")
        if active.modifiers:
            parts.append(f"Modifiers: {[m.name for m in active.modifiers]}")
    return "\n".join(parts)


def extract_code(reply):
    m = CODE_BLOCK_RE.search(reply or "")
    return m.group(1).strip() if m else None


def extract_tool_calls(reply):
    m = TOOL_BLOCK_RE.search(reply or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return None


# ============================================================
# Function Calling 工具层（v9.0.0）
# ============================================================
TOOL_DEFINITIONS = [
    {"name": "create_cube",     "desc": "创建立方体",           "params": {"size": "float=1.0", "location": "[x,y,z]=[0,0,0]", "name": "str=optional"}},
    {"name": "create_sphere",   "desc": "创建球体",             "params": {"radius": "float=1.0", "location": "[x,y,z]=[0,0,0]", "segments": "int=32", "name": "str=optional"}},
    {"name": "create_plane",    "desc": "创建平面",             "params": {"size": "float=2.0", "location": "[x,y,z]=[0,0,0]", "name": "str=optional"}},
    {"name": "create_cylinder", "desc": "创建圆柱体",           "params": {"radius": "float=1.0", "depth": "float=2.0", "location": "[x,y,z]=[0,0,0]", "segments": "int=32", "name": "str=optional"}},
    {"name": "create_empty",    "desc": "创建空物体",           "params": {"type": "str='PLAIN_AXES'", "location": "[x,y,z]=[0,0,0]", "name": "str=optional"}},
    {"name": "set_location",    "desc": "设置选中物体位置",     "params": {"x": "float", "y": "float", "z": "float"}},
    {"name": "set_rotation",    "desc": "设置旋转(欧拉角度制)", "params": {"x": "float", "y": "float", "z": "float"}},
    {"name": "set_scale",       "desc": "设置缩放",             "params": {"x": "float", "y": "float", "z": "float"}},
    {"name": "set_parent",      "desc": "设置父子关系",         "params": {}},
    {"name": "add_modifier",    "desc": "添加修改器",           "params": {"type": "str=SUBSURF/BEVEL/ARRAY/MIRROR/SOLIDIFY/BOOLEAN/SIMPLE_DEFORM", "params": "dict=optional"}},
    {"name": "apply_scale",     "desc": "应用缩放",             "params": {}},
    {"name": "set_shading",     "desc": "设置视图着色模式",     "params": {"mode": "str=WIREFRAME/SOLID/MATERIAL/RENDERED"}},
    {"name": "keyframe_insert", "desc": "插入关键帧",           "params": {"channels": "str=loc/rot/scale/locrot/locrotscale"}},
    {"name": "duplicate",       "desc": "复制物体",             "params": {"offset": "[dx,dy,dz]=[0,0,0]", "linked": "bool=False"}},
]


def _build_system_prompt(use_tools=False):
    if not use_tools:
        return SYSTEM_PROMPT_CODE
    tool_descs = ["  - {name}: {desc} ({params})".format(
        name=t['name'], desc=t['desc'],
        params=", ".join(f"{k}={v}" for k, v in t["params"].items())
    ) for t in TOOL_DEFINITIONS]
    return (
        "You are an expert Blender automation assistant.\n\n"
        "You have TWO ways to fulfill the user's request:\n\n"
        "1. **Tool Call** (preferred for simple operations):\n"
        '   ```tool\n   {"tool": "tool_name", "params": {...}}\n   ```\n'
        "   For batch: use an array in the ```tool``` block.\n\n"
        "2. **Python Code** (for complex logic or if no tool fits):\n"
        "   Output ```python ... ``` block as usual.\n\n"
        "Available tools:\n" + "\n".join(tool_descs) + "\n\n"
        "Rules: Use tools for simple ops; Python for complex. Prefer tools over code."
    )


def execute_tool_call(tool_name, params):
    scene = bpy.context.scene
    active = bpy.context.active_object
    try:
        if tool_name == "create_cube":
            bpy.ops.mesh.primitive_cube_add(size=float(params.get("size", 1.0)), location=params.get("location", [0,0,0]))
            if params.get("name"): bpy.context.active_object.name = params["name"]
            return True, f"创建立方体: {bpy.context.active_object.name}"
        elif tool_name == "create_sphere":
            segs = int(params.get("segments", 32))
            bpy.ops.mesh.primitive_uv_sphere_add(radius=float(params.get("radius", 1.0)), location=params.get("location", [0,0,0]), segments=segs, ring_count=segs//2)
            if params.get("name"): bpy.context.active_object.name = params["name"]
            return True, f"创建球体: {bpy.context.active_object.name}"
        elif tool_name == "create_plane":
            bpy.ops.mesh.primitive_plane_add(size=float(params.get("size", 2.0)), location=params.get("location", [0,0,0]))
            if params.get("name"): bpy.context.active_object.name = params["name"]
            return True, f"创建平面: {bpy.context.active_object.name}"
        elif tool_name == "create_cylinder":
            segs = int(params.get("segments", 32))
            bpy.ops.mesh.primitive_cylinder_add(radius=float(params.get("radius", 1.0)), depth=float(params.get("depth", 2.0)), location=params.get("location", [0,0,0]), vertices=segs)
            if params.get("name"): bpy.context.active_object.name = params["name"]
            return True, f"创建圆柱体: {bpy.context.active_object.name}"
        elif tool_name == "create_empty":
            bpy.ops.object.empty_add(type=params.get("type", "PLAIN_AXES"), location=params.get("location", [0,0,0]))
            if params.get("name"): bpy.context.active_object.name = params["name"]
            return True, f"创建空物体: {bpy.context.active_object.name}"
        elif tool_name == "set_location":
            if not active: return False, "无选中物体"
            active.location = (float(params["x"]), float(params["y"]), float(params["z"]))
            return True, f"设置 {active.name} 位置"
        elif tool_name == "set_rotation":
            if not active: return False, "无选中物体"
            import math
            active.rotation_euler = (math.radians(float(params["x"])), math.radians(float(params["y"])), math.radians(float(params["z"])))
            return True, f"设置 {active.name} 旋转"
        elif tool_name == "set_scale":
            if not active: return False, "无选中物体"
            active.scale = (float(params["x"]), float(params["y"]), float(params["z"]))
            return True, f"设置 {active.name} 缩放"
        elif tool_name == "set_parent":
            sel = list(bpy.context.selected_objects)
            if len(sel) < 2: return False, "需要至少选中2个物体"
            parent = bpy.context.active_object
            for obj in sel:
                if obj != parent: obj.parent = parent
            return True, "设置父子关系"
        elif tool_name == "add_modifier":
            if not active: return False, "无选中物体"
            mod_type = params.get("type", "SUBSURF")
            mod = active.modifiers.new(name=mod_type, type=mod_type)
            for k, v in params.get("params", {}).items():
                if hasattr(mod, k): setattr(mod, k, v)
            return True, f"添加 {mod_type} 修改器"
        elif tool_name == "apply_scale":
            if not active: return False, "无选中物体"
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            return True, f"已应用 {active.name} 缩放"
        elif tool_name == "set_shading":
            mode = params.get("mode", "SOLID")
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D': space.shading.type = mode
                    area.tag_redraw()
            return True, f"着色模式: {mode}"
        elif tool_name == "keyframe_insert":
            if not active: return False, "无选中物体"
            channels = params.get("channels", "locrotscale")
            frame = scene.frame_current
            results = []
            if "loc"   in channels: active.keyframe_insert(data_path="location");         results.append("loc")
            if "rot"   in channels: active.keyframe_insert(data_path="rotation_euler");   results.append("rot")
            if "scale" in channels: active.keyframe_insert(data_path="scale");            results.append("scale")
            return True, f"K帧 {active.name} ({'+'.join(results)}) @ 帧 {frame}"
        elif tool_name == "duplicate":
            bpy.ops.object.duplicate_move(
                OBJECT_OT_duplicate={"linked": bool(params.get("linked", False))},
                TRANSFORM_OT_translate={"value": tuple(params.get("offset", [0,0,0]))},
            )
            return True, "复制物体"
        else:
            return False, f"未知工具: {tool_name}"
    except Exception as e:
        return False, f"工具执行错误: {type(e).__name__}: {e}"


# ============================================================
# AI 调用
# ============================================================
def build_user_message(context, prompt, attach_screenshot=False):
    scene_ctx = get_scene_context(context)
    text_content = f"Scene context:\n{scene_ctx}\n\nUser request:\n{prompt}"
    if not attach_screenshot:
        return text_content, False
    _, _, model, _ = get_credentials(context)
    if model not in MULTIMODAL_MODELS:
        return text_content, False
    b64_url = capture_viewport_screenshot(max_size=512)
    if not b64_url:
        return text_content, False
    if model in CLAUDE_MODELS:
        content = [
            {"type": "text", "text": text_content},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64_url.split(",", 1)[1]}},
        ]
    else:
        content = [
            {"type": "text", "text": text_content},
            {"type": "image_url", "image_url": {"url": b64_url, "detail": "low"}},
        ]
    return content, True


def call_ai_api_async(context, prompt, on_done, auto_fix_round=0):
    api_key, base_url, model, ssl_verify = get_credentials(context)
    if not api_key:
        run_in_main_thread(lambda: on_done(None, "ERROR: 请先填写 API Key"))
        return
    use_tools = AIState.use_tool_mode
    sys_prompt = _build_system_prompt(use_tools)
    user_content, is_multimodal = build_user_message(context, prompt, context.scene.ai_attach_screenshot)
    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(AIState.conversation)
    messages.append({"role": "user", "content": user_content})
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({"model": model, "messages": messages, "max_tokens": 2048 if not is_multimodal else 4096, "temperature": 0.2}).encode("utf-8")

    def _worker():
        try:
            if AIState.cancel_flag:
                run_in_main_thread(lambda: on_done(None, "Cancelled"))
                return
            req = urllib.request.Request(url, data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
            ssl_ctx = None
            if not ssl_verify:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            kwargs = {"timeout": 180}
            if ssl_ctx: kwargs["context"] = ssl_ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                if AIState.cancel_flag:
                    run_in_main_thread(lambda: on_done(None, "Cancelled"))
                    return
                result = json.loads(resp.read().decode("utf-8"))
            reply = result["choices"][0]["message"]["content"]
            run_in_main_thread(lambda u=user_content, r=reply: _push_pair(u, r))
            if use_tools:
                tool_calls = extract_tool_calls(reply)
                if tool_calls:
                    run_in_main_thread(lambda tc=tool_calls: on_done(tc, None))
                    return
            code = extract_code(reply)
            if code:
                run_in_main_thread(lambda c=code: on_done(c, None))
            else:
                run_in_main_thread(lambda r=reply: (
                    sync_code_to_text(r),
                    AIState.log(f"[AI] 文本回复 ({len(r)} 字符)，已写入 Text Editor"),
                    redraw_ui()))
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode("utf-8")[:300]
            except Exception: pass
            msg = f"HTTP {e.code} {e.reason}: {body}"
            run_in_main_thread(lambda m=msg: on_done(None, m))
        except urllib.error.URLError as e:
            msg = f"网络错误: {e.reason}"
            run_in_main_thread(lambda m=msg: on_done(None, m))
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            print(tb)
            msg = f"Error: {type(e).__name__}: {e}"
            run_in_main_thread(lambda m=msg: on_done(None, m))

    threading.Thread(target=_worker, daemon=True).start()


# ============================================================
# 代码执行 + _find_view3d_context（v9.2.6 关键修复，必须保留）
# ============================================================
_group_step_ok = None


def _find_view3d_context():
    """查找 3D View WINDOW region。
    v9.2.6 修复：面板按钮触发时 bpy.context.region 是 UI 区域，
    bpy.ops.transform.translate 的 poll 要求 WINDOW region（有 RegionView3D），
    UI region 不满足，导致 poll 静默失败。必须显式找到 WINDOW region。
    """
    screen = bpy.context.screen
    if not screen:
        return {}
    for area in screen.areas:
        if area.type != 'VIEW_3D':
            continue
        for region in area.regions:
            if region.type == 'WINDOW':
                return {"window": bpy.context.window, "screen": screen, "area": area, "region": region}
    return {}


def execute_code(context, code, prompt=""):
    """执行 AI 生成的 Python 代码（v9.2.6 修复版：含 temp_override）"""
    global _group_step_ok
    try:
        compiled = compile(code, "<ai_generated>", "exec")
        exec_globals = {"__name__": "__ai_exec__", "bpy": bpy, "context": bpy.context}
        view3d = _find_view3d_context()
        if view3d:
            with bpy.context.temp_override(**view3d):
                exec(compiled, exec_globals)
        else:
            exec(compiled, exec_globals)
        _group_step_ok = True
        return True, f"[OK] 执行成功: {prompt[:40]}"
    except Exception as e:
        _group_step_ok = False
        tb = traceback.format_exc(limit=6)
        print(tb)
        last = tb.splitlines()[-1].strip() if tb.splitlines() else str(e)
        return False, f"[ERR] {type(e).__name__}: {e} | {last}"


def auto_fix_and_retry(context, code, error_msg, prompt, round_num):
    """错误自动修复（最多 MAX_AUTO_FIX_ROUNDS 轮）"""
    if round_num >= AIState.MAX_AUTO_FIX_ROUNDS:
        AIState.log(f"[FIX] 已达最大修复轮次 ({AIState.MAX_AUTO_FIX_ROUNDS})，停止")
        return
    fix_prompt = (f"The following Python code produced an error when executed in Blender:\n\n"
                  f"```python\n{code}\n```\n\nError:\n{error_msg}\n\n"
                  f"Please fix the code and output ONLY the corrected ```python``` block.")
    scene_ctx = get_scene_context(context)
    user_content = f"Scene context:\n{scene_ctx}\n\nUser request (auto-fix round {round_num+1}):\n{fix_prompt}"
    AIState.log(f"[FIX] 第 {round_num + 1} 轮自动修复...")
    api_key, base_url, model, ssl_verify = get_credentials(context)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({"model": model, "messages": [{"role": "system", "content": SYSTEM_PROMPT_CODE}, {"role": "user", "content": user_content}], "max_tokens": 2048, "temperature": 0.1}).encode("utf-8")

    def _worker():
        try:
            req = urllib.request.Request(url, data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
            ssl_ctx = None
            if not ssl_verify:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            kwargs = {"timeout": 120}
            if ssl_ctx: kwargs["context"] = ssl_ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            reply = result["choices"][0]["message"]["content"]
            fixed_code = extract_code(reply)
            if fixed_code:
                AIState.log(f"[FIX] AI 返回修正代码 ({len(fixed_code.splitlines())} 行)")
                def _retry():
                    AIState.pending_code = fixed_code
                    AIState.pending_prompt = prompt
                    sync_code_to_text(fixed_code)
                    ok, msg = execute_code(context, fixed_code, prompt + " (auto-fixed)")
                    AIState.log(msg)
                    if ok:
                        AIState.last_ok_code = fixed_code
                        AIState.last_ok_prompt = prompt
                        AIState.reset_pending()
                        sync_code_to_text(fixed_code + "\n\n# --- 自动修复后执行成功 ---")
                    else:
                        auto_fix_and_retry(context, fixed_code, msg, prompt, round_num + 1)
                    redraw_ui()
                run_in_main_thread(_retry)
            else:
                run_in_main_thread(lambda: AIState.log("[FIX] AI 未返回代码块，停止修复"))
        except Exception as e:
            run_in_main_thread(lambda: AIState.log(f"[FIX] 修复请求失败: {e}"))

    threading.Thread(target=_worker, daemon=True).start()


# ============================================================
# Operators
# ============================================================

# ---- 截图测试 Operator ----
class BLENDER_AI_OT_test_screenshot(bpy.types.Operator):
    bl_idname = "blender_ai.test_screenshot"
    bl_label = "测试截图"
    bl_description = "截取当前视口并显示截图信息"

    def execute(self, context):
        AIState.log("[Screenshot] 正在截取视口...")
        b64 = capture_viewport_screenshot(max_size=512)
        if b64:
            size_kb = len(b64) / 1024
            AIState.log(f"[Screenshot] 截图成功 ({size_kb:.0f} KB base64)")
            self.report({'INFO'}, f"截图成功 ({size_kb:.0f} KB)")
        else:
            AIState.log("[Screenshot] 截图失败 - 检查是否有3D View区域")
            self.report({'WARNING'}, "截图失败")
        redraw_ui()
        return {'FINISHED'}


# ---- 核心 Operators ----
class BLENDER_AI_OT_send(bpy.types.Operator):
    bl_idname = "blender_ai.send"
    bl_label = "发送"
    bl_description = "把当前提示发送给 AI（工具模式/截图模式由侧栏开关控制）"

    def execute(self, context):
        if AIState.is_processing:
            self.report({'WARNING'}, "正在处理中")
            return {'CANCELLED'}
        prompt = (context.scene.ai_prompt or "").strip()
        if not prompt:
            self.report({'WARNING'}, "请输入指令")
            return {'CANCELLED'}

        AIState.is_processing = True
        AIState.cancel_flag = False
        AIState.use_tool_mode = context.scene.ai_use_tools
        AIState.log(f">> {prompt}")

        if context.scene.ai_attach_screenshot:
            _, _, model, _ = get_credentials(context)
            if model in MULTIMODAL_MODELS:
                AIState.log(f"[Screenshot] 附带视口截图 (模型: {model})")
            else:
                AIState.log(f"[Screenshot] 注意: {model} 不支持图片输入，仅发送文本")

        # v9.8.0: 不再自动清空 prompt，保留输入框内容方便修改后重发
        _add_to_prompt_history(prompt)
        redraw_ui()

        def _done(code_or_tools, error):
            AIState.is_processing = False
            if error:
                AIState.log(error or "Unknown error")
                redraw_ui()
                return

            if isinstance(code_or_tools, list):
                AIState.log(f"[TOOL] 执行 {len(code_or_tools)} 个工具调用...")
                for tc in code_or_tools:
                    tname = tc.get("tool", "?")
                    tparams = tc.get("params", {})
                    ok, msg = execute_tool_call(tname, tparams)
                    if ok:
                        AIState.log(f"[TOOL] {msg}")
                    else:
                        AIState.log(f"[TOOL] 失败: {msg}")
                AIState.log("[TOOL] 工具调用完成")
                redraw_ui()
                return

            code = code_or_tools
            AIState.pending_code = code
            AIState.pending_prompt = prompt
            sync_code_to_text(code)
            AIState.log(f"[AI] 生成了代码 ({len(code.splitlines())} 行)")
            write_command_log(prompt=prompt, code=code, status="AI生成（待执行）")
            redraw_ui()

        call_ai_api_async(context, prompt, _done)
        return {'FINISHED'}


class BLENDER_AI_OT_cancel(bpy.types.Operator):
    bl_idname = "blender_ai.cancel"
    bl_label = "取消"

    def execute(self, context):
        AIState.cancel_flag = True
        AIState.is_processing = False
        AIState.log("[WARN] 已取消")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_run_pending(bpy.types.Operator):
    bl_idname = "blender_ai.run_pending"
    bl_label = "运行代码"
    bl_description = "运行当前待执行代码"

    def execute(self, context):
        text_code = read_code_from_text().strip()
        code = text_code or AIState.pending_code
        if not code.strip():
            self.report({'WARNING'}, "没有代码可执行")
            return {'CANCELLED'}

        prompt = AIState.pending_prompt
        auto_fix = context.scene.ai_auto_fix

        AIState.log("[EXEC] 运行代码...")
        ok, msg = execute_code(context, code, prompt)
        AIState.log(msg)

        status = "执行成功" if ok else f"执行失败: {msg}"
        write_command_log(prompt=prompt, code=code, status=status)

        if ok:
            AIState.last_ok_code = code
            AIState.last_ok_prompt = prompt
            AIState.reset_pending()
            sync_code_to_text(code + "\n\n# --- 执行成功 ---")
        elif auto_fix:
            auto_fix_and_retry(context, code, msg, prompt, 0)

        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_discard(bpy.types.Operator):
    bl_idname = "blender_ai.discard"
    bl_label = "丢弃"

    def execute(self, context):
        AIState.reset_pending()
        clear_code_text()
        AIState.log("[TRASH] 已丢弃待执行代码")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_open_in_text_editor(bpy.types.Operator):
    bl_idname = "blender_ai.open_in_text_editor"
    bl_label = "在文本编辑器中打开"

    def execute(self, context):
        t = bpy.data.texts.get(AI_TEXT_NAME)
        if t is None:
            self.report({'WARNING'}, "还没有生成代码")
            return {'CANCELLED'}
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'TEXT_EDITOR':
                    for space in area.spaces:
                        if space.type == 'TEXT_EDITOR':
                            space.text = t
                            area.tag_redraw()
                            self.report({'INFO'}, f"已打开: {AI_TEXT_NAME}")
                            return {'FINISHED'}
        self.report({'INFO'}, "未找到 Text Editor 区域")
        return {'FINISHED'}


class BLENDER_AI_OT_copy_code(bpy.types.Operator):
    bl_idname = "blender_ai.copy_code"
    bl_label = "复制代码"

    def execute(self, context):
        code = (read_code_from_text()
                or AIState.pending_code or AIState.last_ok_code)
        if not code:
            self.report({'WARNING'}, "没有可复制的代码")
            return {'CANCELLED'}
        context.window_manager.clipboard = code
        self.report({'INFO'}, "已复制到剪贴板")
        return {'FINISHED'}


class BLENDER_AI_OT_clear_history(bpy.types.Operator):
    bl_idname = "blender_ai.clear_history"
    bl_label = "清空对话历史"

    def execute(self, context):
        AIState.clear_history()
        AIState.log("[INFO] 对话历史已清空")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_clear_logs(bpy.types.Operator):
    bl_idname = "blender_ai.clear_logs"
    bl_label = "清空日志"

    def execute(self, context):
        AIState.clear_logs()
        redraw_ui()
        return {'FINISHED'}


# ---- 日志复制 (v9.8.0) ----
class BLENDER_AI_OT_copy_logs(bpy.types.Operator):
    bl_idname = "blender_ai.copy_logs"
    bl_label = "复制日志"
    bl_description = "将全部操作日志复制到剪贴板"

    def execute(self, context):
        text = "\n".join(AIState.logs)
        if text:
            context.window_manager.clipboard = text
            self.report({'INFO'}, f"已复制 {len(AIState.logs)} 条日志")
        else:
            self.report({'WARNING'}, "没有日志可复制")
        return {'FINISHED'}


# ---- 指令历史 (v9.8.0) ----
class BLENDER_AI_OT_prev_prompt(bpy.types.Operator):
    bl_idname = "blender_ai.prev_prompt"
    bl_label = "上一条指令"
    bl_description = "回填上一条发送过的指令"

    def execute(self, context):
        history = _load_prompt_history()
        current = (context.scene.ai_prompt or "").strip()
        # 在历史中查找当前输入，回退到更早的一条
        found = False
        for h in history:
            if h.strip() == current:
                found = True
                continue
            if found:
                context.scene.ai_prompt = h
                return {'FINISHED'}
        # 没找到当前输入，直接用历史第一条
        if history and not found:
            context.scene.ai_prompt = history[0]
        return {'FINISHED'}


class BLENDER_AI_OT_next_prompt(bpy.types.Operator):
    bl_idname = "blender_ai.next_prompt"
    bl_label = "下一条指令"
    bl_description = "回填下一条（更新的）指令"

    def execute(self, context):
        history = _load_prompt_history()
        current = (context.scene.ai_prompt or "").strip()
        # 在历史中查当前输入前一条
        prev = None
        for h in history:
            if h.strip() == current:
                if prev is not None:
                    context.scene.ai_prompt = prev
                return {'FINISHED'}
            prev = h
        return {'FINISHED'}


class BLENDER_AI_OT_clear_prompt(bpy.types.Operator):
    bl_idname = "blender_ai.clear_prompt"
    bl_label = "清空输入"
    bl_description = "清空输入框"

    def execute(self, context):
        context.scene.ai_prompt = ""
        return {'FINISHED'}


# ---- 预设库 Operators ----
class BLENDER_AI_OT_save_preset(bpy.types.Operator):
    bl_idname = "blender_ai.save_preset"
    bl_label = "保存为预设"

    preset_name: bpy.props.StringProperty(name="名称", default="")
    preset_desc: bpy.props.StringProperty(name="描述", default="")

    def invoke(self, context, event):
        prompt = AIState.last_ok_prompt
        self.preset_name = (prompt[:40] if prompt else "新建预设")
        self.preset_desc = ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.prop(self, "preset_name", icon='FONT_DATA')
        self.layout.prop(self, "preset_desc", icon='INFO')

    def execute(self, context):
        code = AIState.last_ok_code
        prompt = AIState.last_ok_prompt
        if not code:
            self.report({'WARNING'}, "没有可保存的代码")
            return {'CANCELLED'}
        name = self.preset_name.strip()
        if not name:
            self.report({'WARNING'}, "名称不能为空")
            return {'CANCELLED'}
        presets = load_presets()
        presets.append({
            "id": str(uuid.uuid4()),
            "name": name,
            "desc": self.preset_desc.strip(),
            "code": code,
            "prompt": prompt,
        })
        if save_presets(presets):
            AIState.log(f"[SAVE] 已保存预设: {name}")
            write_command_log(prompt=f"[预设保存] {name}", code=code, status="已保存到预设库")
            redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_sync_presets_to_project(bpy.types.Operator):
    """将当前预设数据手动同步到项目目录（.blend 同目录），
    方便复制到其他项目使用，避免预设丢失。"""
    bl_idname = "blender_ai.sync_presets_to_project"
    bl_label = "同步预设到项目"
    bl_description = "将预设文件保存到当前 .blend 所在目录，可随项目迁移"

    def execute(self, context):
        blend_dir = _get_project_blend_dir()
        if not blend_dir:
            self.report({'WARNING'}, "请先保存 .blend 文件")
            return {'CANCELLED'}

        try:
            import shutil
            config_dir = bpy.utils.user_resource('CONFIG')
            files_to_sync = {
                PRESETS_FILENAME: ("预设库", load_presets),
                GROUPS_FILENAME: ("预设组", load_groups),
                MENU_PRESETS_FILENAME: ("菜单预设", _load_menu_presets),
                COMBO_PRESETS_FILENAME: ("组合预设", _load_combo_presets),
            }
            synced = []
            for fname, (label, loader) in files_to_sync.items():
                dest = os.path.join(blend_dir, fname)
                data = loader()
                with open(dest, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                synced.append(f"{label}({len(data) if isinstance(data, list) else len(data.get('groups', []))}项)")
                # 同时更新缓存路径指向项目目录
            CacheManager._last_filepath = ""  # 强制下次 init_paths 重新检测
            # 重新初始化指向项目目录
            CacheManager.init_paths()
            msg = f"已同步到项目目录: {', '.join(synced)}"
            AIState.log(f"[SYNC] {msg}")
            self.report({'INFO'}, msg)
            redraw_ui()
        except Exception as e:
            self.report({'ERROR'}, f"同步失败: {e}")
        return {'FINISHED'}


class BLENDER_AI_OT_open_log_folder(bpy.types.Operator):
    """在系统文件管理器中打开 AI 日志目录"""
    bl_idname = "blender_ai.open_log_folder"
    bl_label = "打开日志目录"
    bl_description = "在资源管理器中打开指令和代码的日志文件目录"

    def execute(self, context):
        blend_dir = _get_project_blend_dir()
        if not blend_dir:
            self.report({'WARNING'}, "请先保存 .blend 文件")
            return {'CANCELLED'}
        log_dir = os.path.join(blend_dir, "blender_ai_logs")
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        try:
            os.startfile(log_dir)
            self.report({'INFO'}, f"已打开: {log_dir}")
        except Exception as e:
            self.report({'ERROR'}, f"无法打开目录: {e}")
        return {'FINISHED'}


class BLENDER_AI_OT_run_preset(bpy.types.Operator):
    """运行预设（直接执行，v9.2.6 修复版）"""
    bl_idname = "blender_ai.run_preset"
    bl_label = "运行预设"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        presets = load_presets()
        target = next((p for p in presets if p.get("id") == self.preset_id), None)
        if target is None:
            self.report({'WARNING'}, "预设不存在")
            return {'CANCELLED'}
        code = target.get("code", "")
        if not code:
            self.report({'WARNING'}, "预设中没有代码")
            return {'CANCELLED'}
        AIState.log(f"[EXEC] 预设: {target.get('name', '')}")
        ok, msg = execute_code(context, code, target.get("prompt", ""))
        AIState.log(msg)
        status = "预设执行成功" if ok else f"预设执行失败: {msg}"
        write_command_log(prompt=target.get("prompt", ""), code=code, status=status)
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_delete_preset(bpy.types.Operator):
    bl_idname = "blender_ai.delete_preset"
    bl_label = "删除预设"

    preset_id: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        presets = load_presets()
        new = [p for p in presets if p.get("id") != self.preset_id]
        if len(new) == len(presets):
            self.report({'WARNING'}, "预设不存在")
            return {'CANCELLED'}
        save_presets(new)
        AIState.log("[TRASH] 预设已删除")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_rename_preset(bpy.types.Operator):
    bl_idname = "blender_ai.rename_preset"
    bl_label = "重命名预设"

    preset_id: bpy.props.StringProperty()
    new_name: bpy.props.StringProperty(name="新名称", default="")

    def invoke(self, context, event):
        for p in load_presets():
            if p.get("id") == self.preset_id:
                self.new_name = p.get("name", "")
                break
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        name = self.new_name.strip()
        if not name:
            self.report({'WARNING'}, "名称不能为空")
            return {'CANCELLED'}
        presets = load_presets()
        for p in presets:
            if p.get("id") == self.preset_id:
                p["name"] = name
                save_presets(presets)
                AIState.log(f"[INFO] 重命名为: {name}")
                redraw_ui()
                return {'FINISHED'}
        self.report({'WARNING'}, "预设不存在")
        return {'CANCELLED'}


# ============================================================
# v9.5.0 预设导出/导入（跨电脑/跨版本迁移）
# ============================================================
class BLENDER_AI_OT_export_presets(bpy.types.Operator, ExportHelper):
    """将所有预设打包导出为单个JSON文件"""
    bl_idname = "blender_ai.export_presets"
    bl_label = "导出全部预设"
    bl_description = "将预设库、预设组、菜单预设、组合预设打包导出为JSON文件"

    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    def execute(self, context):
        bundle = {
            "version": "1.0",
            "export_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "blender_ai_version": "9.8.0",
            "presets": load_presets(),
            "groups": load_groups(),
            "menu_presets": _load_menu_presets(),
            "combo_presets": _load_combo_presets(),
        }
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(bundle, f, ensure_ascii=False, indent=2)
            n_p = len(bundle["presets"])
            n_g = len(bundle["groups"].get("groups", []))
            n_m = len(bundle["menu_presets"])
            n_c = len(bundle["combo_presets"])
            msg = f"已导出: {n_p}预设, {n_g}组, {n_m}菜单, {n_c}组合"
            AIState.log(f"[EXPORT] {msg} -> {self.filepath}")
            self.report({'INFO'}, msg)
            redraw_ui()
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"导出失败: {e}")
            return {'CANCELLED'}


class BLENDER_AI_OT_import_presets(bpy.types.Operator, ImportHelper):
    """从JSON文件导入预设，支持替换或合并两种模式"""
    bl_idname = "blender_ai.import_presets"
    bl_label = "导入预设"
    bl_description = "从JSON文件导入预设数据（替换或合并到现有预设中）"

    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})
    # 显式开启扩展名校验（兼容 Blender 4.5+/5.x）
    check_extension = True

    import_mode: bpy.props.EnumProperty(
        name="导入模式",
        description="替换=用导入数据覆盖当前预设, 合并=保留现有并追加导入的预设",
        items=[
            ('REPLACE', "替换", "用导入的预设完全替换当前所有预设（当前预设将丢失）"),
            ('MERGE', "合并", "将导入的预设追加到现有预设中（避免ID冲突）"),
        ],
        default='REPLACE',
    )

    def check(self, context):
        """显式校验文件扩展名，确保 .json 文件能通过文件浏览器校验。
        Blender 4.5+/5.x 的 ImportHelper 默认 check() 对非标准扩展名可能返回 False，
        导致文件浏览器提示「不支持的格式」。"""
        return self.filepath.lower().endswith('.json')

    def draw(self, context):
        self.layout.prop(self, "import_mode")
        if self.import_mode == 'REPLACE':
            self.layout.label(text="注意: 当前所有预设将被覆盖!", icon='ERROR')

    def execute(self, context):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                bundle = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"文件读取失败: {e}")
            return {'CANCELLED'}

        if not isinstance(bundle, dict):
            self.report({'ERROR'}, "无效的预设文件格式")
            return {'CANCELLED'}

        mode = self.import_mode
        stats = []

        if "presets" in bundle and isinstance(bundle["presets"], list):
            imported = bundle["presets"]
            if mode == 'REPLACE':
                save_presets(imported)
                stats.append(f"{len(imported)}预设")
            else:
                existing = load_presets()
                existing.extend(imported)
                save_presets(existing)
                stats.append(f"{len(imported)}预设(共{len(existing)})")

        if "groups" in bundle and isinstance(bundle["groups"], dict):
            g_data = bundle["groups"]
            if mode == 'REPLACE':
                save_groups(g_data)
                stats.append(f"{len(g_data.get('groups', []))}组")
            else:
                existing = load_groups()
                for g in g_data.get("groups", []):
                    ng = dict(g)
                    ng["id"] = str(uuid.uuid4())
                    existing["groups"].append(ng)
                save_groups(existing)
                stats.append(f"{len(g_data.get('groups', []))}组(共{len(existing['groups'])})")

        if "menu_presets" in bundle and isinstance(bundle["menu_presets"], list):
            imported = bundle["menu_presets"]
            if mode == 'REPLACE':
                _save_menu_presets(imported)
                stats.append(f"{len(imported)}菜单预设")
            else:
                existing = _load_menu_presets()
                existing.extend(imported)
                _save_menu_presets(existing)
                stats.append(f"{len(imported)}菜单预设(共{len(existing)})")

        if "combo_presets" in bundle and isinstance(bundle["combo_presets"], list):
            imported = bundle["combo_presets"]
            if mode == 'REPLACE':
                _save_combo_presets(imported)
                stats.append(f"{len(imported)}组合预设")
            else:
                existing = _load_combo_presets()
                existing.extend(imported)
                _save_combo_presets(existing)
                stats.append(f"{len(imported)}组合预设(共{len(existing)})")

        if not stats:
            self.report({'WARNING'}, "文件中未找到有效预设数据")
            return {'CANCELLED'}

        detail = ", ".join(stats)
        AIState.log(f"[IMPORT] 模式={mode}: {detail}")
        AIState.log(f"[IMPORT] 文件: {self.filepath}")
        self.report({'INFO'}, f"导入成功: {detail}")
        # 导入后刷新缓存（从磁盘同步内存）
        CacheManager.reload_all()
        redraw_ui()
        return {'FINISHED'}


# ---- 菜单预设 Operators ----
class BLENDER_AI_OT_save_to_menu(bpy.types.Operator):
    """将当前成功执行的代码保存到顶部菜单预设"""
    bl_idname = "blender_ai.save_to_menu"
    bl_label = "添加到菜单"
    bl_description = "将代码添加到顶部自动化菜单，方便快速执行"

    preset_name: bpy.props.StringProperty(name="名称", default="")

    def invoke(self, context, event):
        self.preset_name = AIState.last_ok_prompt[:30] if AIState.last_ok_prompt else "菜单项"
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        self.layout.prop(self, "preset_name", icon='FONT_DATA')

    def execute(self, context):
        code = AIState.last_ok_code
        if not code:
            self.report({'WARNING'}, "没有可保存的代码")
            return {'CANCELLED'}
        name = self.preset_name.strip()
        if not name:
            self.report({'WARNING'}, "名称不能为空")
            return {'CANCELLED'}
        menu_items = _load_menu_presets()
        menu_items.append({
            "id": str(uuid.uuid4()),
            "name": name,
            "code": code,
            "prompt": AIState.last_ok_prompt,
        })
        if _save_menu_presets(menu_items):
            AIState.log(f"[MENU] 已添加: {name}")
            redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_run_menu_preset(bpy.types.Operator):
    """从菜单运行预设"""
    bl_idname = "blender_ai.run_menu_preset"
    bl_label = "Run Menu Preset"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        menu_items = _load_menu_presets()
        target = next((p for p in menu_items if p.get("id") == self.preset_id), None)
        if target is None:
            self.report({'WARNING'}, "预设不存在")
            return {'CANCELLED'}
        code = target.get("code", "")
        if not code:
            self.report({'WARNING'}, "预设中没有代码")
            return {'CANCELLED'}
        AIState.log(f"[MENU] {target.get('name', '')}")
        ok, msg = execute_code(context, code, target.get("prompt", ""))
        AIState.log(msg)
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_delete_menu_preset(bpy.types.Operator):
    """从菜单删除预设"""
    bl_idname = "blender_ai.delete_menu_preset"
    bl_label = "Delete Menu Preset"

    preset_id: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        menu_items = _load_menu_presets()
        new_items = [p for p in menu_items if p.get("id") != self.preset_id]
        if len(new_items) == len(menu_items):
            self.report({'WARNING'}, "预设不存在")
            return {'CANCELLED'}
        _save_menu_presets(new_items)
        AIState.log("[MENU] 已删除")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_add_preset_to_menu(bpy.types.Operator):
    """将已有本地预设一键添加到菜单（预设库行尾按钮）"""
    bl_idname = "blender_ai.add_preset_to_menu"
    bl_label = "添加到菜单"
    bl_description = "将此预设复制到顶部自动化菜单"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        presets = load_presets()
        source = next((p for p in presets if p.get("id") == self.preset_id), None)
        if source is None:
            self.report({'WARNING'}, "预设不存在")
            return {'CANCELLED'}
        menu_items = _load_menu_presets()
        menu_items.append({
            "id": str(uuid.uuid4()),
            "name": source.get("name", "Untitled"),
            "code": source.get("code", ""),
            "prompt": source.get("prompt", ""),
        })
        if _save_menu_presets(menu_items):
            AIState.log(f"[MENU] 已添加: {source.get('name', '')}")
            redraw_ui()
        return {'FINISHED'}


# ---- 组合预设 Operators ----
class BLENDER_AI_OT_create_combo(bpy.types.Operator):
    """新建组合预设：选定多个菜单预设编号，保存为一个组合"""
    bl_idname = "blender_ai.create_combo"
    bl_label = "新建组合预设"
    bl_description = "将多个菜单预设打包为组合，一键顺序执行"

    combo_name: bpy.props.StringProperty(name="组合名称", default="")
    combo_ids_str: bpy.props.StringProperty(
        name="预设编号", default="",
        description="输入菜单预设编号，逗号分隔，如: 1,3,5",
    )

    def invoke(self, context, event):
        self.combo_name = "新组合"
        self.combo_ids_str = ""
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "combo_name", icon='BOOKMARKS')
        layout.separator(factor=0.5)
        menu_items = _load_menu_presets()
        if menu_items:
            box = layout.box()
            box.label(text="当前菜单预设（按编号选择）:", icon='PRESET')
            col = box.column(align=True)
            col.scale_y = 0.85
            for i, item in enumerate(menu_items):
                col.label(text=f"  {i+1}. {item.get('name', 'Untitled')}")
        else:
            layout.label(text="菜单中暂无预设，请先添加", icon='INFO')
        layout.separator(factor=0.5)
        layout.prop(self, "combo_ids_str", icon='LINENUMBERS_ON')
        layout.label(text="示例: 1,3,5（按此顺序执行）", icon='INFO')

    def execute(self, context):
        name = self.combo_name.strip()
        if not name:
            self.report({'WARNING'}, "组合名称不能为空")
            return {'CANCELLED'}
        menu_items = _load_menu_presets()
        if not menu_items:
            self.report({'WARNING'}, "菜单中暂无预设")
            return {'CANCELLED'}
        raw = self.combo_ids_str.strip()
        if not raw:
            self.report({'WARNING'}, "请输入预设编号")
            return {'CANCELLED'}
        selected_ids = []
        invalid = []
        for token in raw.split(','):
            token = token.strip()
            if not token:
                continue
            try:
                idx = int(token) - 1
                if 0 <= idx < len(menu_items):
                    selected_ids.append(menu_items[idx].get("id", ""))
                else:
                    invalid.append(token)
            except ValueError:
                invalid.append(token)
        if invalid:
            self.report({'WARNING'}, f"无效编号: {', '.join(invalid)}")
            return {'CANCELLED'}
        if not selected_ids:
            self.report({'WARNING'}, "未选中任何有效预设")
            return {'CANCELLED'}
        combos = _load_combo_presets()
        combos.append({
            "id": str(uuid.uuid4()),
            "name": name,
            "ids": selected_ids,
            "nums": raw,
        })
        if _save_combo_presets(combos):
            AIState.log(f"[COMBO] 已保存组合: {name} ({raw})")
            redraw_ui()
            self.report({'INFO'}, f"组合已保存: {name}")
        return {'FINISHED'}


class BLENDER_AI_OT_run_combo(bpy.types.Operator):
    """运行组合预设（按顺序执行其中所有菜单预设）"""
    bl_idname = "blender_ai.run_combo"
    bl_label = "Run Combo"

    combo_id: bpy.props.StringProperty()

    def execute(self, context):
        combos = _load_combo_presets()
        combo = next((c for c in combos if c.get("id") == self.combo_id), None)
        if combo is None:
            self.report({'WARNING'}, "组合预设不存在")
            return {'CANCELLED'}
        menu_items = _load_menu_presets()
        menu_map = {p.get("id", ""): p for p in menu_items}
        name = combo.get("name", "")
        ids = combo.get("ids", [])
        if not ids:
            self.report({'WARNING'}, "组合中没有预设")
            return {'CANCELLED'}
        AIState.log(f"[COMBO] 运行组合: {name} ({len(ids)} 个预设)")
        ok_count = 0
        for pid in ids:
            preset = menu_map.get(pid)
            if preset is None:
                AIState.log(f"  [SKIP] 预设不存在(id={pid[:8]}...)")
                continue
            code = preset.get("code", "")
            if not code:
                AIState.log(f"  [SKIP] {preset.get('name', '')} 无代码")
                continue
            ok, msg = execute_code(context, code, preset.get("prompt", ""))
            AIState.log(f"  {msg}")
            if ok:
                ok_count += 1
        AIState.log(f"[COMBO] 完成: {ok_count}/{len(ids)} 成功")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_delete_combo(bpy.types.Operator):
    """删除组合预设"""
    bl_idname = "blender_ai.delete_combo"
    bl_label = "Delete Combo"

    combo_id: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        combos = _load_combo_presets()
        new_combos = [c for c in combos if c.get("id") != self.combo_id]
        if len(new_combos) == len(combos):
            self.report({'WARNING'}, "组合不存在")
            return {'CANCELLED'}
        _save_combo_presets(new_combos)
        AIState.log("[COMBO] 已删除组合")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_rename_combo(bpy.types.Operator):
    """重命名组合预设"""
    bl_idname = "blender_ai.rename_combo"
    bl_label = "重命名组合"

    combo_id: bpy.props.StringProperty()
    new_name: bpy.props.StringProperty(name="新名称", default="")

    def invoke(self, context, event):
        combos = _load_combo_presets()
        for c in combos:
            if c.get("id") == self.combo_id:
                self.new_name = c.get("name", "")
                break
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        name = self.new_name.strip()
        if not name:
            self.report({'WARNING'}, "名称不能为空")
            return {'CANCELLED'}
        combos = _load_combo_presets()
        for c in combos:
            if c.get("id") == self.combo_id:
                c["name"] = name
                _save_combo_presets(combos)
                AIState.log(f"[COMBO] 重命名: {name}")
                redraw_ui()
                return {'FINISHED'}
        self.report({'WARNING'}, "组合不存在")
        return {'CANCELLED'}


# ============================================================
# v9.2.0 预设组 Operators
# ============================================================
def _resolve_preset(presets, pid):
    """根据ID查找预设，返回 (preset_dict_or_None, name_or_Untitled)"""
    for p in presets:
        if p.get("id") == pid:
            return p, p.get("name", "Untitled")
    return None, "Untitled"


class BLENDER_AI_OT_group_new(bpy.types.Operator):
    """创建新预设组"""
    bl_idname = "blender_ai.group_new"
    bl_label = "新建预设组"
    bl_description = "创建一个新的预设执行组"

    group_name: bpy.props.StringProperty(name="组名", default="新预设组")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        name = self.group_name.strip()
        if not name:
            self.report({'WARNING'}, "组名不能为空")
            return {'CANCELLED'}
        data = load_groups()
        new_id = str(uuid.uuid4())
        data["groups"].append({"id": new_id, "name": name, "items": []})
        data["active_group_id"] = new_id
        save_groups(data)
        AIState.log(f"[GROUP] 创建组: {name}")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_group_delete(bpy.types.Operator):
    """删除当前预设组"""
    bl_idname = "blender_ai.group_delete"
    bl_label = "删除组"
    bl_description = "删除当前激活的预设组（不删除组内预设）"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        data = load_groups()
        aid = data["active_group_id"]
        if not aid:
            self.report({'WARNING'}, "没有激活的组")
            return {'CANCELLED'}
        name = "?"
        new_groups = []
        for g in data["groups"]:
            if g["id"] == aid:
                name = g.get("name", "?")
            else:
                new_groups.append(g)
        data["groups"] = new_groups
        data["active_group_id"] = new_groups[0]["id"] if new_groups else None
        save_groups(data)
        AIState.log(f"[GROUP] 已删除组: {name}")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_group_rename(bpy.types.Operator):
    """重命名当前预设组"""
    bl_idname = "blender_ai.group_rename"
    bl_label = "重命名组"

    new_name: bpy.props.StringProperty(name="新名称", default="")

    def invoke(self, context, event):
        g = get_active_group()
        self.new_name = g.get("name", "") if g else ""
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        name = self.new_name.strip()
        if not name:
            self.report({'WARNING'}, "名称不能为空")
            return {'CANCELLED'}
        data = load_groups()
        for g in data["groups"]:
            if g["id"] == data["active_group_id"]:
                g["name"] = name
                save_groups(data)
                AIState.log(f"[GROUP] 重命名为: {name}")
                redraw_ui()
                return {'FINISHED'}
        self.report({'WARNING'}, "组不存在")
        return {'CANCELLED'}


class BLENDER_AI_OT_group_select(bpy.types.Operator):
    """切换激活的预设组"""
    bl_idname = "blender_ai.group_select"
    bl_label = "选择组"

    group_id: bpy.props.StringProperty()

    def execute(self, context):
        data = load_groups()
        data["active_group_id"] = self.group_id
        save_groups(data)
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_group_add_preset(bpy.types.Operator):
    """将预设添加到当前组末尾"""
    bl_idname = "blender_ai.group_add_preset"
    bl_label = "添加到组"
    bl_description = "将此预设添加到当前激活组的末尾"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        data = load_groups()
        aid = data["active_group_id"]
        if not aid:
            self.report({'WARNING'}, "请先创建或选择一个预设组")
            return {'CANCELLED'}
        for g in data["groups"]:
            if g["id"] == aid:
                g.setdefault("items", []).append(self.preset_id)
                save_groups(data)
                name = "?"
                for p in load_presets():
                    if p.get("id") == self.preset_id:
                        name = p.get("name", "?")
                        break
                cnt = g["items"].count(self.preset_id)
                AIState.log(f"[GROUP] 添加到 {g['name']}: {name} (x{cnt})")
                redraw_ui()
                return {'FINISHED'}
        self.report({'WARNING'}, "当前激活的组不存在")
        return {'CANCELLED'}


class BLENDER_AI_OT_group_remove_item(bpy.types.Operator):
    """从当前组中移除一个预设（按item_index精确移除，v9.2.3）"""
    bl_idname = "blender_ai.group_remove_item"
    bl_label = "从组移除"

    item_index: bpy.props.IntProperty(default=-1)
    preset_id: bpy.props.StringProperty(default="")  # 兼容旧代码

    def execute(self, context):
        data = load_groups()
        aid = data["active_group_id"]
        for g in data["groups"]:
            if g["id"] == aid:
                items = g.get("items", [])
                if self.item_index >= 0 and self.item_index < len(items):
                    items.pop(self.item_index)
                elif self.preset_id:
                    items = [i for i in items if i != self.preset_id]
                g["items"] = items
                save_groups(data)
                redraw_ui()
                return {'FINISHED'}
        return {'CANCELLED'}


class BLENDER_AI_OT_group_move_item_up(bpy.types.Operator):
    """组内预设上移一位（v9.2.3: 用 item_index 修复重复预设排序）"""
    bl_idname = "blender_ai.group_move_item_up"
    bl_label = "上移"

    item_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        if self.item_index < 0:
            return {'CANCELLED'}
        data = load_groups()
        for g in data["groups"]:
            if g["id"] == data["active_group_id"]:
                items = g.get("items", [])
                i = self.item_index
                if 0 < i < len(items):
                    items[i], items[i - 1] = items[i - 1], items[i]
                    save_groups(data)
                redraw_ui()
                return {'FINISHED'}
        return {'CANCELLED'}


class BLENDER_AI_OT_group_move_item_down(bpy.types.Operator):
    """组内预设下移一位（v9.2.3: 用 item_index 修复重复预设排序）"""
    bl_idname = "blender_ai.group_move_item_down"
    bl_label = "下移"

    item_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        if self.item_index < 0:
            return {'CANCELLED'}
        data = load_groups()
        for g in data["groups"]:
            if g["id"] == data["active_group_id"]:
                items = g.get("items", [])
                i = self.item_index
                if 0 <= i < len(items) - 1:
                    items[i], items[i + 1] = items[i + 1], items[i]
                    save_groups(data)
                redraw_ui()
                return {'FINISHED'}
        return {'CANCELLED'}


class BLENDER_AI_OT_group_run(bpy.types.Operator):
    """按顺序执行当前组内全部预设。
    v9.2.2 修复：改为调用 bpy.ops.blender_ai.run_preset 逐项执行，
    每个预设获得独立的 Operator context，解决 context 污染问题。
    """
    bl_idname = "blender_ai.group_run"
    bl_label = "运行组"
    bl_description = "按组内顺序执行全部预设"

    def execute(self, context):
        g = get_active_group()
        if not g:
            self.report({'WARNING'}, "没有激活的组")
            return {'CANCELLED'}
        items = list(g.get("items", []))
        if not items:
            self.report({'WARNING'}, "组内没有预设")
            return {'CANCELLED'}

        presets = load_presets()
        total = len(items)
        ok_count = 0
        fail_count = 0
        skip_count = 0
        AIState.log(f"[GROUP] === 执行组「{g['name']}」 ({total}项) ===")

        for i, pid in enumerate(items, 1):
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            p, pname = _resolve_preset(presets, pid)
            if p is None:
                AIState.log(f"[GROUP] ({i}/{total}) 预设不存在(id={pid[:8]}...)，跳过")
                skip_count += 1
                continue
            code = p.get("code", "")
            if not code or not code.strip():
                AIState.log(f"[GROUP] ({i}/{total}) {pname} — 无代码，跳过")
                skip_count += 1
                continue
            AIState.log(f"[GROUP] ({i}/{total}) 执行: {pname}")
            global _group_step_ok
            _group_step_ok = None
            try:
                bpy.ops.blender_ai.run_preset('EXEC_DEFAULT', preset_id=pid)
                if _group_step_ok is True:
                    ok_count += 1
                    AIState.log(f"  -> [OK]")
                elif _group_step_ok is False:
                    fail_count += 1
                    AIState.log(f"  -> [FAIL]")
                else:
                    ok_count += 1
                    AIState.log(f"  -> [?] 无法判断结果，继续")
            except Exception as e:
                fail_count += 1
                AIState.log(f"  -> [FAIL] 操作符异常: {type(e).__name__}: {e}")
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass

        summary = f"[GROUP] === 完成: {ok_count}成功, {fail_count}失败, {skip_count}跳过 ==="
        AIState.log(summary)
        if fail_count > 0:
            self.report(
                {'WARNING'} if ok_count > 0 else {'ERROR'},
                f"执行完成: {ok_count}成功/{fail_count}失败/{skip_count}跳过",
            )
        redraw_ui()
        return {'FINISHED'}


# ---- 连接/URL/快速预设 Operators ----
class BLENDER_AI_OT_test_connection(bpy.types.Operator):
    bl_idname = "blender_ai.test_connection"
    bl_label = "测试连接"

    def execute(self, context):
        api_key, base_url, model, ssl_verify = get_credentials(context)
        if not api_key:
            context.scene.ai_conn_status = "fail"
            AIState.log("[ERR] 未填写 API Key")
            redraw_ui()
            return {'FINISHED'}

        context.scene.ai_conn_status = "testing"
        key_preview = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
        url = base_url.rstrip("/") + "/chat/completions"
        AIState.log(f"[TEST] 正在测试连接...")
        try:
            from urllib.parse import urlparse as _urlparse
            pu = _urlparse(url)
            host_part = f"{pu.scheme}://{pu.hostname}" + (f":{pu.port}" if pu.port else "")
            path_part = pu.path or "/"
        except Exception:
            host_part = url
            path_part = ""
        AIState.log(f"[TEST] Host: {host_part}")
        AIState.log(f"[TEST] Path: {path_part}")
        AIState.log(f"[TEST] Key: {key_preview}")
        AIState.log(f"[TEST] Model: {model}")
        AIState.log(f"[TEST] SSL: {'ON' if ssl_verify else 'OFF'}")
        redraw_ui()

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }).encode("utf-8")

        def _worker():
            try:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    method="POST",
                )
                ctx = None if ssl_verify else ssl.create_default_context()
                if not ssl_verify and ctx is not None:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                kwargs = {"timeout": 15}
                if ctx is not None:
                    kwargs["context"] = ctx
                with urllib.request.urlopen(req, **kwargs) as resp:
                    json.loads(resp.read().decode("utf-8"))
                def _ok():
                    bpy.context.scene.ai_conn_status = "ok"
                    AIState.log("[OK] 连接成功!")
                    redraw_ui()
                run_in_main_thread(_ok)
            except urllib.error.HTTPError as e:
                code = e.code
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                err = f"HTTP {code}: {e.reason}"
                if body:
                    err += f" | {body}"
                def _fail():
                    bpy.context.scene.ai_conn_status = "fail"
                    full_url = bpy.context.scene.ai_base_url or "?"
                    AIState.log(f"[ERR] 连接失败: {err}")
                    AIState.log(f"[ERR] 当前 Base URL: {full_url}")
                    AIState.log(f"[ERR] 排查: 1) API Key  2) URL路径(点重置URL)  3) 网络/代理")
                    redraw_ui()
                run_in_main_thread(_fail)
            except urllib.error.URLError as e:
                reason = str(e.reason)
                def _fail():
                    bpy.context.scene.ai_conn_status = "fail"
                    full_url = bpy.context.scene.ai_base_url or "?"
                    AIState.log(f"[ERR] 网络错误: {reason}")
                    AIState.log(f"[ERR] 当前 Base URL: {full_url}")
                    AIState.log(f"[ERR] 排查: 1) 网络连接  2) URL  3) 内网需关闭SSL")
                    redraw_ui()
                run_in_main_thread(_fail)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                def _fail():
                    bpy.context.scene.ai_conn_status = "fail"
                    AIState.log(f"[ERR] 连接失败: {err}")
                    redraw_ui()
                run_in_main_thread(_fail)

        threading.Thread(target=_worker, daemon=True).start()
        return {'FINISHED'}


class BLENDER_AI_OT_reset_url(bpy.types.Operator):
    """重置 Base URL 为 DeepSeek 官方地址"""
    bl_idname = "blender_ai.reset_url"
    bl_label = "重置URL"
    bl_description = "恢复 Base URL 为 DeepSeek 官方地址 (https://api.deepseek.com)"

    def execute(self, context):
        context.scene.ai_base_url = "https://api.deepseek.com"
        AIState.log("[OK] Base URL 已重置为 https://api.deepseek.com")
        return {'FINISHED'}


class BLENDER_AI_OT_set_quick(bpy.types.Operator):
    bl_idname = "blender_ai.set_quick"
    bl_label = "快速预设"

    base_url: bpy.props.StringProperty()
    model_name: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        scene.ai_base_url = self.base_url
        scene.ai_model_name = self.model_name
        ids = [it[0] for it in MODEL_PRESETS]
        scene.ai_model_preset = self.model_name if self.model_name in ids else 'CUSTOM'
        return {'FINISHED'}


# ============================================================
# 3D View 顶部自动化菜单
# ============================================================
class BLENDER_AI_MT_auto_menu(bpy.types.Menu):
    bl_label = "自动化"
    bl_idname = "BLENDER_AI_MT_auto_menu"

    def draw(self, context):
        layout = self.layout
        menu_items = _load_menu_presets()
        combos = _load_combo_presets()

        if not menu_items:
            layout.label(text="暂无菜单预设", icon='INFO')
        else:
            layout.label(text="预设:", icon='PRESET')
            for item in menu_items:
                name = item.get("name", "Untitled")
                item_id = item.get("id", "")
                op = layout.operator("blender_ai.run_menu_preset", text=name, icon='PLAY')
                op.preset_id = item_id

        layout.separator()

        if combos:
            layout.label(text="组合预设:", icon='BOOKMARKS')
            for combo in combos:
                cname = combo.get("name", "Untitled")
                nums = combo.get("nums", "")
                display = f"{cname}  [{nums}]" if nums else cname
                op = layout.operator("blender_ai.run_combo", text=display, icon='SEQUENCE_COLOR_04')
                op.combo_id = combo.get("id", "")
            layout.separator()

        layout.operator("blender_ai.save_to_menu", text="+ 添加预设到菜单", icon='ADD')
        layout.operator("blender_ai.create_combo", text="+ 新建组合预设", icon='BOOKMARKS')
        layout.menu("BLENDER_AI_MT_auto_menu_manage", text="管理/删除...")


class BLENDER_AI_MT_auto_menu_manage(bpy.types.Menu):
    bl_label = "管理预设"
    bl_idname = "BLENDER_AI_MT_auto_menu_manage"

    def draw(self, context):
        layout = self.layout
        menu_items = _load_menu_presets()
        combos = _load_combo_presets()

        if menu_items:
            layout.label(text="删除菜单预设:", icon='PRESET')
            for item in menu_items:
                name = item.get("name", "Untitled")
                item_id = item.get("id", "")
                op = layout.operator("blender_ai.delete_menu_preset",
                                     text=f"x  {name}", icon='TRASH')
                op.preset_id = item_id
        else:
            layout.label(text="暂无菜单预设", icon='INFO')

        layout.separator()

        if combos:
            layout.label(text="组合预设管理:", icon='BOOKMARKS')
            for combo in combos:
                cname = combo.get("name", "Untitled")
                cid = combo.get("id", "")
                row_op = layout.operator("blender_ai.rename_combo",
                                         text=f"改名: {cname}", icon='GREASEPENCIL')
                row_op.combo_id = cid
                del_op = layout.operator("blender_ai.delete_combo",
                                          text=f"x  {cname}", icon='TRASH')
                del_op.combo_id = cid
        else:
            layout.label(text="暂无组合预设", icon='INFO')


def draw_auto_menu(self, context):
    """在 3D View 顶部菜单栏添加「自动化」菜单入口"""
    self.layout.menu("BLENDER_AI_MT_auto_menu", icon='AUTO')


# ============================================================
# UI Panel
# ============================================================
def _update_scene_model_preset(self, context):
    if self.ai_model_preset != 'CUSTOM':
        self.ai_model_name = self.ai_model_preset


class BLENDER_AI_PT_main_panel(bpy.types.Panel):
    bl_label = "Blender AI Assistant"
    bl_idname = "BLENDER_AI_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AI Assistant'

    def draw(self, context):
        try:
            self._draw(context)
        except Exception as e:
            traceback.print_exc()
            self.layout.label(text=f"[ERROR] {e}", icon='ERROR')

    def _draw(self, context):
        layout = self.layout
        scene = context.scene

        # ---- 设置（可折叠） ----
        row = layout.row()
        row.prop(
            scene, "ai_show_settings",
            text="设置",
            icon='TRIA_DOWN' if scene.ai_show_settings else 'TRIA_RIGHT',
            emboss=False,
        )

        if scene.ai_show_settings:
            box = layout.box()
            status = scene.ai_conn_status
            icon = {'ok': 'CHECKMARK', 'fail': 'CANCEL', 'testing': 'TIME'}.get(status, 'RADIOBUT_OFF')
            text = {'ok': '已连接', 'fail': '连接失败', 'testing': '测试中...'}.get(status, '未测试')
            box.label(text=text, icon=icon)

            box.prop(scene, "ai_base_url")
            box.prop(scene, "ai_model_preset")
            if scene.ai_model_preset == 'CUSTOM':
                box.prop(scene, "ai_model_name", text="自定义模型")
            else:
                box.label(text=f"模型: {scene.ai_model_name}", icon='RIGID_BODY')
            box.prop(scene, "ai_api_key")

            row = box.row(align=True)
            row.label(text="快速:", icon='PRESET')
            for label, url, model in (
                ("DeepSeek", "https://api.deepseek.com",    "deepseek-chat"),
                ("R1",       "https://api.deepseek.com",    "deepseek-reasoner"),
                ("OpenAI",   "https://api.openai.com/v1",   "gpt-4o-mini"),
                ("Local",    "http://localhost:11434/v1",    "llama3"),
            ):
                op = row.operator("blender_ai.set_quick", text=label)
                op.base_url = url
                op.model_name = model

            row = box.row(align=True)
            row.operator("blender_ai.test_connection", icon='PLAY')
            row.operator("blender_ai.reset_url", icon='FILE_REFRESH')

            layout.separator(factor=0.5)
            box2 = layout.box()
            box2.label(text="增强功能", icon='SETTINGS')

            row = box2.row()
            row.prop(scene, "ai_attach_screenshot", text="附带视口截图")
            row = box2.row()
            row.prop(scene, "ai_use_tools", text="工具调用模式")
            row = box2.row()
            row.prop(scene, "ai_auto_fix", text="错误自动修复")
            row = box2.row()
            row.prop(scene, "ai_ssl_verify", text="SSL证书验证")

        # ---- 输入 ----
        box = layout.box()
        row = box.row(align=True)
        row.label(text="输入指令", icon='COMMUNITY')
        turns = len(AIState.conversation) // 2
        if turns > 0:
            row.label(text=f"({turns} 轮)")
            row.operator("blender_ai.clear_history", text="", icon='X')

        mode_row = box.row(align=True)
        tags = []
        if scene.ai_use_tools:
            tags.append("工具模式")
        if scene.ai_attach_screenshot:
            tags.append("截图")
        if scene.ai_auto_fix:
            tags.append("自动修复")
        if tags:
            mode_row.label(text=" | ".join(tags), icon='DOT')

        # ---- Prompt 输入 + 历史导航 (v9.8.0) ----
        row = box.row(align=True)
        row.prop(scene, "ai_prompt", text="")
        sub = row.row(align=True)
        sub.scale_x = 0.6
        sub.operator("blender_ai.prev_prompt", text="", icon='TRIA_UP')
        sub.operator("blender_ai.next_prompt", text="", icon='TRIA_DOWN')

        row = box.row(align=True)
        row.scale_y = 1.4
        if AIState.is_processing:
            row.label(text="处理中...", icon='TIME')
            row.operator("blender_ai.cancel", text="取消", icon='X')
        else:
            row.operator("blender_ai.send", text="发送", icon='PLAY')
            row.operator("blender_ai.clear_prompt", text="", icon='X')

        # ---- 待执行代码 ----
        if AIState.pending_code:
            layout.separator()
            box = layout.box()
            row = box.row(align=True)
            row.label(text="代码预览", icon='SCRIPT')
            row.operator("blender_ai.open_in_text_editor", text="", icon='TEXT')
            row.operator("blender_ai.copy_code", text="", icon='COPYDOWN')

            text_code = read_code_from_text()
            shown = text_code if text_code else AIState.pending_code
            edited = text_code and text_code.strip() != AIState.pending_code.strip()
            if edited:
                box.label(text="※ 已在 Text Editor 中编辑", icon='GREASEPENCIL')

            code_box = box.box()
            col = code_box.column(align=True)
            col.scale_y = 0.82
            lines = shown.splitlines()
            max_show = 15
            for line in lines[:max_show]:
                col.label(text=line if line else " ")
            if len(lines) > max_show:
                col.label(text=f"... 还有 {len(lines) - max_show} 行", icon='INFO')

            row = box.row(align=True)
            row.scale_y = 1.3
            row.operator("blender_ai.run_pending", text="运行", icon='PLAY')
            row.operator("blender_ai.discard", text="丢弃", icon='TRASH')

            info_row = box.row()
            info_row.scale_y = 0.8
            info_row.label(
                text=f"提示: 在 Text Editor 打开 '{AI_TEXT_NAME}' 可多行编辑",
                icon='INFO',
            )

        # ---- 刚执行成功的代码 ----
        if AIState.last_ok_code and not AIState.pending_code:
            layout.separator(factor=0.3)
            box = layout.box()
            box.label(text="代码执行成功", icon='CHECKMARK')
            row = box.row(align=True)
            row.scale_y = 1.2
            row.operator("blender_ai.save_preset", text="保存为预设", icon='ADD')
            row.operator("blender_ai.save_to_menu", text="添加到菜单", icon='MENU_PANEL')

        # ---- 预设库 + 预设组（v9.2.0 两层架构）----
        layout.separator()
        box = layout.box()

        presets = load_presets()
        groups_data = load_groups()
        all_groups = groups_data["groups"]
        active_gid = groups_data["active_group_id"]
        active_group = None
        for g in all_groups:
            if g["id"] == active_gid:
                active_group = g
                break

        # === 上半部分: 预设库（素材列表）===
        header = box.row(align=True)
        header.label(text=f"预设库 ({len(presets)})", icon='ASSET_MANAGER')
        sub = header.row(align=True)
        sub.alignment = 'RIGHT'
        sub.operator("blender_ai.export_presets", text="", icon='EXPORT')
        sub.operator("blender_ai.import_presets", text="", icon='IMPORT')
        sub.operator("blender_ai.sync_presets_to_project", text="", icon='FILE_REFRESH')
        if not presets:
            box.label(text="  暂无预设，执行代码后可「保存为预设」", icon='INFO')
        else:
            for i, p in enumerate(presets, 1):
                pid = p.get("id", "")
                pname = p.get("name", "Untitled")

                sub = box.row(align=True)
                sub.scale_y = 1.02

                op = sub.operator("blender_ai.run_preset",
                                  text=f"{i}. {pname}", icon='PLAY')
                op.preset_id = pid

                # 重命名 / 删除 / 添加到组 / 添加到菜单（v9.3.0 新增）
                op = sub.operator("blender_ai.rename_preset", text="", icon='GREASEPENCIL')
                op.preset_id = pid
                op = sub.operator("blender_ai.delete_preset", text="", icon='TRASH')
                op.preset_id = pid
                op = sub.operator("blender_ai.group_add_preset", text="", icon='ADD')
                op.preset_id = pid
                op = sub.operator("blender_ai.add_preset_to_menu", text="", icon='MENU_PANEL')
                op.preset_id = pid

        # === 下半部分: 预设组（执行序列，可排序）===
        layout.separator(factor=0.3)
        grp_box = layout.box()

        row = grp_box.row()
        row.label(text="预设组", icon='OUTLINER_COLLECTION')
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        sub.operator("blender_ai.group_new", text="+新建", icon='ADD')
        if active_group:
            sub.operator("blender_ai.group_run",
                         text=f"运行「{active_group['name']}」", icon='PLAY')

        if not all_groups:
            grp_box.label(text="  暂无预设组，点击「+新建」创建", icon='INFO')
        else:
            # 组选择器
            sel_row = grp_box.row(align=True)
            sel_row.scale_y = 1.0
            for g in all_groups:
                is_active = g["id"] == active_gid
                icon = 'RADIOBUT_ON' if is_active else 'RADIOBUT_OFF'
                op = sel_row.operator("blender_ai.group_select",
                                       text=g["name"], icon=icon,
                                       depress=is_active)
                op.group_id = g["id"]

            mgr_row = grp_box.row(align=True)
            mgr_row.scale_y = 0.9
            mgr_row.operator("blender_ai.group_rename", text="重命名", icon='GREASEPENCIL')
            mgr_row.operator("blender_ai.group_delete", text="删除组", icon='TRASH')

            if active_group:
                items = active_group.get("items", [])
                if not items:
                    grp_box.label(
                        text="  组内暂无预设，从上方预设库点[+]添加",
                        icon='INFO',
                    )
                else:
                    for i, pid in enumerate(items, 1):
                        _, pname = _resolve_preset(presets, pid)
                        sub = grp_box.row(align=True)
                        sub.scale_y = 1.02
                        sub.label(text=f"{i}. {pname}", icon='DOT')

                        idx = i - 1  # 0-based index
                        if i > 1:
                            op = sub.operator("blender_ai.group_move_item_up",
                                              text="", icon='TRIA_UP')
                            op.item_index = idx
                        else:
                            sub.label(text="", icon='BLANK1')
                        if i < len(items):
                            op = sub.operator("blender_ai.group_move_item_down",
                                              text="", icon='TRIA_DOWN')
                            op.item_index = idx
                        else:
                            sub.label(text="", icon='BLANK1')

                        op = sub.operator("blender_ai.group_remove_item",
                                          text="", icon='X')
                        op.item_index = idx
                        op.preset_id = pid

        # ---- 日志 (v9.8.0 增强：可复制) ----
        layout.separator()
        box = layout.box()
        row = box.row()
        row.label(text="操作日志", icon='TEXT')
        row.operator("blender_ai.copy_logs", text="复制", icon='COPYDOWN')
        row.operator("blender_ai.open_log_folder", text="日志", icon='FILE_FOLDER')
        row.operator("blender_ai.clear_logs", text="清空", icon='TRASH')

        col = box.column(align=True)
        col.scale_y = 0.8
        logs = AIState.logs[-20:]
        if not logs:
            col.label(text="  暂无日志", icon='INFO')
        else:
            for line in logs:
                col.label(text=line if line else " ")


# ============================================================
# Preferences
# ============================================================
def _update_pref_model_preset(self, context):
    if self.model_preset != 'CUSTOM':
        self.model_name = self.model_preset


class BLENDER_AI_preferences(bpy.types.AddonPreferences):
    bl_idname = __package__ if __package__ else "blender_ai_assistant"

    api_key: bpy.props.StringProperty(name="API Key", default="", subtype='PASSWORD')
    base_url: bpy.props.StringProperty(
        name="API URL",
        default="https://api.deepseek.com",
        description="API Base URL (DeepSeek: https://api.deepseek.com)",
    )
    model_preset: bpy.props.EnumProperty(
        name="模型",
        items=MODEL_PRESETS,
        default='deepseek-v4-flash',
        update=_update_pref_model_preset,
    )
    model_name: bpy.props.StringProperty(name="模型名称", default="deepseek-v4-flash")

    def draw(self, context):
        layout = self.layout
        layout.label(text="默认 API 配置（Scene 中填写的值会覆盖这里）")
        layout.prop(self, "api_key")
        layout.prop(self, "base_url")
        layout.prop(self, "model_preset")
        if self.model_preset == 'CUSTOM':
            layout.prop(self, "model_name")
        else:
            layout.label(text=f"当前模型: {self.model_name}")


# ============================================================
# 注册
# ============================================================
CLASSES = (
    BLENDER_AI_preferences,
    # 截图
    BLENDER_AI_OT_test_screenshot,
    # 核心
    BLENDER_AI_OT_send,
    BLENDER_AI_OT_cancel,
    BLENDER_AI_OT_run_pending,
    BLENDER_AI_OT_discard,
    BLENDER_AI_OT_open_in_text_editor,
    BLENDER_AI_OT_copy_code,
    BLENDER_AI_OT_clear_history,
    BLENDER_AI_OT_clear_logs,
    # v9.8.0 日志复制 + 指令历史
    BLENDER_AI_OT_copy_logs,
    BLENDER_AI_OT_prev_prompt,
    BLENDER_AI_OT_next_prompt,
    BLENDER_AI_OT_clear_prompt,
    # 预设库
    BLENDER_AI_OT_save_preset,
    BLENDER_AI_OT_run_preset,
    BLENDER_AI_OT_delete_preset,
    BLENDER_AI_OT_rename_preset,
    # 预设导出/导入（v9.5.0）
    BLENDER_AI_OT_export_presets,
    BLENDER_AI_OT_import_presets,
    # 菜单预设（v9.3.0）
    BLENDER_AI_OT_save_to_menu,
    BLENDER_AI_OT_run_menu_preset,
    BLENDER_AI_OT_delete_menu_preset,
    BLENDER_AI_OT_add_preset_to_menu,
    # 组合预设（v9.3.0）
    BLENDER_AI_OT_create_combo,
    BLENDER_AI_OT_run_combo,
    BLENDER_AI_OT_delete_combo,
    BLENDER_AI_OT_rename_combo,
    # 预设组（v9.2.0）
    BLENDER_AI_OT_group_new,
    BLENDER_AI_OT_group_delete,
    BLENDER_AI_OT_group_rename,
    BLENDER_AI_OT_group_select,
    BLENDER_AI_OT_group_add_preset,
    BLENDER_AI_OT_group_remove_item,
    BLENDER_AI_OT_group_move_item_up,
    BLENDER_AI_OT_group_move_item_down,
    BLENDER_AI_OT_group_run,
    # 连接/设置
    BLENDER_AI_OT_test_connection,
    BLENDER_AI_OT_reset_url,
    BLENDER_AI_OT_set_quick,
    # v9.10.0 项目目录日志 + 预设同步
    BLENDER_AI_OT_sync_presets_to_project,
    BLENDER_AI_OT_open_log_folder,
    # 菜单类（v9.3.0）
    BLENDER_AI_MT_auto_menu,
    BLENDER_AI_MT_auto_menu_manage,
    # Panel
    BLENDER_AI_PT_main_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    # 将「自动化」菜单挂载到 3D View 顶部菜单栏（标准挂载点）
    bpy.types.VIEW3D_MT_editor_menus.append(draw_auto_menu)

    S = bpy.types.Scene
    S.ai_show_settings = bpy.props.BoolProperty(name="显示设置", default=True)
    S.ai_base_url = bpy.props.StringProperty(
        name="API URL",
        description="DeepSeek: https://api.deepseek.com  |  OpenAI: https://api.openai.com/v1  |  本地: http://localhost:11434/v1",
        default="https://api.deepseek.com",
    )
    S.ai_model_preset = bpy.props.EnumProperty(
        name="模型", items=MODEL_PRESETS, default='deepseek-v4-flash',
        update=_update_scene_model_preset,
    )
    S.ai_model_name = bpy.props.StringProperty(name="模型名称", default="deepseek-v4-flash")
    S.ai_api_key = bpy.props.StringProperty(name="API Key", default="", subtype='PASSWORD')
    S.ai_prompt = bpy.props.StringProperty(name="提示", default="")
    S.ai_conn_status = bpy.props.StringProperty(name="连接状态", default="")

    # v9.0.0 增强功能开关
    S.ai_attach_screenshot = bpy.props.BoolProperty(
        name="附带视口截图",
        description="发送请求时附带当前3D视口截图（需多模态模型如GPT-4o/Claude）",
        default=False,
    )
    S.ai_use_tools = bpy.props.BoolProperty(
        name="工具调用模式",
        description="启用预置工具函数，AI可调用高级操作而非生成裸bpy代码",
        default=False,
    )
    S.ai_auto_fix = bpy.props.BoolProperty(
        name="错误自动修复",
        description="代码执行失败时自动发送错误信息给AI进行修正（最多3轮）",
        default=False,
    )
    # v9.1.0 SSL验证
    S.ai_ssl_verify = bpy.props.BoolProperty(
        name="SSL证书验证",
        description="关闭后跳过HTTPS证书验证（企业内网/自签证书环境可关闭）",
        default=True,
    )


def unregister():
    # 先移除顶部菜单钩子
    bpy.types.VIEW3D_MT_editor_menus.remove(draw_auto_menu)

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    S = bpy.types.Scene
    for prop in (
        "ai_show_settings", "ai_base_url", "ai_model_preset",
        "ai_model_name", "ai_api_key", "ai_prompt", "ai_conn_status",
        "ai_attach_screenshot", "ai_use_tools", "ai_auto_fix",
        "ai_ssl_verify",
    ):
        if hasattr(S, prop):
            try:
                delattr(S, prop)
            except Exception:
                pass


if __name__ == "__main__":
    register()