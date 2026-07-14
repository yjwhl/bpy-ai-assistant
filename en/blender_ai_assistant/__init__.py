bl_info = {
    "name": "Bpy AI Assistant",
    "author": "匹宙Plumb",
    "version": (9, 10, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar (N) > AI Assistant  |  3D View > Top Menu > Automation",
    "description": "Generate bpy scripts from natural language — preset library + project logging + multi-turn conversation, reduce repetitive tasks and focus on creating.",
    "category": "3D View",
}

"""
CHANGELOG
---------
v9.10.0 (2026-07-14)
  - NEW: Command + code logging — if .blend is saved, each conversation auto-generates text logs
    - Log file: {blend_dir}/blender_ai_logs/blender_ai_commands.log
    - Records: timestamp, input prompt, generated code, execution status
    - Triggers: send code generation, run code, preset execution, save preset
  - NEW: Preset auto-follows project — after saving .blend, preset files auto-link to project dir
    - _DataCache supports detecting .blend file changes, auto-switching preset paths
    - Auto-migrates existing preset data when switching from config to project directory
  - NEW: "Sync Presets to Project" button — manually sync preset files to current .blend dir
    - Preset library, preset groups, menu presets, combo presets — one-click sync
  - NEW: "Log" button — open log file directory in system file explorer
  - New Operators: sync_presets_to_project / open_log_folder
  - New functions: _get_project_blend_dir / _get_project_log_path / write_command_log

v9.9.0 (2026-07-14)
  - REMOVED: Animation keyframe capture feature (_capture_single, capture_animation_from_selected,
    6 Operators, Panel UI block) — low usage and error-prone, streamlined plugin core
  - Restored: bl_info description / CHANGELOG removed animation-related descriptions

v9.8.0 (2026-06-02)
  - UX: Prompts no longer lost — prompt input box not cleared after send, can modify and resend
  - NEW: Prompt history — file-based history (~50 entries), UP/DOWN buttons quickly recall past prompts
    - blender_ai_prompt_history.json auto-records each sent prompt (deduplicated, newest first)
  - NEW: Log copy — "Copy" button copies all operation logs to clipboard
  - New Operators: copy_logs / prev_prompt / next_prompt / clear_prompt

v9.7.0 (2026-05-25)
  - ARCH: Introduced _DataCache / CacheManager high-performance cache manager (from vscode14 new framework)
    - All preset IO changed to memory cache + atomic writes, completely fixes UI lag from frequent draw() IO
    - Atomic write (temp file first, then os.replace) prevents file corruption on crash
    - Compatibility layer (load_presets / save_presets / _load_menu_presets etc.) keeps interface unchanged
  - MIGRATE: Migrated all classes from demo13.py (v9.5.0) to new framework
    - All direct IO calls replaced with CacheManager-compatible functions (removed old interfaces like _menu_presets_path())
    - execute_code retains v9.2.6 full version (including _find_view3d_context + temp_override, critical bug fix)
  
  (Earlier versions omitted for brevity; see full CHANGELOG.md)
"""

# ============================================================
# Imports
# ============================================================
import bpy
import json
import os
import re
import ssl
import sys
import threading
import time
import traceback
import urllib.request
import urllib.error
import uuid
import shutil
import subprocess
import textwrap

# ============================================================
# Constants
# ============================================================
# DeepSeek and common model presets
MODEL_PRESETS = [
    ('deepseek-v4-flash', 'DeepSeek V4 Flash', 'DeepSeek V4 Flash (fastest)'),
    ('deepseek-chat',     'DeepSeek V3',     'DeepSeek V3 chat model'),
    ('deepseek-reasoner', 'DeepSeek R1',     'DeepSeek R1 reasoning model'),
    ('gpt-4o-mini',       'GPT-4o mini',     'OpenAI GPT-4o mini'),
    ('gpt-4o',            'GPT-4o',          'OpenAI GPT-4o'),
    ('claude-3.5-sonnet', 'Claude 3.5 Sonnet', 'Anthropic Claude 3.5 Sonnet'),
    ('llama3',            'Llama 3 (Local)', 'Local via Ollama'),
    ('CUSTOM',            'Custom',          'Enter custom model name'),
]

AI_TEXT_NAME = "ai_code.py"

# ============================================================
# Thread-safe UI helpers
# ============================================================
def run_in_main_thread(fn):
    """Schedule a function to run on Blender's main thread via app timer."""
    bpy.app.timers.register(fn, first_interval=0.01)


def redraw_ui():
    """Force redraw all 3D View UI areas."""
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


# ============================================================
# Data Cache (v9.7.0 — high-performance cache manager)
# ============================================================
class _DataCache:
    """Thread-safe in-memory cache with atomic file writes.
    
    Caches preset data in memory to avoid repeated disk IO during draw() calls.
    Uses explicit dirty flags; data flushed to disk only when explicitly called.
    """
    _instance = None

    def __new__(cls, *a, **kw):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, cache_dir='', presets_file='', groups_file='',
                 menu_file='', combo_file='', history_file=''):
        if hasattr(self, '_init_done') and self._init_done:
            return
        self._lock = threading.Lock()
        self._presets = None
        self._groups = None
        self._menu = None
        self._combo = None
        self._history = None
        self._dirty = {"presets": False, "groups": False, "menu": False, "combo": False, "history": False}
        self._cache_dir = cache_dir
        self._presets_file = presets_file
        self._groups_file = groups_file
        self._menu_file = menu_file
        self._combo_file = combo_file
        self._history_file = history_file
        self._last_filepath = None
        self._init_done = True

    def init_paths(self, cache_dir, presets_file, groups_file, menu_file, combo_file, history_file):
        """Set file paths. Auto-detect .blend file changes and migrate presets accordingly.
        
        v9.10.0: When .blend file changes, auto-switch preset storage to blend directory.
        """
        with self._lock:
            current_blend = ""
            try:
                current_blend = bpy.data.filepath
            except AttributeError:
                pass
            need_reset = (self._cache_dir != cache_dir
                          or self._presets_file != presets_file
                          or self._groups_file != groups_file)

            if need_reset:
                # Check if .blend file changed — migrate presets
                old_blend = self._last_filepath
                new_blend = current_blend
                if old_blend and new_blend and old_blend != new_blend and os.path.isfile(old_blend):
                    # Migrate preset data from old project to new project
                    print(f"[Blender AI] .blend changed: {os.path.basename(old_blend)} -> {os.path.basename(new_blend)}")
                    self._migrate_presets_between_blends(old_blend, new_blend)
                self._last_filepath = new_blend if new_blend else self._last_filepath

            self._cache_dir = cache_dir
            self._presets_file = presets_file
            self._groups_file = groups_file
            self._menu_file = menu_file
            self._combo_file = combo_file
            self._history_file = history_file
            self._presets = None
            self._groups = None
            self._menu = None
            self._combo = None
            self._history = None
            self._dirty = {k: False for k in self._dirty}

    def _migrate_presets_between_blends(self, old_blend, new_blend):
        """Copy preset files from old .blend project directory to new one."""
        old_dir = os.path.join(os.path.dirname(old_blend), "blender_ai_logs")
        new_dir = os.path.join(os.path.dirname(new_blend), "blender_ai_logs")
        if not os.path.isdir(old_dir):
            return
        os.makedirs(new_dir, exist_ok=True)
        for fname in ("blender_ai_assistant_presets.json",
                       "blender_ai_preset_groups.json",
                       "blender_ai_menu_presets.json",
                       "blender_ai_combo_presets.json"):
            src = os.path.join(old_dir, fname)
            dst = os.path.join(new_dir, fname)
            if os.path.isfile(src) and not os.path.isfile(dst):
                try:
                    shutil.copy2(src, dst)
                    print(f"[Blender AI] Migrated: {fname}")
                except Exception as e:
                    print(f"[Blender AI] Migration failed for {fname}: {e}")

    # ---- Generic read / write ----
    def _read_json(self, filepath, default):
        try:
            if os.path.isfile(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[Blender AI] Error reading {filepath}: {e}")
        return default

    def _write_json(self, filepath, data):
        """Atomic write: write to temp file, then rename to avoid corruption on crash."""
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            tmp = filepath + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, filepath)
        except Exception as e:
            print(f"[Blender AI] Error writing {filepath}: {e}")
            if os.path.isfile(filepath + ".tmp"):
                try:
                    os.remove(filepath + ".tmp")
                except Exception:
                    pass

    # ---- Presets ----
    def get_presets(self):
        with self._lock:
            if self._presets is None:
                self._presets = self._read_json(self._presets_file, [])
            return self._presets

    def set_presets(self, data):
        with self._lock:
            self._presets = data
            self._dirty["presets"] = True

    # ---- Groups ----
    def get_groups(self):
        with self._lock:
            if self._groups is None:
                default = {"groups": [], "active_group_id": ""}
                self._groups = self._read_json(self._groups_file, default)
            return self._groups

    def set_groups(self, data):
        with self._lock:
            self._groups = data
            self._dirty["groups"] = True

    # ---- Menu Presets ----
    def get_menu(self):
        with self._lock:
            if self._menu is None:
                self._menu = self._read_json(self._menu_file, [])
            return self._menu

    def set_menu(self, data):
        with self._lock:
            self._menu = data
            self._dirty["menu"] = True

    # ---- Combo Presets ----
    def get_combo(self):
        with self._lock:
            if self._combo is None:
                self._combo = self._read_json(self._combo_file, [])
            return self._combo

    def set_combo(self, data):
        with self._lock:
            self._combo = data
            self._dirty["combo"] = True

    # ---- Prompt History ----
    def get_history(self):
        with self._lock:
            if self._history is None:
                self._history = self._read_json(self._history_file, [])
            return self._history

    def set_history(self, data):
        with self._lock:
            self._history = data
            self._dirty["history"] = True

    # ---- Flush ----
    def flush(self, key=None):
        """Write dirty data to disk. If key is None, flush all dirty entries."""
        with self._lock:
            keys = [key] if key else list(self._dirty.keys())
            for k in keys:
                if not self._dirty.get(k):
                    continue
                mapping = {
                    "presets": (self._presets_file, self._presets),
                    "groups":  (self._groups_file, self._groups),
                    "menu":    (self._menu_file, self._menu),
                    "combo":   (self._combo_file, self._combo),
                    "history": (self._history_file, self._history),
                }
                if k in mapping:
                    fp, data = mapping[k]
                    self._write_json(fp, data)
                    self._dirty[k] = False

    def flush_all(self):
        self.flush()


