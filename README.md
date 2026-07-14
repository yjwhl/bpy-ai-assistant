# Blender AI Assistant

> 在 Blender 里用自然语言写代码 —— AI 生成 bpy 代码，一键预览/编辑/执行，配备预设系统、多轮对话、项目日志记录等完整工作流。

[![Blender 4.2+](https://img.shields.io/badge/Blender-4.2%2B-blue)](https://www.blender.org/)
[![Blender 5.0](https://img.shields.io/badge/Blender-5.0-9cf)](https://www.blender.org/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-green)](https://www.gnu.org/licenses/gpl-3.0)
[![Version](https://img.shields.io/badge/Version-9.10.0-orange)](./CHANGELOG.md)

## 这是什么

Blender AI Assistant 是一个代码生成型的 Blender AI 插件。和 MCP 工具调用型方案不同，它让 AI 直接生成可执行的 bpy Python 代码，你在预览区看到完整代码后再决定是否运行 —— **你始终拥有最终控制权**。

核心理念：**AI 写代码，人类做决策。**

## 核心特性

### AI 代码生成
- 自然语言 → bpy 代码 → 预览 → 一键执行
- 支持 DeepSeek / OpenAI / Claude / 本地模型（兼容 OpenAI API 格式即可）
- 代码预览，可一键在 Text Editor 中编辑后执行
- 多轮对话，AI 能引用上下文中的物体和操作

### 预设系统（四层架构）
1. **预设库** — 每次 AI 生成的成功代码都可存为预设，积累个人操作资产
2. **预设组** — 将多个预设组合成执行序列，一键顺序执行
3. **菜单预设** — 添加到 3D View 顶部「自动化」菜单，随时调用
4. **组合预设** — 将多个菜单预设组合，按编号顺序执行

### 预设跟随项目
- 保存 .blend 文件后，预设自动关联到项目目录
- 换项目、换电脑，预设跟着 .blend 走，不会丢失
- 一键同步按钮，随时手动迁移预设数据

### 项目日志记录
- 若 .blend 已保存，每次对话自动在项目目录生成文本日志
- 日志包含：时间戳、输入指令、生成代码、执行状态
- 被动积累个人 bpy 代码资产，随时翻查复用

### Function Calling 工具层
- 14 个预置工具函数（创建几何体、修改属性、材质、修改器等）
- AI 优先调用工具而非生成裸代码，复杂逻辑才降级为代码生成

### 错误自动修复
- 代码执行失败时自动将错误信息发回 AI 修正，最多 3 轮自愈

### 其他
- 视口截图 + 多模态（需多模态模型）
- 预设导出/导入（JSON 格式，跨项目迁移）
- 高性能缓存架构（CacheManager + 原子写入，界面不卡顿）
- 指令历史（↑↓ 回填，最多 50 条）
- 日志一键复制到剪贴板
- SSL 验证开关

## 兼容性

| Blender 版本 | 安装方式 | 支持状态 |
|-------------|---------|---------|
| 4.2 ~ 4.4 | Extensions → Install from Disk | 完全支持 |
| 5.0 ~ 5.1 | Extensions → Install from Disk | 完全支持 |
| 4.0 ~ 4.1 | Preferences → Add-ons → Install (Legacy) | 支持 |

**已验证的 API 兼容**：不使用 `bgl`、不使用 dict 式 RNA 访问、`temp_override()` 符合 4.0+ 标准。

## 安装

### 方式 1：Extensions（推荐，4.2+）

1. 从 [Releases](https://github.com/匹宙Plumb/blender-ai-assistant/releases) 下载 `blender_ai_assistant-x.x.x.zip`
2. Blender → Edit → Preferences → Get Extensions → 右上角 ▼ → Install from Disk
3. 选择下载的 `.zip` 文件 → 勾选启用
4. 3D View 侧栏按 `N` 键 → 找到「AI Assistant」面板

### 方式 2：Legacy Add-on（4.0 ~ 4.1）

1. 下载仓库，将 `blender_ai_assistant/` 文件夹放入 Blender addons 目录：
   - Windows: `%APPDATA%\Blender Foundation\Blender\4.x\scripts\addons\`
   - macOS: `~/Library/Application Support/Blender/4.x/scripts/addons/`
   - Linux: `~/.config/blender/4.x/scripts/addons/`
2. Blender → Edit → Preferences → Add-ons → 搜索 "AI Assistant" → 启用

## 配置

在侧栏 AI Assistant 面板的「设置」区域填写：

| 字段 | 说明 | 示例 |
|------|------|------|
| API URL | API 地址 | `https://api.deepseek.com` |
| API Key | 密钥 | `sk-xxxxxxxxxxxx` |
| 模型 | 下拉选择或自定义 | `deepseek-chat` |

支持的服务商（任何兼容 OpenAI API 格式的服务均可）：
- **DeepSeek** — `https://api.deepseek.com`
- **OpenAI** — `https://api.openai.com/v1`
- **Claude**（需兼容层）
- **本地模型** — `http://localhost:11434/v1`（Ollama 等）

填好后点「测试连接」验证。

## 快速上手

1. 打开 Blender，默认场景有一个 Cube
2. 保存 .blend 到项目目录（重要：日志和预设依赖项目路径）
3. 在 AI Assistant 面板输入：

```
在原点创建一个半径为 1.5 的球体，并给它一个红色材质
```

4. 点「发送」→ AI 生成代码 → 点「运行」→ 球体出现在视口中

更多演示指令见 [docs/demo-commands.md](./docs/demo-commands.md)。

## 面板位置

- **主面板**：3D View → 侧栏（N键）→ AI Assistant
- **顶部菜单**：3D View → 顶部菜单栏 → 自动化

## 与其他方案的区别

| 特性 | 本插件 | Blender MCP | BlenderGPT |
|------|--------|-------------|------------|
| 架构类型 | 代码生成型 | MCP 工具调用型 | 代码生成型 |
| 代码可见性 | 完整预览 → 编辑 → 执行 | 不可见 | 预览 → 执行 |
| 预设系统 | 四层（库/组/菜单/组合） | 无 | 无 |
| 预设跟随项目 | 自动关联 .blend 目录 | 无 | 无 |
| 项目日志 | 指令+代码自动记录 | 无 | 无 |
| 错误自修复 | 3 轮自动修复 | 无 | 无 |
| 多模型支持 | DeepSeek/OpenAI/Claude/本地 | 仅 Claude | 仅 OpenAI |
| 依赖 | 纯 bpy + 标准库 | MCP Server + Node.js | 无 |
| Blender 5.x | 完全兼容 | 待验证 | 已停止维护 |

**核心差异**：预设系统和项目日志是本插件独有的能力。

## 技术架构

```
用户输入
  ↓
AI API (DeepSeek/OpenAI/Claude)
  ↓
代码提取 (正则匹配 ```python ... ```)
  ↓
写入项目日志 (blender_ai_logs/)
  ↓
代码预览 ──→ Text Editor 编辑 (可选)
  ↓
执行 (exec, 4.0+ temp_override)
  ↓
成功 → 日志 [OK]
失败 → [FIX] 自动修复 (最多3轮) → 重试
  ↓
存为预设 → 预设库 → 预设组 → 菜单 → 组合
```

**缓存架构**：所有预设 IO 通过 `CacheManager` 内存缓存 + 原子写入（先临时文件，再 `os.replace`）。

**API 兼容**：零 `bgl` 依赖，全部使用标准 `bpy` + `gpu` API。`temp_override()` 符合 4.0+ 标准。通过 Blender 5.0 全面兼容验证。

## 系统要求

- Blender 4.2+（4.0-4.1 支持 Legacy 模式）
- Python 3.10+（Blender 内置）
- 网络连接
- 任一兼容 OpenAI API 格式的 AI 服务

## 开发者信息

### 扩展结构
```
blender_ai_assistant/
├── __init__.py              # 主插件代码 (~2760行)
├── blender_manifest.toml    # 扩展清单（4.2+ 标准）
├── LICENSE                  # GPL-3.0
├── README.md
├── CHANGELOG.md
└── docs/
    └── demo-commands.md     # 演示指令
```

### 开发命令
```bash
# 构建扩展 .zip（Blender 命令行工具）
blender --command extension build --source-dir ./blender_ai_assistant --output-dir ./dist

# 安装测试
blender --command extension install-file --file ./dist/blender_ai_assistant-9.10.0.zip --enable
```

### 关键类索引

| 类名 | 功能 |
|------|------|
| `_DataCache` / `CacheManager` | 预设缓存 + 原子写入 + 项目路径检测 |
| `AIState` | 全局状态管理（日志/历史/代码/对话） |
| `BLENDER_AI_OT_send` | 核心发送指令 + 日志记录 |
| `BLENDER_AI_OT_run_pending` | 执行 AI 代码 + 日志记录 |
| `BLENDER_AI_PT_main_panel` | 主面板 UI |

## 路线图

- [ ] 视口截图 + 多模态模型深度集成
- [ ] 流式响应（SSE）
- [ ] 几何节点 / 材质节点 AI 辅助
- [ ] 工作流录制与回放
- [ ] 提交 Blender Extensions Platform

## 贡献

欢迎提交 Issue 和 Pull Request。

- Bug 报告请附上操作日志（面板「复制」按钮一键复制）
- PR 请基于最新版本开发

## License

GPL-3.0 — 与 Blender 本身许可证一致。

## 致谢

- [DeepSeek](https://www.deepseek.com/) — 默认 AI 后端
- [Blender](https://www.blender.org/) — 伟大的开源 3D 软件
- 所有在 MCP 生态中探索 Blender + AI 的开发者们
