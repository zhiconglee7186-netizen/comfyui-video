omfyUI GUI Script Guide (comfyui_gui.py)
Overview
comfyui_gui.py is a Tkinter desktop app that sends ComfyUI workflows to a remote ComfyUI server and downloads outputs.

It supports:

T2V (text-to-video)
I2V (image-to-video)
Director JSON multi-shot mode (per-shot render mode, subtitle burn-in, concat, optional BGM mix)
Runtime Requirements
Python 3.9+ (recommended)
Python packages:
requests
tkinter (usually included with Python on Windows)
ffmpeg available in PATH
ffprobe available in PATH (used for audio stream checks; strongly recommended)
A reachable ComfyUI server API endpoint (the script currently uses a fixed server IP in code)
Required Files
Place these files where the script can find them (same directory as comfyui_gui.py, current working directory, or packaged resource path).

1) Main script
comfyui_gui.py
2) Workflow JSON files (required for video generation)
At least one valid file for each mode:

T2V workflow (one of):

T2VNEWAPI (1).json
T2VnewAPI.json
T2VNEWAPI.json
I2V workflow (one of):

I2VNEWAPI (1).json
I2VnewAPI.json
I2VNEWAPI.json
3) Inputs for Director JSON mode
Director file: any valid *.json with a shots array
Product image file: required by current validation in Director mode (for I2V shots and general mode checks)
Optional but Recommended Files
BGM workflow (optional)
If present, the script generates and mixes background music. If missing, it falls back to video-only final output.

Accepted names/paths include:

BGMAPI (1).json
BGMAPI(1).json
bgmAPI.json
Quick Start
Start ComfyUI server and confirm API is reachable.
Ensure ffmpeg/ffprobe are in PATH.
Put workflow JSON files next to comfyui_gui.py (or in a resource path used by your packaged build).
Run:
python comfyui_gui.py
In the GUI:
Choose mode (Batch/Parameter or Director JSON shots)
Select required input files
Click generate
Output
Intermediate and final files are written under ./outputs
Director mode creates per-shot outputs and a final merged video
Notes
Per-shot render_mode is respected from your Director JSON (t2v / i2v).
If workflow node IDs/classes differ from your exported ComfyUI JSON, adjust the workflow JSON or mapping logic in code.