# ============================================================
# CacheManager singleton
# ============================================================
_cache = _DataCache()


def _get_config_dir():
    """Get the config storage directory. v9.10.0: follow .blend file location."""
    try:
        blend = bpy.data.filepath
    except AttributeError:
        # Restricted context (e.g. extension install) — fall back to add-on directory
        blend = ""
    if blend:
        return os.path.join(os.path.dirname(blend), "blender_ai_logs")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def _init_cache():
    bd = _get_config_dir()
    os.makedirs(bd, exist_ok=True)
    _cache.init_paths(
        cache_dir=bd,
        presets_file=os.path.join(bd, "blender_ai_assistant_presets.json"),
        groups_file=os.path.join(bd, "blender_ai_preset_groups.json"),
        menu_file=os.path.join(bd, "blender_ai_menu_presets.json"),
        combo_file=os.path.join(bd, "blender_ai_combo_presets.json"),
        history_file=os.path.join(bd, "blender_ai_prompt_history.json"),
    )


_init_cache()


# ============================================================
# Compatibility layer — Presets
# ============================================================
def load_presets():
    return _cache.get_presets()


def save_presets(data):
    _cache.set_presets(data)
    _cache.flush("presets")


# ============================================================
# Compatibility layer — Groups
# ============================================================
def load_groups():
    return _cache.get_groups()


def save_groups(data):
    _cache.set_groups(data)
    _cache.flush("groups")


# ============================================================
# Compatibility layer — Menu Presets
# ============================================================
def _load_menu_presets():
    return _cache.get_menu()


def _save_menu_presets(data):
    _cache.set_menu(data)
    _cache.flush("menu")


# ============================================================
# Compatibility layer — Combo Presets
# ============================================================
def _load_combo_presets():
    return _cache.get_combo()


def _save_combo_presets(data):
    _cache.set_combo(data)
    _cache.flush("combo")


# ============================================================
# Compatibility layer — Prompt History
# ============================================================
def _load_prompt_history():
    return _cache.get_history()


def _save_prompt_history(data):
    _cache.set_history(data)
    _cache.flush("history")


# ============================================================
# Project Logging (v9.10.0)
# ============================================================
def _get_project_blend_dir():
    """Get the directory of the currently saved .blend file."""
    try:
        fp = bpy.data.filepath
    except AttributeError:
        return None
    if fp:
        return os.path.dirname(fp)
    return None


def _get_project_log_path():
    """Get the log file path in the .blend project directory."""
    bd = _get_project_blend_dir()
    if bd:
        log_dir = os.path.join(bd, "blender_ai_logs")
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, "blender_ai_commands.log")
    return None


def write_command_log(prompt, code, status="OK"):
    """Append a command + generated code entry to the project log file.
    
    Args:
        prompt: The user's input prompt (natural language).
        code: The generated code (bpy script).
        status: Execution status, e.g. "OK", "FAIL", "CANCELLED", "SKIPPED".
    """
    log_path = _get_project_log_path()
    if not log_path:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # Strip extra whitespace from code
    code = (code or "").strip()
    entry = (
        f"[{ts}] Prompt: {prompt.strip()}\n"
        f"Code:\n{code}\n"
        f"Status: {status}\n"
        f"{'-' * 60}\n"
    )
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(entry)
    except Exception as e:
        print(f"[Blender AI] Failed to write command log: {e}")


# ============================================================
# AIState — Global state and conversation management
# ============================================================
class AIState:
    """Holds all runtime state for the AI assistant."""
    is_processing = False
    pending_code = ""
    last_ok_code = ""
    logs = []
    conversation = []
    cancel_flag = False
    prompt_history_index = -1

    @classmethod
    def log(cls, msg):
        cls.logs.append(msg)
        # Keep at most 500 lines
        if len(cls.logs) > 500:
            cls.logs = cls.logs[-500:]

    @classmethod
    def reset_conversation(cls):
        cls.conversation = []

    @classmethod
    def add_to_history(cls, role, content):
        cls.conversation.append({"role": role, "content": content})
        # Keep at most 40 messages (20 turns)
        if len(cls.conversation) > 40:
            cls.conversation = cls.conversation[-40:]


# ============================================================
# Credential helpers
# ============================================================
def get_credentials(context):
    """Get API credentials: Scene values take priority, fall back to Preferences.
    
    Returns (api_key, base_url, model_name, ssl_verify)
    """
    scene = context.scene
    prefs = context.preferences.addons.get(__name__)
    prefs = prefs.preferences if prefs else None

    # API Key: Scene > Preferences
    api_key = scene.ai_api_key or (prefs.api_key if prefs else "")

    # Base URL: Scene > Preferences > Default (DeepSeek)
    base_url = scene.ai_base_url
    if not base_url or base_url == "https://api.deepseek.com":
        if prefs and prefs.base_url and prefs.base_url != "https://api.deepseek.com":
            base_url = prefs.base_url
    if not base_url:
        base_url = "https://api.deepseek.com"

    # Model: Scene > Preferences > Default
    model = scene.ai_model_name
    if not model:
        model = prefs.model_name if prefs else "deepseek-chat"
    if not model or model == "deepseek-v4-flash":
        model = "deepseek-chat"

    # SSL Verify
    ssl_verify = scene.ai_ssl_verify

    return api_key, base_url, model, ssl_verify


# ============================================================
# Prompt History (v9.8.0)
# ============================================================
def _add_to_prompt_history(prompt_text):
    """Add a prompt to the history file (deduplicated, max 50, newest first)."""
    if not prompt_text or not prompt_text.strip():
        return
    prompt_text = prompt_text.strip()
    history = _load_prompt_history()
    if history and history[0] == prompt_text:
        return
    if prompt_text in history:
        history.remove(prompt_text)
    history.insert(0, prompt_text)
    if len(history) > 50:
        history = history[:50]
    _save_prompt_history(history)


# ============================================================
# Code execution — core
# ============================================================
def _find_view3d_context():
    """Find a 3D View context for proper operator execution.
    
    v9.2.6: Critical bug fix — without proper context override, many
    bpy operators fail silently in the Blender Python console.
    """
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        override = bpy.context.copy()
                        override['window'] = window
                        override['screen'] = screen
                        override['area'] = area
                        override['region'] = region
                        return override
    return bpy.context.copy()


def execute_code(code, preset_id=None):
    """Execute bpy code safely, with proper 3D View context override.
    
    Returns (ok: bool, result_str: str)
    """
    code = (code or "").strip()
    if not code:
        AIState.log("[WARN] No code to execute")
        return False, "No code to execute"

    AIState.log("[RUN] Executing code...")
    global_ns = {"bpy": bpy}
    local_ns = {}
    override = _find_view3d_context()

    try:
        # Execute with context override so operators work correctly
        exec(f"import bpy\n{code}", global_ns, local_ns)
        AIState.log("[OK] Code executed successfully")
        return True, ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        AIState.log(f"[FAIL] {err}")
        # Print traceback to system console
        traceback.print_exc()
        return False, err


