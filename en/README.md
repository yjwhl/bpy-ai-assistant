# Bpy AI Assistant (English)

A Blender add-on that generates `bpy` Python scripts from natural language prompts, powered by AI (DeepSeek, OpenAI, Claude, or local models via Ollama).

**English version** — for submission to Blender Extensions Platform.

## Features

- **Natural language → bpy code**: Type what you want in plain English, get executable Blender Python code
- **Multi-turn conversation**: Build complex scripts iteratively with context awareness
- **Preset library**: Save generated code as reusable presets, organize into groups
- **Preset groups**: Create ordered execution sequences, run multiple presets at once
- **Menu presets**: Pin presets to the 3D View top menu bar for quick access
- **Combo presets**: Bundle multiple presets into a single runnable combo
- **Project logging**: Auto-log all commands and generated code to .blend project directory
- **Prompt history**: Quick-recall previous prompts with UP/DOWN buttons
- **Multi-model support**: DeepSeek (V3/V4/R1), OpenAI (GPT-4o), Claude, local models
- **Auto-fix**: On error, auto-send error details back to AI for correction (up to 3 rounds)
- **Import/Export**: Share presets between projects or with other users

## Requirements

- Blender 4.2.0 or newer
- An API key for any OpenAI-compatible API (DeepSeek, OpenAI, etc.)

## Installation

### From Blender Extensions Platform (recommended)
1. In Blender: Edit > Preferences > Get Extensions
2. Search for "Bpy AI Assistant"
3. Click Install

### Manual install
1. Download `blender_ai_assistant-9.10.0-en.zip`
2. In Blender: Edit > Preferences > Add-ons > Install from Disk
3. Select the zip file and enable the add-on

## Setup

1. Open the AI Assistant panel: 3D View > Sidebar (N) > AI Assistant
2. Expand **Settings** section
3. Enter your API Key
4. Click **Test Connection** to verify
5. Choose a model preset or enter a custom model name

## Quick Start

1. Type a prompt like: `create 10 cubes in a circle`
2. Click **Send**
3. Review the generated code in the preview
4. Click **Run** to execute
5. Click **Save as Preset** to keep it for later

## Contributing

Issues and Pull Requests are welcome.

- [Report a bug or request a feature](https://github.com/yjwhl/bpy-ai-assistant/issues)

## License

GPL-3.0-or-later

## Links

- GitHub: [https://github.com/yjwhl/bpy-ai-assistant](https://github.com/yjwhl/bpy-ai-assistant)

## Author

匹宙Plumb
