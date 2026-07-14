# Blender AI Assistant — 更新日志

## v9.8.0 (2026-06-02) — Prompt 复用 + 日志复制

### 新增功能
- **Prompt 不清空**：发送指令后输入框保留原文，可直接修改后重发（不再一去不复返）
- **指令历史**：文件级历史记录（最多 50 条），↑↓ 按钮快速回填之前发送过的指令
  - 存储于 `blender_ai_prompt_history.json`，去重 + 最新在前
  - 新增 Operator: `prev_prompt` / `next_prompt`
- **日志复制**：「复制」按钮一键复制全部操作日志到系统剪贴板
  - 新增 Operator: `copy_logs`
- **手动清空按钮**：输入框右侧 `X` 按钮可手动清空（不再依赖发送自动清空）

### 面板布局调整
```
[___________________________] [↑] [↓]          ← 输入框 + 历史导航
[发送] [X]                                      ← 发送 + 手动清空
...
[操作日志]  [复制] [清空]                        ← 日志控制按钮
```

## v9.7.0 (2026-05-25) — CacheManager Edition

### 架构升级
- 引入 `_DataCache` / `CacheManager` 高性能缓存管理器（来自 vscode14 新框架）
- 所有预设 IO 改为内存缓存 + 原子写入，彻底解决 `draw()` 频繁 IO 导致的界面卡顿
- 原子写入（先临时文件，再 `os.replace`）防止崩溃时文件损坏
- 兼容函数层（`load_presets` / `save_presets` / `_load_menu_presets` 等）保持接口不变，所有 `return True` 确保 `if save_presets():` 判断兼容

### Class 迁移
- 将 demo13.py（v9.5.0）全部 class 迁移至新框架
- 所有直接 IO 调用替换为 CacheManager 兼容函数（移除 `_menu_presets_path()` 等旧接口）
- `execute_code` 保留 v9.2.6 完整版（含 `_find_view3d_context` + `temp_override`）
- `AIState` 使用 demo13 完整版（含 `MAX_LOGS`/`MAX_HISTORY_PAIRS`/`MAX_AUTO_FIX_ROUNDS`/`use_tool_mode`）
- `BLENDER_AI_OT_run_preset` 使用 demo13 完整版（直接执行代码，非仅加载）
- `BLENDER_AI_OT_import_presets.execute()` 末尾增加 `CacheManager.reload_all()` 刷新缓存
- Panel 使用 demo13 完整版（含动画采集/预设库+预设组/日志等全部区块）

### 文件清单
- `Blender AI Assistant vscode15.py` — v9.7.0 主文件（2807行）
- `Blender AI Assistant vscode14（新框架）.py` — v9.6.0 框架骨架（保留参考）
- `Blender AI Assistant demo13.py` — v9.5.0 功能完整版（保留参考）

### 技术债务
- `CacheManager` 目前为模块级全局单例，多文件注册时可能共享同一实例，未来考虑 per-addon 实例隔离
- Panel `_draw()` 方法约 290 行，考虑通过 Helper 拆分

---

## v9.5.0 (demo13) — 预设导出/导入

## v9.4.0 (demo13) — 菜单挂载点修复

## v9.3.0 (demo13) — 顶部自动化菜单

## v9.2.6 (demo11) — context 修复

## v9.2.5 (demo11) — 帧号规则

## v9.2.0 (demo11) — 预设组系统

## v9.0.0 (demo11) — 多模态/工具调用