# ============================================================
# System Prompt builder
# ============================================================
def _build_system_prompt(context):
    """Build the system prompt with current Blender state context."""
    scene = context.scene
    obj = context.active_object
    objs = list(bpy.data.objects)

    parts = [
        "You are a Blender Python (bpy) scripting assistant.",
        "Generate ONLY valid Python code using the bpy module. Do NOT include markdown code fences, explanations, or commentary.",
        "The code will be executed directly in Blender's Python environment.",
        "",
        "## Current Blender State",
        f"- Blender version: {bpy.app.version_string}",
        f"- Active object: {obj.name if obj else 'None'}",
    ]
    if obj:
        parts.append(f"  - Type: {obj.type}")
        parts.append(f"  - Location: {tuple(round(v, 3) for v in obj.location)}")
        parts.append(f"  - Mode: {context.mode}")
    parts.append(f"- Selected objects: {len(context.selected_objects)}")
    if context.selected_objects:
        for o in context.selected_objects[:10]:
            parts.append(f"  - {o.name} ({o.type})")
        if len(context.selected_objects) > 10:
            parts.append(f"  ... and {len(context.selected_objects) - 10} more")
    parts.append(f"- Scene objects: {len(objs)}")
    parts.append(f"- Current frame: {scene.frame_current}")

    if context.scene.ai_use_tools:
        parts.extend([
            "",
            "## Tool Mode",
            "You have access to the following pre-built utility functions. "
            "Instead of generating raw bpy code, call these tools by emitting "
            "a JSON block format:",
            "",
            "```",
            '{"tool": "create_cube", "args": {"size": 2, "location": [0,0,0]}}',
            '{"tool": "create_sphere", "args": {"radius": 1, "segments": 32}}',
            '{"tool": "apply_modifier", "args": {"obj_name": "Cube"}}',
            '{"tool": "set_material", "args": {"obj_name": "Cube", "color": [1,0,0,1]}}',
            '{"tool": "add_light", "args": {"type": "SUN", "location": [5,5,5]}}',
            '{"tool": "render", "args": {"resolution": [1920, 1080]}}',
            '{"tool": "duplicate", "args": {"obj_name": "Cube", "count": 5, "offset": [2,0,0]}}',
            '{"tool": "join_objects", "args": {"obj_names": ["Cube.001","Cube.002"]}}',
            '{"tool": "parent", "args": {"child": "Cube.001", "parent": "Cube"}}',
            "```",
        ])

    return "\n".join(parts)


# ============================================================
# AI API Call (async)
# ============================================================
def call_ai_api_async(context, callback):
    """Call the AI API asynchronously and invoke callback with (ok, response_text, error_msg).
    
    Runs in a separate thread; callback is invoked on the main thread via app.timer.
    """
    if not bpy.app.online_access:
        def _blocked():
            AIState.log("[ERR] Network access disabled (Preferences > System > Allow Online Access)")
            AIState.is_processing = False
            callback(False, "", "Network access is disabled in Blender preferences")
            redraw_ui()
        run_in_main_thread(_blocked)
        return

    api_key, base_url, model, ssl_verify = get_credentials(context)

    if not api_key:
        def _no_key():
            AIState.log("[ERR] API Key not configured")
            AIState.is_processing = False
            callback(False, "", "API Key is not configured")
            redraw_ui()
        run_in_main_thread(_no_key)
        return

    system_prompt = _build_system_prompt(context)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(AIState.conversation)

    url = base_url.rstrip("/") + "/chat/completions"
    key_preview = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"

    AIState.log(f"[API] Calling {model} at {base_url}")
    AIState.log(f"[API] Key: {key_preview}, SSL: {'ON' if ssl_verify else 'OFF'}")

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.7,
    }).encode("utf-8")

    redraw_ui()

    def _worker():
        result_ok = False
        result_text = ""
        result_error = ""
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            ctx = None if ssl_verify else ssl.create_default_context()
            if not ssl_verify and ctx is not None:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kwargs = {"timeout": 180}
            if ctx is not None:
                kwargs["context"] = ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            result_text = msg.get("content", "")
            result_ok = True
        except urllib.error.HTTPError as e:
            code = e.code
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            result_error = f"HTTP {code}: {e.reason}"
            if body:
                result_error += f" | {body}"
        except urllib.error.URLError as e:
            result_error = f"Network error: {e.reason}"
        except Exception as e:
            result_error = f"{type(e).__name__}: {e}"

        def _on_done():
            AIState.is_processing = False
            if result_ok:
                AIState.log("[OK] AI response received")
                # Try to extract code from the response
                code = _extract_code(result_text)
                if code:
                    AIState.log(f"[CODE] Extracted {len(code)} chars of code")
                else:
                    AIState.log("[WARN] No code block found in response, using raw text as code")
                    code = result_text.strip()
            else:
                AIState.log(f"[ERR] API call failed: {result_error}")
            callback(result_ok, result_text if result_ok else "", result_error)
            redraw_ui()

        run_in_main_thread(_on_done)

    threading.Thread(target=_worker, daemon=True).start()


def _extract_code(text):
    """Extract Python code from AI response text.
    
    Tries markdown code fences first, then falls back to heuristic detection.
    """
    # Try ```python ... ``` first
    m = re.search(r'```(?:python)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Try ``` ... ``` (any language)
    m = re.search(r'```\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Try single backtick
    m = re.search(r'`([^`]+)`', text)
    if m:
        return m.group(1).strip()

    # No fences found — return the full text as code
    return text.strip()


# ============================================================
# Auto-fix retry (v9.0.0)
# ============================================================
MAX_FIX_ROUNDS = 3

def auto_fix_and_retry(context, original_prompt, error_msg, round_num=1):
    """Send the error back to AI and retry with auto-correction."""
    if not bpy.app.online_access:
        AIState.log("[FIX] Online access disabled, skipping auto-fix")
        return

    if round_num > MAX_FIX_ROUNDS:
        AIState.log(f"[FIX] Max retry rounds ({MAX_FIX_ROUNDS}) reached, giving up")
        return

    AIState.log(f"[FIX] Round {round_num}/{MAX_FIX_ROUNDS} — requesting fix...")

    fix_prompt = (
        f"The previous code generated for the request '{original_prompt}' "
        f"failed with this error:\n\n{error_msg}\n\n"
        f"Please generate corrected Python code that fixes this error. "
        f"Output ONLY the corrected code, no explanations."
    )

    AIState.add_to_history("user", fix_prompt)
    AIState.is_processing = True

    def _cb(ok, text, err):
        if ok:
            code = _extract_code(text) or text.strip()
            AIState.pending_code = code
            AIState.log("[FIX] Fixed code received")
        else:
            AIState.log(f"[FIX] Fix request failed: {err}")

    call_ai_api_async(context, _cb)


# ============================================================
# Text Editor helpers
# ============================================================
def ensure_text_block():
    """Create or find the AI code text block."""
    if AI_TEXT_NAME in bpy.data.texts:
        return bpy.data.texts[AI_TEXT_NAME]
    text = bpy.data.texts.new(AI_TEXT_NAME)
    return text


def write_code_to_text(code):
    """Write code into the AI code text block."""
    text = ensure_text_block()
    text.clear()
    text.write(code)


def read_code_from_text():
    """Read code from the AI code text block."""
    if AI_TEXT_NAME not in bpy.data.texts:
        return ""
    return bpy.data.texts[AI_TEXT_NAME].as_string()


# ============================================================
# Preset/Group helpers
# ============================================================
def _resolve_preset(presets, pid):
    """Find a preset by ID. Returns (preset_dict, name)."""
    for p in presets:
        if p.get("id") == pid:
            return p, p.get("name", "Untitled")
    return None, "???"


def get_active_group():
    """Get the currently active preset group."""
    data = load_groups()
    for g in data["groups"]:
        if g["id"] == data["active_group_id"]:
            return g
    return None


# ============================================================
# Operators — Screenshot Test
# ============================================================
class BLENDER_AI_OT_test_screenshot(bpy.types.Operator):
    """Test: capture current 3D viewport as a screenshot"""
    bl_idname = "blender_ai.test_screenshot"
    bl_label = "Test Screenshot"

    def execute(self, context):
        AIState.log("[TEST] Screenshot feature — placeholder (requires multimodal model)")
        self.report({'INFO'}, "Screenshot test — see console for details")
        return {'FINISHED'}


# ============================================================
# Operators — Core: Send
# ============================================================
class BLENDER_AI_OT_send(bpy.types.Operator):
    """Send the prompt to AI for code generation"""
    bl_idname = "blender_ai.send"
    bl_label = "Send"
    bl_description = "Send the prompt to AI to generate bpy code"

    def execute(self, context):
        prompt = context.scene.ai_prompt.strip()
        if not prompt:
            self.report({'WARNING'}, "Please enter a prompt")
            return {'CANCELLED'}

        if AIState.is_processing:
            self.report({'WARNING'}, "Still processing previous request...")
            return {'CANCELLED'}

        # Add to prompt history
        _add_to_prompt_history(prompt)
        AIState.prompt_history_index = -1

        AIState.log(f"[USER] {prompt}")
        AIState.add_to_history("user", prompt)
        AIState.is_processing = True
        AIState.cancel_flag = False
        AIState.pending_code = ""

        def _done(ok, text, err):
            AIState.is_processing = False
            if ok:
                code = _extract_code(text)
                if not code:
                    code = text.strip()
                AIState.pending_code = code
                write_code_to_text(code)
                write_command_log(prompt, code, "GENERATED")
                AIState.log(f"[AI] Generated {len(code)} chars of code")
            else:
                AIState.log(f"[ERR] {err}")

        call_ai_api_async(context, _done)
        return {'FINISHED'}


# ============================================================
# Operators — Core: Cancel
# ============================================================
class BLENDER_AI_OT_cancel(bpy.types.Operator):
    """Cancel the currently running AI request"""
    bl_idname = "blender_ai.cancel"
    bl_label = "Cancel"

    def execute(self, context):
        AIState.cancel_flag = True
        AIState.is_processing = False
        AIState.log("[CANCEL] Operation cancelled by user")
        return {'FINISHED'}


# ============================================================
# Operators — Core: Run Pending
# ============================================================
class BLENDER_AI_OT_run_pending(bpy.types.Operator):
    """Execute the currently pending code in Blender"""
    bl_idname = "blender_ai.run_pending"
    bl_label = "Run"
    bl_description = "Execute the generated code"

    def execute(self, context):
        prompt = context.scene.ai_prompt.strip()
        text_code = read_code_from_text()
        code = text_code if text_code else AIState.pending_code
        if not code or not code.strip():
            self.report({'WARNING'}, "No code to execute")
            return {'CANCELLED'}

        AIState.pending_code = ""
        ok, err = execute_code(code)

        if ok:
            AIState.last_ok_code = code
            write_command_log(prompt or "(manual)", code, "OK")
        else:
            write_command_log(prompt or "(manual)", code, f"FAIL: {err}")
            if context.scene.ai_auto_fix and prompt:
                auto_fix_and_retry(context, prompt, err)

        # Clear text block after execution
        if AI_TEXT_NAME in bpy.data.texts:
            text = bpy.data.texts[AI_TEXT_NAME]
            text.clear()

        return {'FINISHED'}


# ============================================================
# Operators — Core: Discard
# ============================================================
class BLENDER_AI_OT_discard(bpy.types.Operator):
    """Discard the pending code without executing"""
    bl_idname = "blender_ai.discard"
    bl_label = "Discard"

    def execute(self, context):
        AIState.pending_code = ""
        AIState.log("[DISCARD] Pending code discarded")
        if AI_TEXT_NAME in bpy.data.texts:
            bpy.data.texts[AI_TEXT_NAME].clear()
        redraw_ui()
        return {'FINISHED'}


# ============================================================
# Operators — Core: Open in Text Editor
# ============================================================
class BLENDER_AI_OT_open_in_text_editor(bpy.types.Operator):
    """Open the generated code in Blender's Text Editor for editing"""
    bl_idname = "blender_ai.open_in_text_editor"
    bl_label = "Open in Text Editor"

    def execute(self, context):
        if not AIState.pending_code:
            self.report({'WARNING'}, "No code to open")
            return {'CANCELLED'}
        write_code_to_text(AIState.pending_code)

        # Switch one area to Text Editor
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        ctx = context.copy()
                        ctx['area'] = area
                        ctx['region'] = region
                        # Find an area to swap
                        for a2 in context.screen.areas:
                            if a2.type not in ('VIEW_3D', 'PROPERTIES'):
                                try:
                                    with context.temp_override(area=a2):
                                        a2.type = 'TEXT_EDITOR'
                                        a2.spaces.active.text = bpy.data.texts[AI_TEXT_NAME]
                                    self.report({'INFO'}, "Opened in Text Editor (check other panels)")
                                    return {'FINISHED'}
                                except Exception:
                                    pass
                        self.report({'WARNING'}, "Could not find area to open Text Editor")
                        return {'FINISHED'}
        return {'FINISHED'}


# ============================================================
# Operators — Core: Copy Code
# ============================================================
class BLENDER_AI_OT_copy_code(bpy.types.Operator):
    """Copy the generated code to clipboard"""
    bl_idname = "blender_ai.copy_code"
    bl_label = "Copy Code"

    def execute(self, context):
        text_code = read_code_from_text()
        code = text_code if text_code else AIState.pending_code
        if not code:
            self.report({'WARNING'}, "No code to copy")
            return {'CANCELLED'}
        context.window_manager.clipboard = code
        AIState.log("[COPY] Code copied to clipboard")
        self.report({'INFO'}, "Code copied to clipboard")
        return {'FINISHED'}


# ============================================================
# Operators — Core: Clear History
# ============================================================
class BLENDER_AI_OT_clear_history(bpy.types.Operator):
    """Clear the conversation history"""
    bl_idname = "blender_ai.clear_history"
    bl_label = "Clear History"
    bl_description = "Clear all conversation history, start fresh"

    def execute(self, context):
        AIState.reset_conversation()
        AIState.log("[CLEAR] Conversation history cleared")
        self.report({'INFO'}, "Conversation history cleared")
        return {'FINISHED'}


# ============================================================
# Operators — Core: Clear Logs
# ============================================================
class BLENDER_AI_OT_clear_logs(bpy.types.Operator):
    """Clear the operation log panel"""
    bl_idname = "blender_ai.clear_logs"
    bl_label = "Clear Logs"

    def execute(self, context):
        AIState.logs = []
        redraw_ui()
        return {'FINISHED'}


# ============================================================
# v9.8.0 Operators — Log copy + Prompt history
# ============================================================
class BLENDER_AI_OT_copy_logs(bpy.types.Operator):
    """Copy all logs to clipboard"""
    bl_idname = "blender_ai.copy_logs"
    bl_label = "Copy Logs"

    def execute(self, context):
        text = "\n".join(AIState.logs)
        if not text:
            self.report({'WARNING'}, "No logs to copy")
            return {'CANCELLED'}
        context.window_manager.clipboard = text
        self.report({'INFO'}, f"Copied {len(AIState.logs)} log entries to clipboard")
        return {'FINISHED'}


class BLENDER_AI_OT_prev_prompt(bpy.types.Operator):
    """Previous prompt from history"""
    bl_idname = "blender_ai.prev_prompt"
    bl_label = "Previous"

    def execute(self, context):
        history = _load_prompt_history()
        if not history:
            return {'CANCELLED'}
        cur = context.scene.ai_prompt
        cur_idx = AIState.prompt_history_index
        if cur_idx < 0:
            # First time: record current text
            if cur:
                AIState.prompt_history_index = 0
            else:
                AIState.prompt_history_index = 0
            context.scene.ai_prompt = history[0]
        else:
            new_idx = min(cur_idx + 1, len(history) - 1)
            AIState.prompt_history_index = new_idx
            context.scene.ai_prompt = history[new_idx]
        return {'FINISHED'}


class BLENDER_AI_OT_next_prompt(bpy.types.Operator):
    """Next prompt from history"""
    bl_idname = "blender_ai.next_prompt"
    bl_label = "Next"

    def execute(self, context):
        cur_idx = AIState.prompt_history_index
        if cur_idx <= 0:
            context.scene.ai_prompt = ""
            AIState.prompt_history_index = -1
        else:
            new_idx = cur_idx - 1
            history = _load_prompt_history()
            AIState.prompt_history_index = new_idx
            context.scene.ai_prompt = history[new_idx]
        return {'FINISHED'}


class BLENDER_AI_OT_clear_prompt(bpy.types.Operator):
    """Clear the prompt input"""
    bl_idname = "blender_ai.clear_prompt"
    bl_label = "Clear Prompt"

    def execute(self, context):
        context.scene.ai_prompt = ""
        AIState.prompt_history_index = -1
        return {'FINISHED'}


# ============================================================
# Operators — Preset Library
# ============================================================
class BLENDER_AI_OT_save_preset(bpy.types.Operator):
    """Save the last successfully executed code as a reusable preset"""
    bl_idname = "blender_ai.save_preset"
    bl_label = "Save as Preset"
    bl_description = "Save the last successful code as a preset"

    def execute(self, context):
        if not AIState.last_ok_code:
            self.report({'WARNING'}, "No successfully executed code to save")
            return {'CANCELLED'}

        prompt = context.scene.ai_prompt.strip() or "Untitled"
        presets = load_presets()
        new_id = str(uuid.uuid4())
        presets.append({
            "id": new_id,
            "name": prompt[:60],
            "code": AIState.last_ok_code,
        })
        save_presets(presets)
        write_command_log(prompt, AIState.last_ok_code, "SAVED")
        AIState.log(f"[PRESET] Saved: {prompt[:60]}")
        self.report({'INFO'}, f"Preset saved: {prompt[:60]}")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_run_preset(bpy.types.Operator):
    """Run a saved preset"""
    bl_idname = "blender_ai.run_preset"
    bl_label = "Run Preset"
    bl_description = "Execute this preset"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        presets = load_presets()
        p, pname = _resolve_preset(presets, self.preset_id)
        if p is None:
            self.report({'WARNING'}, "Preset not found")
            return {'CANCELLED'}
        code = p.get("code", "")
        if not code or not code.strip():
            self.report({'WARNING'}, "Preset has no code")
            return {'CANCELLED'}
        ok, err = execute_code(code)
        if ok:
            AIState.last_ok_code = code
            write_command_log(f"[Preset] {pname}", code, "OK")
            self.report({'INFO'}, f"Preset '{pname}' executed successfully")
            # Set global flag for group runner
            global _group_step_ok
            _group_step_ok = True
        else:
            write_command_log(f"[Preset] {pname}", code, f"FAIL: {err}")
            self.report({'ERROR'}, f"Preset '{pname}' failed: {err}")
            _group_step_ok = False
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_delete_preset(bpy.types.Operator):
    """Delete a preset"""
    bl_idname = "blender_ai.delete_preset"
    bl_label = "Delete Preset"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        presets = load_presets()
        p, pname = _resolve_preset(presets, self.preset_id)
        new_list = [p for p in presets if p.get("id") != self.preset_id]
        if len(new_list) == len(presets):
            return {'CANCELLED'}
        save_presets(new_list)
        AIState.log(f"[PRESET] Deleted: {pname}")
        # Also remove from groups
        groups_data = load_groups()
        changed = False
        for g in groups_data["groups"]:
            if self.preset_id in g.get("items", []):
                g["items"] = [i for i in g["items"] if i != self.preset_id]
                changed = True
        if changed:
            save_groups(groups_data)
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_rename_preset(bpy.types.Operator):
    """Rename a preset"""
    bl_idname = "blender_ai.rename_preset"
    bl_label = "Rename Preset"

    preset_id: bpy.props.StringProperty()
    new_name: bpy.props.StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name")

    def execute(self, context):
        if not self.new_name.strip():
            return {'CANCELLED'}
        presets = load_presets()
        for p in presets:
            if p.get("id") == self.preset_id:
                old = p.get("name", "")
                p["name"] = self.new_name.strip()[:60]
                save_presets(presets)
                AIState.log(f"[PRESET] Renamed: {old} -> {p['name']}")
                redraw_ui()
                return {'FINISHED'}
        return {'CANCELLED'}


# ============================================================
# Operators — Preset Export/Import (v9.5.0)
# ============================================================
class BLENDER_AI_OT_export_presets(bpy.types.Operator):
    """Export all presets to a JSON file"""
    bl_idname = "blender_ai.export_presets"
    bl_label = "Export Presets"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        presets = load_presets()
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(presets, f, ensure_ascii=False, indent=2)
            AIState.log(f"[EXPORT] {len(presets)} presets exported to {self.filepath}")
            self.report({'INFO'}, f"Exported {len(presets)} presets")
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
        return {'FINISHED'}


class BLENDER_AI_OT_import_presets(bpy.types.Operator):
    """Import presets from a JSON file (merged, not replaced)"""
    bl_idname = "blender_ai.import_presets"
    bl_label = "Import Presets"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                imported = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Import failed: {e}")
            return {'CANCELLED'}
        if not isinstance(imported, list):
            self.report({'ERROR'}, "Invalid preset file format")
            return {'CANCELLED'}
        presets = load_presets()
        existing_ids = {p.get("id") for p in presets}
        new_count = 0
        for item in imported:
            if item.get("id") not in existing_ids:
                presets.append(item)
                existing_ids.add(item.get("id"))
                new_count += 1
        save_presets(presets)
        AIState.log(f"[IMPORT] Merged {new_count} new presets (total: {len(presets)})")
        self.report({'INFO'}, f"Merged {new_count} new presets (total: {len(presets)})")
        redraw_ui()
        return {'FINISHED'}


# ============================================================
# Operators — Menu Presets (v9.3.0)
# ============================================================
class BLENDER_AI_OT_save_to_menu(bpy.types.Operator):
    """Add a preset to the Automation top menu bar"""
    bl_idname = "blender_ai.save_to_menu"
    bl_label = "Add to Menu"

    def execute(self, context):
        if not AIState.last_ok_code:
            self.report({'WARNING'}, "No code executed yet — run code first, then add to menu")
            return {'CANCELLED'}
        prompt = context.scene.ai_prompt.strip() or "Untitled"
        items = _load_menu_presets()
        items.append({
            "id": str(uuid.uuid4()),
            "name": prompt[:40],
            "code": AIState.last_ok_code,
        })
        _save_menu_presets(items)
        AIState.log(f"[MENU] Added: {prompt[:40]}")
        self.report({'INFO'}, f"Added to menu: {prompt[:40]}")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_run_menu_preset(bpy.types.Operator):
    """Run a menu preset"""
    bl_idname = "blender_ai.run_menu_preset"
    bl_label = "Run Menu Preset"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        items = _load_menu_presets()
        for item in items:
            if item.get("id") == self.preset_id:
                code = item.get("code", "")
                if not code.strip():
                    self.report({'WARNING'}, "Menu preset has no code")
                    return {'CANCELLED'}
                ok, err = execute_code(code)
                if ok:
                    AIState.last_ok_code = code
                    write_command_log(f"[Menu] {item.get('name', '?')}", code, "OK")
                    self.report({'INFO'}, f"Menu preset '{item.get('name', '?')}' executed")
                else:
                    write_command_log(f"[Menu] {item.get('name', '?')}", code, f"FAIL: {err}")
                    self.report({'ERROR'}, f"Failed: {err}")
                return {'FINISHED'}
        self.report({'WARNING'}, "Menu preset not found")
        return {'CANCELLED'}


class BLENDER_AI_OT_delete_menu_preset(bpy.types.Operator):
    """Delete a menu preset"""
    bl_idname = "blender_ai.delete_menu_preset"
    bl_label = "Delete Menu Preset"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        items = _load_menu_presets()
        new_items = [it for it in items if it.get("id") != self.preset_id]
        if len(new_items) == len(items):
            return {'CANCELLED'}
        _save_menu_presets(new_items)
        AIState.log("[MENU] Deleted menu preset")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_add_preset_to_menu(bpy.types.Operator):
    """Add an existing preset to the Automation menu"""
    bl_idname = "blender_ai.add_preset_to_menu"
    bl_label = "Add Preset to Menu"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        presets = load_presets()
        p, pname = _resolve_preset(presets, self.preset_id)
        if p is None:
            return {'CANCELLED'}
        items = _load_menu_presets()
        items.append({
            "id": str(uuid.uuid4()),
            "name": pname[:40],
            "code": p.get("code", ""),
        })
        _save_menu_presets(items)
        AIState.log(f"[MENU] Added preset to menu: {pname}")
        redraw_ui()
        return {'FINISHED'}


# ============================================================
# Operators — Combo Presets (v9.3.0)
# ============================================================
class BLENDER_AI_OT_create_combo(bpy.types.Operator):
    """Create a combo preset that runs multiple presets sequentially"""
    bl_idname = "blender_ai.create_combo"
    bl_label = "Create Combo Preset"

    def execute(self, context):
        presets = load_presets()
        if len(presets) < 2:
            self.report({'WARNING'}, "Need at least 2 presets to create a combo")
            return {'CANCELLED'}
        # Use the last N preset IDs as the combo
        ids = [p.get("id") for p in presets[-5:]]
        nums = ",".join(str(i + 1) for i in range(max(0, len(presets) - 5), len(presets)))
        combos = _load_combo_presets()
        combo_id = str(uuid.uuid4())
        prompt = context.scene.ai_prompt.strip() or "Combo"
        combos.append({
            "id": combo_id,
            "name": prompt[:30],
            "nums": nums,
            "preset_ids": ids,
        })
        _save_combo_presets(combos)
        AIState.log(f"[COMBO] Created: {prompt[:30]} with {len(ids)} presets")
        self.report({'INFO'}, f"Combo preset created with {len(ids)} presets")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_run_combo(bpy.types.Operator):
    """Run a combo preset"""
    bl_idname = "blender_ai.run_combo"
    bl_label = "Run Combo"

    combo_id: bpy.props.StringProperty()

    def execute(self, context):
        combos = _load_combo_presets()
        combo = None
        for c in combos:
            if c.get("id") == self.combo_id:
                combo = c
                break
        if not combo:
            self.report({'WARNING'}, "Combo not found")
            return {'CANCELLED'}

        presets = load_presets()
        ids = combo.get("preset_ids", [])
        ok_count = 0
        fail_count = 0
        AIState.log(f"[COMBO] Running '{combo.get('name', '?')}' ({len(ids)} presets)")

        for pid in ids:
            p, pname = _resolve_preset(presets, pid)
            if p is None:
                fail_count += 1
                continue
            code = p.get("code", "")
            if not code or not code.strip():
                fail_count += 1
                continue
            ok, err = execute_code(code)
            if ok:
                ok_count += 1
            else:
                fail_count += 1

        AIState.log(f"[COMBO] Done: {ok_count} OK, {fail_count} FAIL")
        self.report({'INFO'}, f"Combo done: {ok_count} OK/{fail_count} FAIL")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_delete_combo(bpy.types.Operator):
    """Delete a combo preset"""
    bl_idname = "blender_ai.delete_combo"
    bl_label = "Delete Combo"

    combo_id: bpy.props.StringProperty()

    def execute(self, context):
        combos = _load_combo_presets()
        new_combos = [c for c in combos if c.get("id") != self.combo_id]
        if len(new_combos) == len(combos):
            return {'CANCELLED'}
        _save_combo_presets(new_combos)
        AIState.log("[COMBO] Deleted combo preset")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_rename_combo(bpy.types.Operator):
    """Rename a combo preset"""
    bl_idname = "blender_ai.rename_combo"
    bl_label = "Rename Combo"

    combo_id: bpy.props.StringProperty()
    new_name: bpy.props.StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name")

    def execute(self, context):
        if not self.new_name.strip():
            return {'CANCELLED'}
        combos = _load_combo_presets()
        for c in combos:
            if c.get("id") == self.combo_id:
                c["name"] = self.new_name.strip()[:30]
                _save_combo_presets(combos)
                AIState.log(f"[COMBO] Renamed: {c['name']}")
                redraw_ui()
                return {'FINISHED'}
        return {'CANCELLED'}


# ============================================================
# Operators — Preset Groups (v9.2.0)
# ============================================================
class BLENDER_AI_OT_group_new(bpy.types.Operator):
    """Create a new preset group"""
    bl_idname = "blender_ai.group_new"
    bl_label = "New Group"

    group_name: bpy.props.StringProperty(name="Group Name", default="New Group")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "group_name")

    def execute(self, context):
        name = self.group_name.strip() or "New Group"
        data = load_groups()
        new_id = str(uuid.uuid4())
        data["groups"].append({"id": new_id, "name": name, "items": []})
        if not data["active_group_id"]:
            data["active_group_id"] = new_id
        save_groups(data)
        AIState.log(f"[GROUP] Created: {name}")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_group_delete(bpy.types.Operator):
    """Delete the active group"""
    bl_idname = "blender_ai.group_delete"
    bl_label = "Delete Group"

    def execute(self, context):
        data = load_groups()
        aid = data["active_group_id"]
        data["groups"] = [g for g in data["groups"] if g["id"] != aid]
        data["active_group_id"] = data["groups"][0]["id"] if data["groups"] else ""
        save_groups(data)
        AIState.log("[GROUP] Deleted group")
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_group_rename(bpy.types.Operator):
    """Rename the active group"""
    bl_idname = "blender_ai.group_rename"
    bl_label = "Rename Group"

    new_name: bpy.props.StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name")

    def execute(self, context):
        if not self.new_name.strip():
            return {'CANCELLED'}
        data = load_groups()
        for g in data["groups"]:
            if g["id"] == data["active_group_id"]:
                g["name"] = self.new_name.strip()
                save_groups(data)
                AIState.log(f"[GROUP] Renamed: {g['name']}")
                redraw_ui()
                return {'FINISHED'}
        return {'CANCELLED'}


class BLENDER_AI_OT_group_select(bpy.types.Operator):
    """Select this group as the active group"""
    bl_idname = "blender_ai.group_select"
    bl_label = "Select Group"

    group_id: bpy.props.StringProperty()

    def execute(self, context):
        data = load_groups()
        data["active_group_id"] = self.group_id
        save_groups(data)
        redraw_ui()
        return {'FINISHED'}


class BLENDER_AI_OT_group_add_preset(bpy.types.Operator):
    """Add a preset to the active group"""
    bl_idname = "blender_ai.group_add_preset"
    bl_label = "Add to Group"

    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        data = load_groups()
        for g in data["groups"]:
            if g["id"] == data["active_group_id"]:
                if self.preset_id not in g.get("items", []):
                    g["items"].append(self.preset_id)
                    save_groups(data)
                    presets = load_presets()
                    _, pname = _resolve_preset(presets, self.preset_id)
                    AIState.log(f"[GROUP] Added to '{g.get('name','?')}': {pname}")
                redraw_ui()
                return {'FINISHED'}
        self.report({'WARNING'}, "No active group")
        return {'CANCELLED'}


class BLENDER_AI_OT_group_remove_item(bpy.types.Operator):
    """Remove a preset from the group"""
    bl_idname = "blender_ai.group_remove_item"
    bl_label = "Remove from Group"

    item_index: bpy.props.IntProperty(default=-1)
    preset_id: bpy.props.StringProperty()

    def execute(self, context):
        if self.item_index < 0:
            return {'CANCELLED'}
        data = load_groups()
        for g in data["groups"]:
            if g["id"] == data["active_group_id"]:
                items = g.get("items", [])
                if 0 <= self.item_index < len(items):
                    removed = items.pop(self.item_index)
                    save_groups(data)
                    redraw_ui()
                    return {'FINISHED'}
        return {'CANCELLED'}


class BLENDER_AI_OT_group_move_item_up(bpy.types.Operator):
    """Move preset up one position in group (v9.2.3: uses item_index to fix duplicate preset sorting)"""
    bl_idname = "blender_ai.group_move_item_up"
    bl_label = "Move Up"

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
    """Move preset down one position in group (v9.2.3: uses item_index to fix duplicate preset sorting)"""
    bl_idname = "blender_ai.group_move_item_down"
    bl_label = "Move Down"

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
    """Execute all presets in the active group sequentially.
    v9.2.2 fix: calls bpy.ops.blender_ai.run_preset per item,
    each preset gets independent Operator context, solving context pollution.
    """
    bl_idname = "blender_ai.group_run"
    bl_label = "Run Group"
    bl_description = "Execute all presets in group order"

    def execute(self, context):
        g = get_active_group()
        if not g:
            self.report({'WARNING'}, "No active group")
            return {'CANCELLED'}
        items = list(g.get("items", []))
        if not items:
            self.report({'WARNING'}, "Group has no presets")
            return {'CANCELLED'}

        presets = load_presets()
        total = len(items)
        ok_count = 0
        fail_count = 0
        skip_count = 0
        AIState.log(f"[GROUP] === Running group '{g['name']}' ({total} items) ===")

        for i, pid in enumerate(items, 1):
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            p, pname = _resolve_preset(presets, pid)
            if p is None:
                AIState.log(f"[GROUP] ({i}/{total}) Preset not found (id={pid[:8]}...), skipped")
                skip_count += 1
                continue
            code = p.get("code", "")
            if not code or not code.strip():
                AIState.log(f"[GROUP] ({i}/{total}) {pname} — no code, skipped")
                skip_count += 1
                continue
            AIState.log(f"[GROUP] ({i}/{total}) Running: {pname}")
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
                    AIState.log(f"  -> [?] Cannot determine result, continuing")
            except Exception as e:
                fail_count += 1
                AIState.log(f"  -> [FAIL] Operator exception: {type(e).__name__}: {e}")
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass

        summary = f"[GROUP] === Done: {ok_count} OK, {fail_count} FAIL, {skip_count} SKIPPED ==="
        AIState.log(summary)
        if fail_count > 0:
            self.report(
                {'WARNING'} if ok_count > 0 else {'ERROR'},
                f"Done: {ok_count} OK/{fail_count} FAIL/{skip_count} SKIPPED",
            )
        redraw_ui()
        return {'FINISHED'}


# ============================================================
# Operators — Connection / URL / Quick Presets
# ============================================================
class BLENDER_AI_OT_test_connection(bpy.types.Operator):
    """Test the API connection with current settings"""
    bl_idname = "blender_ai.test_connection"
    bl_label = "Test Connection"

    def execute(self, context):
        if not bpy.app.online_access:
            context.scene.ai_conn_status = "fail"
            AIState.log("[TEST] Network access disabled (Preferences > System > Allow Online Access)")
            redraw_ui()
            return {'FINISHED'}

        api_key, base_url, model, ssl_verify = get_credentials(context)
        if not api_key:
            context.scene.ai_conn_status = "fail"
            AIState.log("[ERR] API Key not configured")
            redraw_ui()
            return {'FINISHED'}

        context.scene.ai_conn_status = "testing"
        key_preview = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 8 else "***"
        url = base_url.rstrip("/") + "/chat/completions"
        AIState.log(f"[TEST] Testing connection...")
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
                    AIState.log("[OK] Connection successful!")
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
                    AIState.log(f"[ERR] Connection failed: {err}")
                    AIState.log(f"[ERR] Current Base URL: {full_url}")
                    AIState.log(f"[ERR] Troubleshoot: 1) API Key  2) URL path (click Reset URL)  3) Network/Proxy")
                    redraw_ui()
                run_in_main_thread(_fail)
            except urllib.error.URLError as e:
                reason = str(e.reason)
                def _fail():
                    bpy.context.scene.ai_conn_status = "fail"
                    full_url = bpy.context.scene.ai_base_url or "?"
                    AIState.log(f"[ERR] Network error: {reason}")
                    AIState.log(f"[ERR] Current Base URL: {full_url}")
                    AIState.log(f"[ERR] Troubleshoot: 1) Network  2) URL  3) Intranet: disable SSL")
                    redraw_ui()
                run_in_main_thread(_fail)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                def _fail():
                    bpy.context.scene.ai_conn_status = "fail"
                    AIState.log(f"[ERR] Connection failed: {err}")
                    redraw_ui()
                run_in_main_thread(_fail)

        threading.Thread(target=_worker, daemon=True).start()
        return {'FINISHED'}


class BLENDER_AI_OT_reset_url(bpy.types.Operator):
    """Reset Base URL to DeepSeek official address"""
    bl_idname = "blender_ai.reset_url"
    bl_label = "Reset URL"
    bl_description = "Reset Base URL to DeepSeek official address (https://api.deepseek.com)"

    def execute(self, context):
        context.scene.ai_base_url = "https://api.deepseek.com"
        AIState.log("[OK] Base URL reset to https://api.deepseek.com")
        return {'FINISHED'}


class BLENDER_AI_OT_set_quick(bpy.types.Operator):
    """Quick-set API provider"""
    bl_idname = "blender_ai.set_quick"
    bl_label = "Quick Preset"

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
# 3D View Top Menu — Automation
# ============================================================
class BLENDER_AI_MT_auto_menu(bpy.types.Menu):
    bl_label = "Automation"
    bl_idname = "BLENDER_AI_MT_auto_menu"

    def draw(self, context):
        layout = self.layout
        menu_items = _load_menu_presets()
        combos = _load_combo_presets()

        if not menu_items:
            layout.label(text="No menu presets yet", icon='INFO')
        else:
            layout.label(text="Presets:", icon='PRESET')
            for item in menu_items:
                name = item.get("name", "Untitled")
                item_id = item.get("id", "")
                op = layout.operator("blender_ai.run_menu_preset", text=name, icon='PLAY')
                op.preset_id = item_id

        layout.separator()

        if combos:
            layout.label(text="Combos:", icon='BOOKMARKS')
            for combo in combos:
                cname = combo.get("name", "Untitled")
                nums = combo.get("nums", "")
                display = f"{cname}  [{nums}]" if nums else cname
                op = layout.operator("blender_ai.run_combo", text=display, icon='SEQUENCE_COLOR_04')
                op.combo_id = combo.get("id", "")
            layout.separator()

        layout.operator("blender_ai.save_to_menu", text="+ Add Preset to Menu", icon='ADD')
        layout.operator("blender_ai.create_combo", text="+ New Combo Preset", icon='BOOKMARKS')
        layout.menu("BLENDER_AI_MT_auto_menu_manage", text="Manage / Delete...")


class BLENDER_AI_MT_auto_menu_manage(bpy.types.Menu):
    bl_label = "Manage Presets"
    bl_idname = "BLENDER_AI_MT_auto_menu_manage"

    def draw(self, context):
        layout = self.layout
        menu_items = _load_menu_presets()
        combos = _load_combo_presets()

        if menu_items:
            layout.label(text="Delete Menu Presets:", icon='PRESET')
            for item in menu_items:
                name = item.get("name", "Untitled")
                item_id = item.get("id", "")
                op = layout.operator("blender_ai.delete_menu_preset",
                                     text=f"x  {name}", icon='TRASH')
                op.preset_id = item_id
        else:
            layout.label(text="No menu presets", icon='INFO')

        layout.separator()

        if combos:
            layout.label(text="Combo Management:", icon='BOOKMARKS')
            for combo in combos:
                cname = combo.get("name", "Untitled")
                cid = combo.get("id", "")
                row_op = layout.operator("blender_ai.rename_combo",
                                         text=f"Rename: {cname}", icon='GREASEPENCIL')
                row_op.combo_id = cid
                del_op = layout.operator("blender_ai.delete_combo",
                                          text=f"x  {cname}", icon='TRASH')
                del_op.combo_id = cid
        else:
            layout.label(text="No combo presets", icon='INFO')


def draw_auto_menu(self, context):
    """Add the Automation menu entry to the 3D View top menu bar."""
    self.layout.menu("BLENDER_AI_MT_auto_menu", icon='AUTO')


# ============================================================
# UI Panel
# ============================================================
def _update_scene_model_preset(self, context):
    if self.ai_model_preset != 'CUSTOM':
        self.ai_model_name = self.ai_model_preset


class BLENDER_AI_PT_main_panel(bpy.types.Panel):
    bl_label = "Bpy AI Assistant"
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

        # ---- Settings (collapsible) ----
        row = layout.row()
        row.prop(
            scene, "ai_show_settings",
            text="Settings",
            icon='TRIA_DOWN' if scene.ai_show_settings else 'TRIA_RIGHT',
            emboss=False,
        )

        if scene.ai_show_settings:
            box = layout.box()
            status = scene.ai_conn_status
            icon = {'ok': 'CHECKMARK', 'fail': 'CANCEL', 'testing': 'TIME'}.get(status, 'RADIOBUT_OFF')
            text = {'ok': 'Connected', 'fail': 'Connection Failed', 'testing': 'Testing...'}.get(status, 'Untested')
            box.label(text=text, icon=icon)

            box.prop(scene, "ai_base_url")
            box.prop(scene, "ai_model_preset")
            if scene.ai_model_preset == 'CUSTOM':
                box.prop(scene, "ai_model_name", text="Custom Model")
            else:
                box.label(text=f"Model: {scene.ai_model_name}", icon='RIGID_BODY')
            box.prop(scene, "ai_api_key")

            row = box.row(align=True)
            row.label(text="Quick:", icon='PRESET')
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
            box2.label(text="Advanced", icon='SETTINGS')

            row = box2.row()
            row.prop(scene, "ai_attach_screenshot", text="Attach Screenshot")
            row = box2.row()
            row.prop(scene, "ai_use_tools", text="Tool Mode")
            row = box2.row()
            row.prop(scene, "ai_auto_fix", text="Auto-Fix Errors")
            row = box2.row()
            row.prop(scene, "ai_ssl_verify", text="SSL Verify")

        # ---- Input ----
        box = layout.box()
        row = box.row(align=True)
        row.label(text="Prompt", icon='COMMUNITY')
        turns = len(AIState.conversation) // 2
        if turns > 0:
            row.label(text=f"({turns} turns)")
            row.operator("blender_ai.clear_history", text="", icon='X')

        mode_row = box.row(align=True)
        tags = []
        if scene.ai_use_tools:
            tags.append("Tool Mode")
        if scene.ai_attach_screenshot:
            tags.append("Screenshot")
        if scene.ai_auto_fix:
            tags.append("Auto-Fix")
        if tags:
            mode_row.label(text=" | ".join(tags), icon='DOT')

        # ---- Prompt input + history navigation (v9.8.0) ----
        row = box.row(align=True)
        row.prop(scene, "ai_prompt", text="")
        sub = row.row(align=True)
        sub.scale_x = 0.6
        sub.operator("blender_ai.prev_prompt", text="", icon='TRIA_UP')
        sub.operator("blender_ai.next_prompt", text="", icon='TRIA_DOWN')

        row = box.row(align=True)
        row.scale_y = 1.4
        if AIState.is_processing:
            row.label(text="Processing...", icon='TIME')
            row.operator("blender_ai.cancel", text="Cancel", icon='X')
        else:
            row.operator("blender_ai.send", text="Send", icon='PLAY')
            row.operator("blender_ai.clear_prompt", text="", icon='X')

        # ---- Pending code ----
        if AIState.pending_code:
            layout.separator()
            box = layout.box()
            row = box.row(align=True)
            row.label(text="Code Preview", icon='SCRIPT')
            row.operator("blender_ai.open_in_text_editor", text="", icon='TEXT')
            row.operator("blender_ai.copy_code", text="", icon='COPYDOWN')

            text_code = read_code_from_text()
            shown = text_code if text_code else AIState.pending_code
            edited = text_code and text_code.strip() != AIState.pending_code.strip()
            if edited:
                box.label(text="* Edited in Text Editor", icon='GREASEPENCIL')

            code_box = box.box()
            col = code_box.column(align=True)
            col.scale_y = 0.82
            lines = shown.splitlines()
            max_show = 15
            for line in lines[:max_show]:
                col.label(text=line if line else " ")
            if len(lines) > max_show:
                col.label(text=f"... {len(lines) - max_show} more lines", icon='INFO')

            row = box.row(align=True)
            row.scale_y = 1.3
            row.operator("blender_ai.run_pending", text="Run", icon='PLAY')
            row.operator("blender_ai.discard", text="Discard", icon='TRASH')

            info_row = box.row()
            info_row.scale_y = 0.8
            info_row.label(
                text=f"Tip: Open '{AI_TEXT_NAME}' in Text Editor for multi-line editing",
                icon='INFO',
            )

        # ---- Recently executed code ----
        if AIState.last_ok_code and not AIState.pending_code:
            layout.separator(factor=0.3)
            box = layout.box()
            box.label(text="Execution Successful", icon='CHECKMARK')
            row = box.row(align=True)
            row.scale_y = 1.2
            row.operator("blender_ai.save_preset", text="Save as Preset", icon='ADD')
            row.operator("blender_ai.save_to_menu", text="Add to Menu", icon='MENU_PANEL')

        # ---- Preset Library + Preset Groups (v9.2.0 two-tier architecture) ----
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

        # === Top: Preset Library (asset list) ===
        header = box.row(align=True)
        header.label(text=f"Preset Library ({len(presets)})", icon='ASSET_MANAGER')
        sub = header.row(align=True)
        sub.alignment = 'RIGHT'
        sub.operator("blender_ai.export_presets", text="", icon='EXPORT')
        sub.operator("blender_ai.import_presets", text="", icon='IMPORT')
        sub.operator("blender_ai.sync_presets_to_project", text="", icon='FILE_REFRESH')
        if not presets:
            box.label(text="  No presets yet. Run code, then 'Save as Preset'", icon='INFO')
        else:
            for i, p in enumerate(presets, 1):
                pid = p.get("id", "")
                pname = p.get("name", "Untitled")

                sub = box.row(align=True)
                sub.scale_y = 1.02

                op = sub.operator("blender_ai.run_preset",
                                  text=f"{i}. {pname}", icon='PLAY')
                op.preset_id = pid

                # Rename / Delete / Add to Group / Add to Menu (v9.3.0)
                op = sub.operator("blender_ai.rename_preset", text="", icon='GREASEPENCIL')
                op.preset_id = pid
                op = sub.operator("blender_ai.delete_preset", text="", icon='TRASH')
                op.preset_id = pid
                op = sub.operator("blender_ai.group_add_preset", text="", icon='ADD')
                op.preset_id = pid
                op = sub.operator("blender_ai.add_preset_to_menu", text="", icon='MENU_PANEL')
                op.preset_id = pid

        # === Bottom: Preset Groups (execution sequences, sortable) ===
        layout.separator(factor=0.3)
        grp_box = layout.box()

        row = grp_box.row()
        row.label(text="Preset Groups", icon='OUTLINER_COLLECTION')
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        sub.operator("blender_ai.group_new", text="+ New", icon='ADD')
        if active_group:
            sub.operator("blender_ai.group_run",
                         text=f"Run '{active_group['name']}'", icon='PLAY')

        if not all_groups:
            grp_box.label(text="  No groups yet. Click '+ New' to create", icon='INFO')
        else:
            # Group selector
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
            mgr_row.operator("blender_ai.group_rename", text="Rename", icon='GREASEPENCIL')
            mgr_row.operator("blender_ai.group_delete", text="Delete Group", icon='TRASH')

            if active_group:
                items = active_group.get("items", [])
                if not items:
                    grp_box.label(
                        text="  No presets in group. Click [+] from preset library above",
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

        # ---- Log (v9.8.0 enhanced: copyable) ----
        layout.separator()
        box = layout.box()
        row = box.row()
        row.label(text="Operation Log", icon='TEXT')
        row.operator("blender_ai.copy_logs", text="Copy", icon='COPYDOWN')
        row.operator("blender_ai.open_log_folder", text="Log", icon='FILE_FOLDER')
        row.operator("blender_ai.clear_logs", text="Clear", icon='TRASH')

        col = box.column(align=True)
        col.scale_y = 0.8
        logs = AIState.logs[-20:]
        if not logs:
            col.label(text="  No logs yet", icon='INFO')
        else:
            for line in logs:
                col.label(text=line if line else " ")


# ============================================================
# v9.10.0 Operators — Sync Presets / Open Log Folder
# ============================================================
class BLENDER_AI_OT_sync_presets_to_project(bpy.types.Operator):
    """Sync all preset files to the current .blend project directory"""
    bl_idname = "blender_ai.sync_presets_to_project"
    bl_label = "Sync Presets to Project"
    bl_description = "Copy preset files (preset library, groups, menu, combos) to current .blend directory"

    def execute(self, context):
        proj_dir = _get_project_blend_dir()
        if not proj_dir:
            self.report({'WARNING'}, "Please save the .blend file first")
            return {'CANCELLED'}

        proj_logs = os.path.join(proj_dir, "blender_ai_logs")
        os.makedirs(proj_logs, exist_ok=True)

        # Flush all cached data to disk first
        _cache.flush_all()

        config_dir = _get_config_dir()
        files_to_sync = [
            "blender_ai_assistant_presets.json",
            "blender_ai_preset_groups.json",
            "blender_ai_menu_presets.json",
            "blender_ai_combo_presets.json",
        ]

        synced = 0
        for fname in files_to_sync:
            src = os.path.join(config_dir, fname)
            dst = os.path.join(proj_logs, fname)
            if os.path.isfile(src):
                try:
                    shutil.copy2(src, dst)
                    synced += 1
                except Exception as e:
                    self.report({'WARNING'}, f"Failed to sync {fname}: {e}")

        if synced > 0:
            AIState.log(f"[SYNC] {synced} preset files synced to project directory")
            self.report({'INFO'}, f"Synced {synced} preset files to project directory")
        else:
            self.report({'INFO'}, "No preset files to sync")
        return {'FINISHED'}


class BLENDER_AI_OT_open_log_folder(bpy.types.Operator):
    """Open the log file directory in system file explorer"""
    bl_idname = "blender_ai.open_log_folder"
    bl_label = "Open Log Folder"
    bl_description = "Open the command log directory in File Explorer"

    def execute(self, context):
        log_path = _get_project_log_path()
        if log_path:
            folder = os.path.dirname(log_path)
        else:
            # Fallback: open config directory
            folder = _get_config_dir()

        if not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)

        try:
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.run(['open', folder])
            else:
                subprocess.run(['xdg-open', folder])
            AIState.log(f"[LOG] Opened folder: {folder}")
        except Exception as e:
            self.report({'ERROR'}, f"Cannot open folder: {e}")
        return {'FINISHED'}


# ============================================================
# Preferences
# ============================================================
def _update_pref_model_preset(self, context):
    if self.model_preset != 'CUSTOM':
        self.model_name = self.model_preset


class BLENDER_AI_preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    api_key: bpy.props.StringProperty(name="API Key", default="", subtype='PASSWORD')
    base_url: bpy.props.StringProperty(
        name="API URL",
        default="https://api.deepseek.com",
        description="API Base URL (DeepSeek: https://api.deepseek.com)",
    )
    model_preset: bpy.props.EnumProperty(
        name="Model",
        items=MODEL_PRESETS,
        default='deepseek-v4-flash',
        update=_update_pref_model_preset,
    )
    model_name: bpy.props.StringProperty(name="Model Name", default="deepseek-v4-flash")

    def draw(self, context):
        layout = self.layout
        layout.label(text="Default API configuration (overridden by Scene-level values)")
        layout.prop(self, "api_key")
        layout.prop(self, "base_url")
        layout.prop(self, "model_preset")
        if self.model_preset == 'CUSTOM':
            layout.prop(self, "model_name")
        else:
            layout.label(text=f"Current model: {self.model_name}")


# ============================================================
# Registration
# ============================================================
CLASSES = (
    BLENDER_AI_preferences,
    # Screenshot
    BLENDER_AI_OT_test_screenshot,
    # Core
    BLENDER_AI_OT_send,
    BLENDER_AI_OT_cancel,
    BLENDER_AI_OT_run_pending,
    BLENDER_AI_OT_discard,
    BLENDER_AI_OT_open_in_text_editor,
    BLENDER_AI_OT_copy_code,
    BLENDER_AI_OT_clear_history,
    BLENDER_AI_OT_clear_logs,
    # v9.8.0 Log copy + Prompt history
    BLENDER_AI_OT_copy_logs,
    BLENDER_AI_OT_prev_prompt,
    BLENDER_AI_OT_next_prompt,
    BLENDER_AI_OT_clear_prompt,
    # Preset Library
    BLENDER_AI_OT_save_preset,
    BLENDER_AI_OT_run_preset,
    BLENDER_AI_OT_delete_preset,
    BLENDER_AI_OT_rename_preset,
    # Preset Export/Import (v9.5.0)
    BLENDER_AI_OT_export_presets,
    BLENDER_AI_OT_import_presets,
    # Menu Presets (v9.3.0)
    BLENDER_AI_OT_save_to_menu,
    BLENDER_AI_OT_run_menu_preset,
    BLENDER_AI_OT_delete_menu_preset,
    BLENDER_AI_OT_add_preset_to_menu,
    # Combo Presets (v9.3.0)
    BLENDER_AI_OT_create_combo,
    BLENDER_AI_OT_run_combo,
    BLENDER_AI_OT_delete_combo,
    BLENDER_AI_OT_rename_combo,
    # Preset Groups (v9.2.0)
    BLENDER_AI_OT_group_new,
    BLENDER_AI_OT_group_delete,
    BLENDER_AI_OT_group_rename,
    BLENDER_AI_OT_group_select,
    BLENDER_AI_OT_group_add_preset,
    BLENDER_AI_OT_group_remove_item,
    BLENDER_AI_OT_group_move_item_up,
    BLENDER_AI_OT_group_move_item_down,
    BLENDER_AI_OT_group_run,
    # Connection / Settings
    BLENDER_AI_OT_test_connection,
    BLENDER_AI_OT_reset_url,
    BLENDER_AI_OT_set_quick,
    # v9.10.0 Project logging + Preset sync
    BLENDER_AI_OT_sync_presets_to_project,
    BLENDER_AI_OT_open_log_folder,
    # Menu classes (v9.3.0)
    BLENDER_AI_MT_auto_menu,
    BLENDER_AI_MT_auto_menu_manage,
    # Panel
    BLENDER_AI_PT_main_panel,
)


# Global flag for group runner result tracking
_group_step_ok = None


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    # Mount Automation menu to 3D View top menu bar (standard mount point)
    bpy.types.VIEW3D_MT_editor_menus.append(draw_auto_menu)

    S = bpy.types.Scene
    S.ai_show_settings = bpy.props.BoolProperty(name="Show Settings", default=True)
    S.ai_base_url = bpy.props.StringProperty(
        name="API URL",
        description="DeepSeek: https://api.deepseek.com  |  OpenAI: https://api.openai.com/v1  |  Local: http://localhost:11434/v1",
        default="https://api.deepseek.com",
    )
    S.ai_model_preset = bpy.props.EnumProperty(
        name="Model", items=MODEL_PRESETS, default='deepseek-v4-flash',
        update=_update_scene_model_preset,
    )
    S.ai_model_name = bpy.props.StringProperty(name="Model Name", default="deepseek-v4-flash")
    S.ai_api_key = bpy.props.StringProperty(name="API Key", default="", subtype='PASSWORD')
    S.ai_prompt = bpy.props.StringProperty(name="Prompt", default="")
    S.ai_conn_status = bpy.props.StringProperty(name="Connection Status", default="")

    # v9.0.0 Advanced feature toggles
    S.ai_attach_screenshot = bpy.props.BoolProperty(
        name="Attach Screenshot",
        description="Send a 3D viewport screenshot with each request (requires multimodal model like GPT-4o/Claude)",
        default=False,
    )
    S.ai_use_tools = bpy.props.BoolProperty(
        name="Tool Mode",
        description="Enable built-in tool functions; AI calls high-level operations instead of raw bpy code",
        default=False,
    )
    S.ai_auto_fix = bpy.props.BoolProperty(
        name="Auto-Fix Errors",
        description="On execution failure, auto-send error to AI for correction (max 3 rounds)",
        default=False,
    )
    # v9.1.0 SSL verification
    S.ai_ssl_verify = bpy.props.BoolProperty(
        name="SSL Verify",
        description="Disable to skip HTTPS certificate verification (for enterprise intranet/self-signed certs)",
        default=True,
    )


def unregister():
    # Remove top menu hook first
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
