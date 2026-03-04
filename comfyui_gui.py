"""
ComfyUI LTX-2 工作流 GUI 应用
支持 T2V (Text-to-Video) 和 I2V (Image-to-Video) 工作流
"""

import tkinter as tk
from tkinter import scrolledtext, messagebox, filedialog, ttk
import requests
import json
import time
import os
import sys
import uuid
import threading
import shutil
import tempfile
import subprocess
import re
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Any, Tuple

# 获取资源路径（用于打包后的exe）
def resource_path(relative_path):
    """获取资源文件的绝对路径，支持PyInstaller打包"""
    try:
        # PyInstaller创建临时文件夹，将路径存储在_MEIPASS中
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def parse_time_range_to_seconds(time_range) -> Tuple[float, float]:
    """解析时间范围，返回 (start_sec, end_sec)，支持浮点数
    
    支持两种格式：
    1. 字符串格式: "0-2s", "2-5s", "5-8", "8-10s", "0-1.5s" 等
    2. 字典格式: {"start": 0, "end": 5} 或 {"start": 0.0, "end": 5.0}
    """
    # 如果是字典格式
    if isinstance(time_range, dict):
        start = time_range.get('start')
        end = time_range.get('end')
        if start is None or end is None:
            raise ValueError(f"字典格式的 time_range 必须包含 'start' 和 'end' 字段: {time_range}")
        try:
            start = float(start)
            end = float(end)
            if start < 0 or end <= start:
                raise ValueError(f"无效的时间范围: start={start}, end={end}")
            return (start, end)
        except (ValueError, TypeError) as e:
            raise ValueError(f"无法解析时间范围字典 '{time_range}': {e}")
    
    # 如果是字符串格式
    if not isinstance(time_range, str):
        raise ValueError(f"time_range 必须是字符串或字典，当前类型: {type(time_range).__name__}")
    
    # 支持 "0-2s", "2-5s", "5-8", "8-10s", "0-1.5s" 等格式
    time_range = time_range.strip().rstrip('s').strip()
    parts = time_range.split('-')
    if len(parts) != 2:
        raise ValueError(f"无效的时间范围格式: {time_range}")
    try:
        start = float(parts[0].strip())
        end = float(parts[1].strip())
        if start < 0 or end <= start:
            raise ValueError(f"无效的时间范围: start={start}, end={end}")
        return (start, end)
    except ValueError as e:
        raise ValueError(f"无法解析时间范围 '{time_range}': {e}")


def validate_timeline(shots: List[Dict], video_meta: Dict, default_fps: float, log_callback=None) -> bool:
    """校验时间轴一致性，返回是否通过"""
    if not shots:
        return True
    
    # 计算总帧数
    total_frames = 0
    for shot in shots:
        time_range = shot.get('time_range', '')
        fps = shot.get('fps') or shot.get('frame_rate') or default_fps
        
        if time_range:
            try:
                start_sec, end_sec = parse_time_range_to_seconds(time_range)
                duration_sec = end_sec - start_sec
                length = max(1, int(round(duration_sec * fps)))
                total_frames += length
            except (ValueError, Exception) as e:
                if log_callback:
                    log_callback(f"[Timeline][Warn] Shot {shot.get('shot_id', '?')} time_range 解析失败: {e}")
        else:
            length = shot.get('length') or shot.get('frames') or 0
            total_frames += length
    
    # 获取预期总时长
    expected_duration = None
    if isinstance(video_meta, dict):
        expected_duration = video_meta.get('duration')
    
    if expected_duration is None:
        # 使用最后一个 shot 的 end_sec
        last_shot = shots[-1]
        time_range = last_shot.get('time_range', '')
        if time_range:
            try:
                _, end_sec = parse_time_range_to_seconds(time_range)
                expected_duration = end_sec
            except (ValueError, Exception):
                pass
    
    if expected_duration is not None:
        expected_frames = int(round(expected_duration * default_fps))
        actual_frames = total_frames
        diff_frames = abs(actual_frames - expected_frames)
        
        if diff_frames > 1:
            if log_callback:
                log_callback(f"[Timeline][Warn] 时间轴不一致: 预期 {expected_frames} 帧 ({expected_duration:.2f}秒), 实际 {actual_frames} 帧, 差异 {diff_frames} 帧")
            return False
    
    return True


def scan_workflow_params(workflow: dict) -> List[Dict]:
    """扫描工作流中的关键参数（strength/cfg/steps/seed等）
    
    Args:
        workflow: ComfyUI 工作流字典
        
    Returns:
        List[Dict]: 包含节点ID、类型和关键参数的列表
    """
    results = []
    key_params = ["strength", "cfg", "steps", "seed", "noise_seed", "denoise", "guidance", 
                  "sampler_name", "scheduler", "frame_rate", "fps", "length", "frames", 
                  "seconds", "batch_size", "control_after_generate", "max_shift", "base_shift"]
    
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        
        class_type = node.get("class_type", "")
        inputs = node.get("inputs", {})
        
        # 只收集存在的关键参数
        picked_inputs = {k: v for k, v in inputs.items() if k in key_params}
        
        if picked_inputs:
            results.append({
                "node_id": node_id,
                "class_type": class_type,
                "params": picked_inputs
            })
    
    return results


def format_workflow_param_snapshot(results: List[Dict]) -> str:
    """格式化工作流参数快照为可读字符串
    
    Args:
        results: scan_workflow_params 返回的结果列表
        
    Returns:
        str: 格式化的多行字符串
    """
    lines = []
    for record in results:
        node_id = record.get("node_id", "?")
        class_type = record.get("class_type", "?")
        params = record.get("params", {})
        
        # 格式化参数为 key=value 形式
        param_str = " ".join([f"{k}={v}" for k, v in params.items()])
        lines.append(f"[WF][Param] node={node_id} class={class_type} {param_str}")
    
    return "\n".join(lines)


def apply_i2v_anti_deform_lock(workflow: dict, shot_id: str, render_mode: str, log_callback=None) -> dict:
    """对 I2V shot3/shot4 应用防变形锁定（参数覆盖 + negative prompt 注入）
    
    Args:
        workflow: ComfyUI 工作流字典
        shot_id: Shot ID（字符串或数字）
        render_mode: 渲染模式（"i2v" 或 "t2v"）
        log_callback: 日志回调函数
        
    Returns:
        dict: 修改后的工作流
    """
    # 检查是否为 I2V 且 shot_id 为 3 或 4
    shot_id_str = str(shot_id).strip()
    is_i2v_shot34 = (render_mode.lower() == "i2v") and (shot_id_str in ("3", "4"))
    
    if not is_i2v_shot34:
        return workflow
    
    # 确定 strength 值
    if shot_id_str == "3":
        target_strength = 0.9
    elif shot_id_str == "4":
        target_strength = 1.0
    else:
        target_strength = None
    
    # 记录最终参数值（用于日志）
    final_params = {
        "strength": None,
        "cfg": None,
        "max_shift": None,
        "base_shift": None
    }
    
    # A) I2V 参数覆盖
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        
        class_type = node.get("class_type", "")
        inputs = node.get("inputs", {})
        
        # 1) LTXVImgToVideoInplace: 设置 strength
        if class_type == "LTXVImgToVideoInplace" and "strength" in inputs and target_strength is not None:
            old_strength = inputs.get("strength")
            inputs["strength"] = target_strength
            final_params["strength"] = target_strength
            if log_callback:
                log_callback(f"  [I2V_LOCK] node {node_id}: strength {old_strength} -> {target_strength}")
        
        # 2) CFGGuider: 限制 cfg <= 3.0
        if class_type == "CFGGuider" and "cfg" in inputs:
            old_cfg = inputs.get("cfg")
            if isinstance(old_cfg, (int, float)) and old_cfg > 3.0:
                inputs["cfg"] = 3.0
                final_params["cfg"] = 3.0
                if log_callback:
                    log_callback(f"  [I2V_LOCK] node {node_id}: cfg {old_cfg} -> 3.0")
            else:
                # 记录当前值（即使没有修改）
                final_params["cfg"] = old_cfg
        
        # 3) LTXVScheduler: 设置 max_shift 和 base_shift
        if class_type == "LTXVScheduler":
            if "max_shift" in inputs:
                old_max_shift = inputs.get("max_shift")
                inputs["max_shift"] = 1.2
                final_params["max_shift"] = 1.2
                if log_callback:
                    log_callback(f"  [I2V_LOCK] node {node_id}: max_shift {old_max_shift} -> 1.2")
            if "base_shift" in inputs:
                old_base_shift = inputs.get("base_shift")
                inputs["base_shift"] = 0.6
                final_params["base_shift"] = 0.6
                if log_callback:
                    log_callback(f"  [I2V_LOCK] node {node_id}: base_shift {old_base_shift} -> 0.6")
    
    # B) negative prompt 注入（节点 5198）
    neg_node_id = "5198"
    neg_append_success = False
    neg_field_name = None
    
    if neg_node_id in workflow:
        node_5198 = workflow[neg_node_id]
        if isinstance(node_5198, dict):
            inputs_5198 = node_5198.get("inputs", {})
            
            # 按优先级查找文本字段
            text_field = None
            for field_name in ["negative_prompt", "text", "prompt"]:
                if field_name in inputs_5198:
                    text_field = field_name
                    neg_field_name = field_name
                    break
            
            if text_field:
                old_negative = inputs_5198.get(text_field, "").strip()
                
                # 检查是否已包含 "wrong screen shape"（避免重复追加）
                if "wrong screen shape" not in old_negative.lower():
                    append_text = "deformed, warped, stretched, melted, mutated, broken proportions, changed silhouette, redesign, extra parts, missing parts, extra buttons, missing buttons, wrong screen shape, wrong bezel, wrong stand structure, color shift, texture change, add-on modules, unrealistic geometry, text, logo, watermark"
                    new_negative = (old_negative + ", " + append_text).strip(" ,")
                    inputs_5198[text_field] = new_negative
                    neg_append_success = True
                    if log_callback:
                        log_callback(f"  [I2V_LOCK] node {neg_node_id} ({text_field}): appended anti-deform negative")
                else:
                    if log_callback:
                        log_callback(f"  [I2V_LOCK] node {neg_node_id} ({text_field}): anti-deform negative already present, skipped")
                    neg_append_success = True  # 已存在也算成功
            else:
                if log_callback:
                    log_callback(f"  [I2V_LOCK][Warn] node {neg_node_id} has no negative_prompt/text/prompt field, cannot inject negative")
        else:
            if log_callback:
                log_callback(f"  [I2V_LOCK][Warn] node {neg_node_id} is not a dict, cannot inject negative")
    else:
        if log_callback:
            log_callback(f"  [I2V_LOCK][Warn] node {neg_node_id} not found in workflow, cannot inject negative")
    
    # C) 日志汇总
    if log_callback:
        strength_str = str(final_params["strength"]) if final_params["strength"] is not None else "N/A"
        cfg_str = str(final_params["cfg"]) if final_params["cfg"] is not None else "N/A"
        max_shift_str = str(final_params["max_shift"]) if final_params["max_shift"] is not None else "N/A"
        base_shift_str = str(final_params["base_shift"]) if final_params["base_shift"] is not None else "N/A"
        neg_append_str = "YES" if neg_append_success else "NO"
        neg_field_str = neg_field_name if neg_field_name else "N/A"
        
        log_callback(f"[I2V_LOCK] shot={shot_id_str} strength={strength_str} cfg={cfg_str} max_shift={max_shift_str} base_shift={base_shift_str} neg_node={neg_node_id} neg_field={neg_field_str} neg_append={neg_append_str}")
    
    return workflow


def create_ass_subtitle(text: str, start_sec: float, end_sec: float,
                        font_size: int = 9, margin_v: int = 60) -> str:
    """为单个shot创建ASS字幕文件内容"""
    # 转义ASS特殊字符
    text_escaped = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    text_escaped = text_escaped.replace("\n", "\\N")
    
    def seconds_to_ass_time(sec: float) -> str:
        """将秒数转换为ASS时间格式 H:MM:SS.cs"""
        hours = int(sec // 3600)
        minutes = int((sec % 3600) // 60)
        seconds = int(sec % 60)
        centiseconds = int((sec % 1) * 100)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"
    
    ass_content = f"""[Script Info]
Title: Generated Subtitle
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{seconds_to_ass_time(start_sec)},{seconds_to_ass_time(end_sec)},Default,,0,0,0,,{text_escaped}
"""
    return ass_content


def burn_subtitle_ffmpeg(input_video: str, ass_file: str, output_video: str, log_callback=None) -> str:
    """使用ffmpeg烧录ASS字幕到视频，返回实际输出路径"""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg 命令！请确保已安装 ffmpeg 并添加到 PATH 环境变量。")
    
    # 绝对路径（用于日志与结果校验）
    input_abs = os.path.abspath(input_video)
    ass_abs = os.path.abspath(ass_file)
    output_abs = os.path.abspath(output_video)

    # 确保 ASS 文件是 UTF-8 编码
    try:
        with open(ass_abs, 'r', encoding='utf-8') as f:
            content = f.read()
        with open(ass_abs, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        if log_callback:
            log_callback(f"[Warn] ASS 文件编码检查失败: {e}")

    # 工作目录：与输入视频同目录，ffmpeg 使用相对路径，规避盘符转义问题
    workdir = os.path.dirname(input_abs)
    input_name = os.path.basename(input_abs)
    ass_name = os.path.basename(ass_abs)
    output_name = os.path.basename(output_abs)

    # 规范化ASS路径（给 filter 使用的相对路径 + Windows filtergraph 转义）
    # 使用 subprocess.list2cmdline 或手动转义
    ass_norm_raw = ass_name.replace("\\", "/")
    ass_norm = ass_norm_raw.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")

    if log_callback:
        log_callback(f"ASS 原始路径: {ass_abs}")
        log_callback(f"ASS filter 路径(转义后): {ass_norm}")

    # 检测输入视频是否有音轨
    audio_streams = ffprobe_audio_streams(input_abs)
    # Treat -1 (unknown/ffprobe missing) as "assume has audio" to avoid stripping audio
    has_audio = (audio_streams > 0) or (audio_streams == -1)
    
    if log_callback:
        log_callback(f"输入视频路径: {input_abs}")
        log_callback(f"Audio streams detected: {audio_streams}, has_audio={has_audio}")

    # 构建ffmpeg命令（使用相对文件名，cwd=workdir）
    cmd = [
        "ffmpeg", "-y",
        "-i", input_name,
        "-vf", f"ass='{ass_norm}'",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
    ]
    
    # 根据音轨存在性添加音频编码参数
    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")  # 无音轨时不编码音频
    
    cmd.append(output_name)
    
    if log_callback:
        log_callback(f"完整 ffmpeg 命令: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir)
    stderr_text = result.stderr or ""
    stdout_text = result.stdout or ""

    # 非0返回码直接认为失败
    if result.returncode != 0:
        error_msg = stderr_text or stdout_text or "未知错误"
        if log_callback:
            log_callback(f"ffmpeg 错误输出:\n{error_msg}")
            log_callback(f"完整命令: {' '.join(cmd)}")
        raise RuntimeError(f"ffmpeg 字幕烧录失败 (返回码 {result.returncode}): {error_msg}")

    # 即便返回码为0，如果stderr中包含明显错误关键字，也视为失败
    error_keywords = ["Could not open", "ass_read_file", "Error", "No such file or directory"]
    if any(k in stderr_text for k in error_keywords):
        if log_callback:
            log_callback(f"ffmpeg stderr 中包含错误信息:\n{stderr_text}")
            log_callback(f"完整命令: {' '.join(cmd)}")
        raise RuntimeError(f"ffmpeg 字幕烧录疑似失败（stderr 含错误信息），请检查：\n{stderr_text}")

    # 结果文件校验：确保生成的输出文件存在且大小足够
    output_final_path = os.path.join(workdir, output_name)
    if not os.path.exists(output_final_path):
        if log_callback:
            log_callback(f"完整命令: {' '.join(cmd)}")
        raise RuntimeError(
            f"ffmpeg 字幕烧录后未找到输出文件: {output_final_path}\n"
            f"请检查 ffmpeg stderr：\n{stderr_text}"
        )
    file_size = os.path.getsize(output_final_path)
    if file_size < 50 * 1024:
        if log_callback:
            log_callback(f"完整命令: {' '.join(cmd)}")
        raise RuntimeError(
            f"ffmpeg 字幕烧录输出文件过小（{file_size} 字节），可能失败，请检查 ffmpeg stderr。\n"
            f"{stderr_text}"
        )

    if log_callback:
        log_callback(f"字幕烧录成功: {output_final_path} (大小: {file_size} 字节)")
    
    return output_final_path


def transcode_to_mp4(input_video: str, output_mp4: str, log_callback=None) -> None:
    """将视频转码为mp4格式（用于concat前的统一格式）"""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg 命令！")
    
    input_norm = os.path.abspath(input_video).replace("\\", "/")
    output_norm = os.path.abspath(output_mp4).replace("\\", "/")
    
    cmd = [
        "ffmpeg", "-y",
        "-i", input_norm,
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        output_norm
    ]
    
    if log_callback:
        log_callback(f"转码为 MP4: {input_norm} -> {output_norm}")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "未知错误"
        if log_callback:
            log_callback(f"ffmpeg 转码错误:\n{error_msg}")
        raise RuntimeError(f"ffmpeg 转码失败: {error_msg}")


def concat_videos_ffmpeg(video_paths: List[str], output_path: str, log_callback=None) -> None:
    """使用ffmpeg concat demuxer拼接视频"""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg 命令！")
    
    # 创建临时concat列表文件
    concat_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
    try:
        for video_path in video_paths:
            abs_path = os.path.abspath(video_path).replace("\\", "/")
            concat_file.write(f"file '{abs_path}'\n")
        concat_file.close()
        
        output_norm = os.path.abspath(output_path).replace("\\", "/")
        concat_norm = os.path.abspath(concat_file.name).replace("\\", "/")
        
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_norm,
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            output_norm
        ]
        
        if log_callback:
            log_callback(f"拼接视频 ({len(video_paths)} 个片段): {output_norm}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "未知错误"
            if log_callback:
                log_callback(f"ffmpeg 拼接错误:\n{error_msg}")
            raise RuntimeError(f"ffmpeg 拼接失败: {error_msg}")
        
        if log_callback:
            log_callback(f"视频拼接成功: {output_norm}")
    finally:
        if os.path.exists(concat_file.name):
            os.unlink(concat_file.name)


def find_video_file(outputs: Dict[str, str], log_callback=None) -> Optional[str]:
    """从下载的输出文件中找到视频文件（优先mp4，若多个mp4选最新修改时间）"""
    video_extensions = ['.mp4', '.webm', '.mov', '.mkv', '.avi']
    gif_extensions = ['.gif']
    
    # 优先查找 mp4 文件（若多个，选最新修改时间）
    mp4_files = []
    for path in outputs.values():
        if os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext == '.mp4':
                mp4_files.append(path)
    
    if mp4_files:
        # 按修改时间排序，返回最新的
        mp4_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        selected = mp4_files[0]
        if log_callback and len(mp4_files) > 1:
            log_callback(f"[Video][Info] 找到 {len(mp4_files)} 个 mp4 文件，选择最新的: {os.path.basename(selected)}")
        return selected
    
    # 其次查找其他视频文件
    for path in outputs.values():
        if os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in video_extensions:
                return path
    
    # 最后查找gif
    for path in outputs.values():
        if os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in gif_extensions:
                return path
    
    return None


def ffprobe_audio_streams(video_path: str) -> int:
    """使用 ffprobe 检测视频文件的音频流数量，返回整数（0表示无音轨）"""
    if not shutil.which("ffprobe"):
        return -1
    
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            audio_count = len([line for line in result.stdout.strip().split('\n') if line.strip()])
            return audio_count
        else:
            return -1
    except Exception as e:
        return -1


def check_audio_streams(video_path: str, log_callback=None) -> int:
    """使用 ffprobe 检测视频文件的音频流数量（带日志）"""
    audio_count = ffprobe_audio_streams(video_path)
    
    if audio_count == -1:
        if log_callback:
            log_callback(f"[Audio][Warn] ffprobe 未找到或检测失败，无法检测音频流")
        return -1
    
    if log_callback:
        log_callback(f"Audio streams: {audio_count}")
        if audio_count == 0:
            log_callback(f"[Audio][Warn] 视频文件没有音频流: {video_path}")
    
    return audio_count


def build_bgm_prompt_from_director(director_data: Dict) -> str:
    """从 Director JSON 生成 BGM prompt（10秒广告用的情绪推进）"""
    shots = director_data.get('shots', [])
    bgm_moods = []
    
    # 按 time_range 顺序收集 bgm_mood
    for shot in shots:
        bgm_mood = shot.get('bgm_mood', '').strip()
        if bgm_mood:
            bgm_moods.append(bgm_mood.lower())
    
    # 如果全部为空，使用默认"极简科技感广告 BGM"模板
    if not bgm_moods:
        # 默认极简科技感广告 BGM
        return "Minimal premium product ad background music, 100 BPM, modern clean mix, soft synth pad, warm pluck melody, muted percussion, gentle sidechain, subtle bass, spacious reverb, crisp highs, smooth dynamics, loopable, uplifting but calm, no vocals, no speech, no lyrics."
    
    # 情绪推进映射：tight/minimal -> calm/grounded -> airy/relief -> open/inviting
    mood_progression = []
    for mood in bgm_moods:
        if 'minimal' in mood or 'tight' in mood or 'subtle' in mood:
            mood_progression.append('tight/minimal')
        elif 'calm' in mood or 'grounded' in mood or 'warm' in mood:
            mood_progression.append('calm/grounded')
        elif 'airy' in mood or 'relief' in mood or 'light' in mood:
            mood_progression.append('airy/relief')
        elif 'open' in mood or 'inviting' in mood or 'uplifting' in mood:
            mood_progression.append('open/inviting')
        else:
            mood_progression.append('modern minimal')
    
    # 确保有完整的情绪推进
    if len(mood_progression) < 4:
        default_progression = ['tight/minimal', 'calm/grounded', 'airy/relief', 'open/inviting']
        mood_progression = mood_progression + default_progression[len(mood_progression):]
        mood_progression = mood_progression[:4]
    
    # 构建 BGM prompt
    prompt_parts = [
        "Modern minimal electronic product ad background music",
        f"Emotional progression: {', '.join(mood_progression[:4])}",
        "100-120 BPM",
        "Clean modern mix",
        "Soft synth pad",
        "Warm pluck melody",
        "Muted percussion",
        "Gentle sidechain",
        "Subtle bass",
        "Spacious reverb",
        "Crisp highs",
        "Smooth dynamics",
        "Loopable",
        "Uplifting but calm",
        "No vocals, no speech, no lyrics"
    ]
    
    return ", ".join(prompt_parts)


def get_video_duration_from_director(director_data: Dict) -> float:
    """获取视频总时长（优先用 video_meta.duration，否则用最后一个 shot 的 end_sec）"""
    # 优先使用 video_meta.duration
    video_meta = director_data.get('video_meta', {})
    if isinstance(video_meta, dict):
        duration = video_meta.get('duration')
        if duration is not None:
            try:
                return float(duration)
            except (ValueError, TypeError):
                pass
    
    # 否则计算最后一个 shot 的 end_sec
    shots = director_data.get('shots', [])
    if shots:
        last_shot = shots[-1]
        time_range = last_shot.get('time_range', '')
        if time_range:
            try:
                start_sec, end_sec = parse_time_range_to_seconds(time_range)
                return end_sec
            except (ValueError, Exception):
                pass
    
    # 默认返回 10 秒
    return 10.0


def find_audio_file(outputs: Dict[str, str]) -> Optional[str]:
    """从下载的输出文件中找到音频文件（wav/mp3/m4a/aac/flac/ogg 任一）"""
    audio_extensions = ['.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg']
    
    for path in outputs.values():
        if os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in audio_extensions:
                return path
    
    return None


def mix_bgm_into_video(video_path: str, bgm_audio_path: str, out_path: str, bgm_volume: float = 0.25, log_callback=None) -> str:
    """将 BGM 混入视频（支持无音轨视频）"""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("未找到 ffmpeg 命令！")
    
    video_abs = os.path.abspath(video_path).replace("\\", "/")
    bgm_abs = os.path.abspath(bgm_audio_path).replace("\\", "/")
    out_abs = os.path.abspath(out_path).replace("\\", "/")
    
    # 检测输入视频是否有音轨
    audio_streams = ffprobe_audio_streams(video_abs)
    
    if log_callback:
        log_callback(f"输入视频路径: {video_abs}")
        log_callback(f"检测到的 audio_streams: {audio_streams}")
    
    # 构建无音轨命令（直接添加 BGM 作为唯一音轨）
    cmd_no_audio = [
        "ffmpeg", "-y",
        "-i", video_abs,
        "-i", bgm_abs,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_abs
    ]
    
    # 构建混音命令（人声 + bgm），显式选择第一个音频流
    cmd_mix = [
        "ffmpeg", "-y",
        "-i", video_abs,
        "-i", bgm_abs,
        "-filter_complex", f"[1:a]volume={bgm_volume}[a1];[0:a:0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_abs
    ]
    
    # 根据 audio_streams 选择策略
    if audio_streams == 0:
        # 视频没有音轨，直接添加 BGM 作为唯一音轨
        if log_callback:
            log_callback(f"[分支] audio_streams == 0，走无音轨模式：直接添加 BGM")
            log_callback(f"完整 ffmpeg 命令: {' '.join(cmd_no_audio)}")
        cmd = cmd_no_audio
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if log_callback and result.stderr:
            log_callback(f"ffmpeg stderr: {result.stderr[:500]}...")
        
    elif audio_streams == -1:
        # ffprobe 缺失/失败，使用方案A（双尝试）：先尝试混音，失败则降级
        if log_callback:
            log_callback(f"[分支] audio_streams == -1 (ffprobe 缺失/失败)，使用双尝试策略")
            log_callback(f"[尝试1] 先尝试混音模式")
            log_callback(f"完整 ffmpeg 命令: {' '.join(cmd_mix)}")
        
        result = subprocess.run(cmd_mix, capture_output=True, text=True, timeout=300)
        stderr_text = result.stderr or ""
        if log_callback and stderr_text:
            log_callback(f"ffmpeg stderr: {stderr_text[:500]}...")
        
        if result.returncode != 0:
            # 检查是否是因为音频流缺失导致的错误
            if "Stream specifier" in stderr_text and ("matches no streams" in stderr_text or "no streams" in stderr_text):
                if log_callback:
                    log_callback(f"[Fallback] 检测到音频流缺失，切换到无音频模式")
                    log_callback(f"[尝试1失败] ffmpeg stderr:\n{stderr_text}")
                    log_callback(f"[尝试2] 降级为无音轨模式：直接添加 BGM")
                    log_callback(f"完整 ffmpeg 命令: {' '.join(cmd_no_audio)}")
                
                result = subprocess.run(cmd_no_audio, capture_output=True, text=True, timeout=300)
                if log_callback and result.stderr:
                    log_callback(f"ffmpeg stderr: {result.stderr[:500]}...")
                if result.returncode == 0:
                    # Fallback 成功
                    if log_callback:
                        log_callback(f"[尝试2成功] 无音轨模式执行成功")
                    cmd = cmd_no_audio
                else:
                    # Fallback 也失败
                    error_msg_fallback = result.stderr or result.stdout or "未知错误"
                    if log_callback:
                        log_callback(f"完整命令: {' '.join(cmd_no_audio)}")
                        log_callback(f"ffmpeg fallback 错误输出:\n{error_msg_fallback}")
                    raise RuntimeError(f"ffmpeg 添加 BGM 失败（fallback）: {error_msg_fallback}")
            else:
                # 其他错误，不适用 fallback
                error_msg = stderr_text or result.stdout or "未知错误"
                if log_callback:
                    log_callback(f"完整命令: {' '.join(cmd_mix)}")
                    log_callback(f"ffmpeg 错误输出:\n{error_msg}")
                raise RuntimeError(f"ffmpeg 混音 BGM 失败: {error_msg}")
        else:
            # 混音成功
            if log_callback:
                log_callback(f"[尝试1成功] 混音模式执行成功")
            cmd = cmd_mix
            
    else:
        # audio_streams > 0，正常混音（也添加 fallback 保护）
        if log_callback:
            log_callback(f"[分支] audio_streams > 0，走混音模式")
            log_callback(f"完整 ffmpeg 命令: {' '.join(cmd_mix)}")
        cmd = cmd_mix
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        stderr_text = result.stderr or ""
        if log_callback and stderr_text:
            log_callback(f"ffmpeg stderr: {stderr_text[:500]}...")
        
        if result.returncode != 0:
            # 检查是否是因为音频流缺失导致的错误，如果是则 fallback
            if "Stream specifier" in stderr_text and ("matches no streams" in stderr_text or "no streams" in stderr_text):
                if log_callback:
                    log_callback(f"[Fallback] 检测到音频流缺失，切换到无音频模式")
                    log_callback(f"完整 ffmpeg 命令: {' '.join(cmd_no_audio)}")
                
                result = subprocess.run(cmd_no_audio, capture_output=True, text=True, timeout=300)
                if log_callback and result.stderr:
                    log_callback(f"ffmpeg stderr: {result.stderr[:500]}...")
                if result.returncode == 0:
                    # Fallback 成功
                    if log_callback:
                        log_callback(f"[Fallback成功] 无音轨模式执行成功")
                    cmd = cmd_no_audio
                else:
                    # Fallback 也失败
                    error_msg_fallback = result.stderr or result.stdout or "未知错误"
                    if log_callback:
                        log_callback(f"完整命令: {' '.join(cmd_no_audio)}")
                        log_callback(f"ffmpeg fallback 错误输出:\n{error_msg_fallback}")
                    raise RuntimeError(f"ffmpeg 添加 BGM 失败（fallback）: {error_msg_fallback}")
    
    # 检查最终执行结果
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "未知错误"
        if log_callback:
            log_callback(f"完整命令: {' '.join(cmd)}")
            log_callback(f"ffmpeg 错误输出:\n{error_msg}")
        raise RuntimeError(f"ffmpeg 处理失败: {error_msg}")
    
    # 验证输出文件
    if not os.path.exists(out_abs):
        if log_callback:
            log_callback(f"完整命令: {' '.join(cmd)}")
        raise RuntimeError(f"ffmpeg 处理后未找到输出文件: {out_abs}")
    
    file_size = os.path.getsize(out_abs)
    if file_size < 50 * 1024:
        if log_callback:
            log_callback(f"完整命令: {' '.join(cmd)}")
        raise RuntimeError(f"ffmpeg 输出文件过小（{file_size} 字节），可能失败")
    
    if log_callback:
        log_callback(f"BGM 处理成功: {out_abs} (大小: {file_size} 字节)")
    
    return out_abs


class ComfyUIAPI:
    """ComfyUI API 客户端"""
    
    def __init__(self, server_address: str = "127.0.0.1:8188"):
        """初始化 ComfyUI API 客户端"""
        if not server_address.startswith("http"):
            server_address = f"http://{server_address}"
        self.server_address = server_address.rstrip('/')
        self.client_id = str(uuid.uuid4())
    
    def queue_prompt(self, workflow: Dict) -> str:
        """提交工作流到队列"""
        # 将工作流转换为API格式：
        # - 如果是 ComfyUI 编辑器导出的完整workflow（包含 nodes/links），需要转换；
        # - 如果已经是 API prompt 结构（如 T2VnewAPI.json / I2VnewAPI.json），直接使用。
        if isinstance(workflow, dict) and 'nodes' in workflow and 'links' in workflow:
            prompt = self.workflow_to_prompt(workflow)
        else:
            prompt = workflow
        
        data = {
            "prompt": prompt,
            "client_id": self.client_id
        }
        response = requests.post(
            f"{self.server_address}/prompt",
            json=data,
            timeout=30
        )
        
        # 如果出错，显示详细错误信息
        if response.status_code != 200:
            error_detail = response.text
            raise Exception(f"HTTP {response.status_code} 错误: {error_detail}")
        
        response.raise_for_status()
        result = response.json()
        return result['prompt_id']
    
    def workflow_to_prompt(self, workflow: Dict) -> Dict:
        """
        将ComfyUI工作流JSON格式转换为API prompt格式
        """
        prompt = {}
        nodes = workflow.get('nodes', [])
        links = workflow.get('links', [])
        
        # 创建链接映射：link_id -> [source_node_id, source_slot]
        link_source_map = {}
        for link in links:
            # link格式: [link_id, source_node_id, source_slot, target_node_id, target_slot, type]
            if len(link) >= 5:
                link_id = link[0]
                source_node_id = str(link[1])
                source_slot = link[2]
                link_source_map[link_id] = [source_node_id, source_slot]
        
        # 转换每个节点
        for node in nodes:
            node_id = str(node['id'])
            node_type = node.get('type', '')

            # 某些工作流中包含仅用于UI注释的 MarkdownNote 节点，
            # 这类节点在部分 ComfyUI 部署中可能不存在，实现上也不会参与推理，直接跳过即可。
            if node_type == 'MarkdownNote':
                continue
            widgets_values = node.get('widgets_values', [])
            inputs_def = node.get('inputs', [])
            
            # 构建节点的inputs
            inputs = {}
            
            # 处理每个输入定义
            widget_index = 0
            for input_def in inputs_def:
                input_name = input_def.get('name')
                link_id = input_def.get('link')
                
                if link_id is not None:
                    # 如果有链接，使用连接
                    if link_id in link_source_map:
                        inputs[input_name] = link_source_map[link_id]
                elif 'widget' in input_def:
                    # 如果是widget输入，使用widgets_values
                    if widget_index < len(widgets_values):
                        widget_value = widgets_values[widget_index]
                        inputs[input_name] = widget_value
                        widget_index += 1
            
            # 处理剩余的widgets_values（没有在inputs中定义的widget值）
            # 某些节点的widget值可能不在inputs中定义，需要根据节点类型处理
            remaining_widgets = widgets_values[widget_index:] if widget_index < len(widgets_values) else []
            
            # 对于常见节点类型的特殊处理
            if remaining_widgets:
                if node_type == 'PrimitiveStringMultiline' and len(remaining_widgets) > 0:
                    inputs['text'] = remaining_widgets[0]
                elif node_type == 'PrimitiveInt' and len(remaining_widgets) > 0:
                    inputs['value'] = remaining_widgets[0]
                elif node_type == 'PrimitiveFloat' and len(remaining_widgets) > 0:
                    inputs['value'] = remaining_widgets[0]
                elif node_type == 'EmptyImage' and len(remaining_widgets) >= 4:
                    inputs['width'] = remaining_widgets[0]
                    inputs['height'] = remaining_widgets[1]
                    inputs['batch_size'] = remaining_widgets[2]
                elif node_type == 'LoadImage' and len(remaining_widgets) > 0:
                    inputs['image'] = remaining_widgets[0]
                elif node_type == 'CreateVideo' and len(remaining_widgets) > 0:
                    # CreateVideo的fps在inputs中，但可能需要在widgets_values中
                    if 'fps' not in inputs and len(remaining_widgets) > 0:
                        inputs['fps'] = remaining_widgets[0]
            
            # 构建prompt节点
            prompt[node_id] = {
                "inputs": inputs,
                "class_type": node_type
            }
        
        return prompt
    
    def get_history(self, prompt_id: str, timeout: int = 120) -> Optional[Dict]:
        """获取工作流执行历史
        
        Args:
            prompt_id: 提示词ID
            timeout: 请求超时时间（秒），默认120秒，视频生成可能需要较长时间
        """
        # 增加超时时间，视频生成可能需要很长时间
        response = requests.get(f"{self.server_address}/history/{prompt_id}", timeout=timeout)
        response.raise_for_status()
        history = response.json()
        return history.get(prompt_id)
    
    def wait_for_completion(self, prompt_id: str, check_interval: float = 1.0,
                          log_callback=None, cancel_callback=None) -> Dict:
        """等待工作流执行完成
        
        Args:
            prompt_id: 提示词ID
            check_interval: 检查间隔（秒）
            log_callback: 日志回调函数
            cancel_callback: 取消回调函数
        """
        if log_callback:
            log_callback(f"等待工作流执行完成 (Prompt ID: {prompt_id})...")
        
        max_wait_time = 3600  # 最大等待1小时
        start_time = time.time()
        consecutive_timeouts = 0  # 连续超时次数
        max_consecutive_timeouts = 5  # 最大连续超时次数
        
        while True:
            if cancel_callback and cancel_callback():
                raise Exception("工作流已被用户取消")

            try:
                history = self.get_history(prompt_id, timeout=120)
                consecutive_timeouts = 0  # 重置超时计数
                if history:
                    # ComfyUI API 返回格式: {prompt_id: {status: {...}, outputs: {...}}}
                    # 或者直接返回包含outputs的字典表示已完成
                    if 'outputs' in history:
                        if log_callback:
                            log_callback("\n工作流执行完成!")
                        return history
                    
                    # 检查错误状态
                    status = history.get('status', {})
                    if status:
                        if status.get('completed', False):
                            if log_callback:
                                log_callback("\n工作流执行完成!")
                            return history
                        elif status.get('error', False) or status.get('status_str') == 'error':
                            error_msg = status.get('error_message', status.get('message', '未知错误'))
                            raise Exception(f"工作流执行失败: {error_msg}")
            except requests.exceptions.ReadTimeout as e:
                consecutive_timeouts += 1
                if log_callback:
                    log_callback(f"\n[警告] 请求超时 (连续 {consecutive_timeouts}/{max_consecutive_timeouts} 次)，继续重试...")
                if consecutive_timeouts >= max_consecutive_timeouts:
                    raise Exception(f"连续 {max_consecutive_timeouts} 次请求超时，可能服务器响应过慢或网络不稳定")
            except requests.exceptions.RequestException as e:
                if log_callback:
                    log_callback(f"\n[警告] 网络请求错误: {str(e)}，继续重试...")
                consecutive_timeouts += 1
                if consecutive_timeouts >= max_consecutive_timeouts:
                    raise Exception(f"连续 {max_consecutive_timeouts} 次网络请求失败: {str(e)}")
            
            # 检查超时
            if time.time() - start_time > max_wait_time:
                raise Exception("工作流执行超时（超过1小时）")
            
            time.sleep(check_interval)
            if log_callback:
                # 仅打印一个点表示仍在等待，避免使用不兼容的 flush 参数
                log_callback(".")
    
    def download_outputs(self, history: Dict, output_dir: str = "./outputs",
                        log_callback=None) -> Dict[str, str]:
        """下载生成的文件（支持图片、视频、音频等通用输出）"""
        os.makedirs(output_dir, exist_ok=True)
        outputs = {}
        
        output_data = history.get('outputs', {})
        for node_id, node_output in output_data.items():
            # 兼容多种输出类型：images / files / videos / gifs / audio
            for key in ('images', 'files', 'videos', 'gifs', 'audio'):
                if key not in node_output:
                    continue
                items = node_output.get(key) or []
                for idx, file_info in enumerate(items):
                    filename = file_info.get('filename')
                    if not filename:
                        continue
                    subfolder = file_info.get('subfolder', '')
                    file_type = file_info.get('type', 'output')

                    url = f"{self.server_address}/view"
                    params = {
                        'filename': filename,
                        'subfolder': subfolder,
                        'type': file_type
                    }

                    response = requests.get(url, params=params, timeout=120)
                    response.raise_for_status()

                    output_filename = f"{node_id}_{key}_{idx}_{filename}"
                    output_path = os.path.join(output_dir, output_filename)

                    with open(output_path, 'wb') as f:
                        f.write(response.content)

                    outputs[f"{node_id}_{key}_{idx}"] = output_path
                    if log_callback:
                        log_callback(f"已下载: {output_path}")
        
        return outputs


class ComfyUIApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ComfyUI LTX-2 视频生成工具")
        self.root.geometry("800x900")

        # 默认配置
        # 服务器IP地址（固定，不可修改）
        self.server_ip = "100.68.210.35:8188"  # 请根据实际情况修改此IP地址
        self.default_length = 121  # 帧数，根据帧率计算时长
        self.default_fps = 24
        self.default_width = 1280
        self.default_height = 720

        # Director JSON 模式用的取消标志
        self.should_stop = False

        # 创建界面
        self.create_widgets()
        
    def create_widgets(self):
        """创建GUI组件"""
        
        # 服务器配置（只读显示，IP在代码中固定）
        config_frame = tk.LabelFrame(self.root, text="服务器配置", padx=5, pady=5)
        config_frame.pack(fill="x", padx=10, pady=5)
        
        tk.Label(config_frame, text="ComfyUI 服务器地址:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        # 使用Label显示IP地址，更清晰明显
        ip_label = tk.Label(config_frame, text=self.server_ip, 
                           font=("Arial", 10, "bold"), 
                           fg="blue", 
                           bg="#F0F0F0",
                           relief=tk.SUNKEN,
                           padx=5, pady=2,
                           width=30,
                           anchor="w")
        ip_label.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        tk.Label(config_frame, text="(固定)", fg="gray", font=("Arial", 8)).grid(row=0, column=2, sticky="w", padx=2)

        # 生成模式：批量/参数 模式 vs Director JSON 模式
        mode_frame = tk.LabelFrame(self.root, text="生成模式", padx=5, pady=5)
        mode_frame.pack(fill="x", padx=10, pady=5)
        self.mode_var = tk.StringVar(value="batch")
        tk.Radiobutton(
            mode_frame, text="批量 / 参数模式", variable=self.mode_var,
            value="batch", command=self.on_mode_change
        ).pack(side="left", padx=10)
        tk.Radiobutton(
            mode_frame, text="Director JSON shots 模式", variable=self.mode_var,
            value="director", command=self.on_mode_change
        ).pack(side="left", padx=10)

        # 批量 / 参数模式容器
        self.batch_container = tk.Frame(self.root)
        self.batch_container.pack(fill="both", expand=False)

        # 工作流类型选择
        workflow_frame = tk.LabelFrame(self.batch_container, text="工作流类型", padx=5, pady=5)
        workflow_frame.pack(fill="x", padx=10, pady=5)
        
        self.workflow_type = tk.StringVar(value="t2v")
        tk.Radiobutton(workflow_frame, text="T2V (文本生成视频)", variable=self.workflow_type, 
                      value="t2v", command=self.on_workflow_change).pack(side="left", padx=10)
        tk.Radiobutton(workflow_frame, text="I2V (图片生成视频)", variable=self.workflow_type, 
                      value="i2v", command=self.on_workflow_change).pack(side="left", padx=10)
        
        # 视频参数
        video_frame = tk.LabelFrame(self.batch_container, text="视频参数", padx=5, pady=5)
        video_frame.pack(fill="x", padx=10, pady=5)
        
        tk.Label(video_frame, text="视频帧数 (length):").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.length_entry = tk.Entry(video_frame, width=20)
        self.length_entry.insert(0, str(self.default_length))
        self.length_entry.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        tk.Label(video_frame, text="(建议值: 能被8整除+1，如121, 241)").grid(row=0, column=2, sticky="w", padx=5)
        
        tk.Label(video_frame, text="帧率 (FPS):").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.fps_entry = tk.Entry(video_frame, width=20)
        self.fps_entry.insert(0, str(self.default_fps))
        self.fps_entry.grid(row=1, column=1, sticky="w", padx=5, pady=2)
        tk.Label(video_frame, text="fps").grid(row=1, column=2, sticky="w", padx=5)
        
        # 计算视频时长显示
        self.duration_label = tk.Label(video_frame, text="", fg="blue")
        self.duration_label.grid(row=2, column=0, columnspan=3, sticky="w", padx=5)
        self.update_duration_display()
        
        # 绑定长度和帧率变化
        self.length_entry.bind('<KeyRelease>', lambda e: self.update_duration_display())
        self.fps_entry.bind('<KeyRelease>', lambda e: self.update_duration_display())
        
        # 提示词输入
        prompt_frame = tk.LabelFrame(self.batch_container, text="提示词 (Prompt)", padx=5, pady=5)
        prompt_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.prompt_text = scrolledtext.ScrolledText(prompt_frame, height=8, wrap=tk.WORD)
        self.prompt_text.pack(fill="both", expand=True)
        
        # I2V 图片选择（初始隐藏）
        self.image_frame = tk.LabelFrame(self.batch_container, text="输入图片 (I2V)", padx=5, pady=5)
        self.image_frame.pack(fill="x", padx=10, pady=5)
        
        self.image_path_entry = tk.Entry(self.image_frame, width=50)
        self.image_path_entry.pack(side="left", fill="x", expand=True, padx=5, pady=5)
        
        tk.Button(self.image_frame, text="选择图片...", 
                 command=self.browse_image).pack(side="right", padx=5)
        
        # 初始状态：隐藏图片选择（因为默认是T2V）
        self.on_workflow_change()

        # Director JSON 模式容器（初始隐藏）
        self.director_container = tk.LabelFrame(self.root, text="Director JSON 模式参数", padx=5, pady=5)
        tk.Label(self.director_container, text="Director JSON 文件:").grid(
            row=0, column=0, sticky="e", padx=5, pady=2
        )
        self.director_json_entry = tk.Entry(self.director_container, width=50)
        self.director_json_entry.grid(row=0, column=1, sticky="we", padx=5, pady=2)
        tk.Button(self.director_container, text="选择文件...", command=self.browse_director_json).grid(
            row=0, column=2, padx=5, pady=2
        )
        tk.Label(self.director_container, text="产品图片 (I2V):").grid(
            row=1, column=0, sticky="e", padx=5, pady=2
        )
        self.director_image_entry = tk.Entry(self.director_container, width=50)
        self.director_image_entry.grid(row=1, column=1, sticky="we", padx=5, pady=2)
        tk.Button(self.director_container, text="选择图片...", command=self.browse_director_image).grid(
            row=1, column=2, padx=5, pady=2
        )

        # Director 批量次数
        tk.Label(self.director_container, text="批量次数:").grid(
            row=2, column=0, sticky="e", padx=5, pady=2
        )
        self.director_batch_entry = tk.Entry(self.director_container, width=10)
        self.director_batch_entry.insert(0, "1")
        self.director_batch_entry.grid(row=2, column=1, sticky="w", padx=5, pady=2)

        # 字幕参数
        tk.Label(self.director_container, text="字幕字号:").grid(
            row=3, column=0, sticky="e", padx=5, pady=2
        )
        self.subtitle_fontsize_entry = tk.Entry(self.director_container, width=10)
        self.subtitle_fontsize_entry.insert(0, "9")
        self.subtitle_fontsize_entry.grid(row=3, column=1, sticky="w", padx=5, pady=2)

        tk.Label(self.director_container, text="字幕底部边距 (MarginV):").grid(
            row=4, column=0, sticky="e", padx=5, pady=2
        )
        self.subtitle_margin_entry = tk.Entry(self.director_container, width=10)
        self.subtitle_margin_entry.insert(0, "60")
        self.subtitle_margin_entry.grid(row=4, column=1, sticky="w", padx=5, pady=2)

        self.director_container.columnconfigure(1, weight=1)

        # 执行按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=5)
        
        self.execute_btn = tk.Button(
            btn_frame,
            text="开始生成视频",
            command=self.start_generation,
            bg="#4CAF50",
            fg="white",
            font=("Arial", 12, "bold"),
            height=2,
        )
        self.execute_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.stop_btn = tk.Button(
            btn_frame,
            text="强制停止",
            command=self.stop_generation,
            bg="#F44336",
            fg="white",
            font=("Arial", 12, "bold"),
            height=2,
            state="disabled",
        )
        self.stop_btn.pack(side="right")
        
        # 日志显示
        log_frame = tk.LabelFrame(self.root, text="执行日志", padx=5, pady=5)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, height=10, state='disabled')
        self.log_area.pack(fill="both", expand=True)

        # 根据当前模式更新显示
        self.on_mode_change()
        
        # 运行文件确认（防止跑错脚本/旧exe）
        self.log(f"RUNNING FILE: {__file__}")
        if getattr(sys, 'frozen', False):
            self.log(f"EXECUTABLE: {sys.executable}")
        
    def update_duration_display(self):
        """更新视频时长显示"""
        try:
            length = int(self.length_entry.get())
            fps = float(self.fps_entry.get())
            if fps > 0:
                duration = length / fps
                minutes = int(duration // 60)
                seconds = duration % 60
                self.duration_label.config(
                    text=f"预计视频时长: {minutes}分{seconds:.2f}秒 ({duration:.2f}秒)"
                )
        except ValueError:
            self.duration_label.config(text="请输入有效的数值")
    
    def on_workflow_change(self):
        """工作流类型改变时的处理"""
        if self.workflow_type.get() == "i2v":
            self.image_frame.pack(fill="x", padx=10, pady=5)
        else:
            self.image_frame.pack_forget()

    def on_mode_change(self):
        """生成模式变更时切换 UI 显示"""
        mode = getattr(self, "mode_var", None)
        if mode is None:
            return
        mode = self.mode_var.get()
        if mode == "director":
            # 隐藏批量参数 UI，只显示 Director JSON + 产品图
            self.batch_container.pack_forget()
            self.director_container.pack(fill="x", padx=10, pady=5)
        else:
            # 显示原有 UI，隐藏 Director 区域
            self.director_container.pack_forget()
            self.batch_container.pack(fill="both", expand=False)
    
    def browse_image(self):
        """选择图片文件"""
        filename = filedialog.askopenfilename(
            title="选择输入图片",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.gif"),
                ("All files", "*.*")
            ]
        )
        if filename:
            self.image_path_entry.delete(0, tk.END)
            self.image_path_entry.insert(0, filename)
            self.log(f"已选择图片: {filename}")

    def browse_director_json(self):
        """选择 Director JSON 文件"""
        filename = filedialog.askopenfilename(
            title="选择 Director JSON 文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if filename:
            self.director_json_entry.delete(0, tk.END)
            self.director_json_entry.insert(0, filename)
            self.log(f"已选择 Director JSON: {filename}")

    def browse_director_image(self):
        """选择 Director 模式使用的产品图片"""
        filename = filedialog.askopenfilename(
            title="选择产品图片 (I2V)",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.gif"),
                ("All files", "*.*")
            ]
        )
        if filename:
            self.director_image_entry.delete(0, tk.END)
            self.director_image_entry.insert(0, filename)
            self.log(f"已选择产品图片: {filename}")

    def stop_generation(self):
        """用户点击强制停止"""
        if not self.should_stop:
            self.should_stop = True
            self.log("收到用户强制停止指令，将在当前步骤结束后终止。")
        self.stop_btn.config(state="disabled")
    
    def log(self, message, end="\n", **kwargs):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_area.config(state='normal')
        self.log_area.insert(tk.END, f"[{timestamp}] {message}{end}")
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')
        self.root.update_idletasks()
    
    def load_workflow(self, workflow_type: str) -> Dict:
        """加载工作流JSON文件"""
        # 根据类型选择候选文件名（优先使用你最新导出的 *NEWAPI (1).json）
        if workflow_type == "i2v":
            candidates = [
                "I2VNEWAPI (1).json",
                "I2VnewAPI.json",
                "I2VNEWAPI.json",
            ]
        else:
            candidates = [
                "T2VNEWAPI (1).json",
                "T2VnewAPI.json",
                "T2VNEWAPI.json",
            ]

        workflow_file = None
        for name in candidates:
            path = resource_path(name)
            if os.path.exists(path):
                workflow_file = path
                break

        if workflow_file is None:
            raise FileNotFoundError(f"找不到工作流文件（尝试过）: {', '.join(candidates)}")
        
        with open(workflow_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def set_text_input(self, inputs: Dict, text: str, node_id: str = "") -> bool:
        """兼容写入 value/text 两种字段的辅助函数"""
        if "value" in inputs:
            inputs["value"] = text
            return True
        elif "text" in inputs:
            inputs["text"] = text
            return True
        else:
            # 尝试找第一个是 str 类型的字段
            for key, val in inputs.items():
                if isinstance(val, str) and key not in ["image", "clip"]:
                    inputs[key] = text
                    if node_id:
                        self.log(f"[Negative][Warn] node {node_id} using fallback field '{key}'")
                    return True
            if node_id:
                self.log(f"[Negative][Warn] node {node_id} has no writable text field")
            return False
    
    def fallback_find_node_id(self, workflow: Dict, predicates: List[callable]) -> Optional[str]:
        """按 class_type + inputs 字段特征查找节点（fallback）"""
        # 检查是否是 api mapping 结构
        if isinstance(workflow, dict) and not any(isinstance(v, dict) and 'class_type' in v for v in workflow.values()):
            # 可能是旧版 nodes+links 结构，需要转换
            return None
        
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = node.get("class_type", "")
            inputs = node.get("inputs", {})
            
            # 检查所有谓词是否匹配
            if all(pred(class_type, inputs) for pred in predicates):
                return node_id
        
        return None
    
    def validate_workflow_nodes(self, workflow: Dict, workflow_type: str) -> Dict[str, str]:
        """验证工作流关键节点是否存在，返回节点ID映射（支持fallback）"""
        node_map = {}
        required_nodes = []
        
        if workflow_type == "t2v":
            required_nodes = [
                ("pos_prompt", ["5225"], lambda ct, inp: ct in ["CLIPTextEncode", "PrimitiveStringMultiline"] and ("value" in inp or "text" in inp)),
                ("neg_prompt", ["5226"], lambda ct, inp: ct in ["CLIPTextEncode", "PrimitiveStringMultiline"] and ("value" in inp or "text" in inp)),
                ("enhancer", ["5227"], lambda ct, inp: "LTXVGemmaEnhancePrompt" in ct or "Enhancer" in ct),
            ]
        elif workflow_type == "i2v":
            required_nodes = [
                ("pos_prompt", ["5175"], lambda ct, inp: ct in ["CLIPTextEncode", "PrimitiveStringMultiline"] and ("value" in inp or "text" in inp)),
                ("neg_prompt", ["5198"], lambda ct, inp: ct in ["CLIPTextEncode", "PrimitiveStringMultiline"] and ("value" in inp or "text" in inp)),
                ("image_input", ["5180"], lambda ct, inp: ct == "LoadImage" and "image" in inp),
                ("enhancer", ["5192"], lambda ct, inp: "LTXVGemmaEnhancePrompt" in ct or "Enhancer" in ct),
            ]
        
        for node_name, hardcoded_ids, predicate in required_nodes:
            found_id = None
            
            # 先尝试硬编码ID
            for node_id in hardcoded_ids:
                if node_id in workflow:
                    node = workflow.get(node_id)
                    if isinstance(node, dict):
                        class_type = node.get("class_type", "")
                        inputs = node.get("inputs", {})
                        if predicate(class_type, inputs):
                            found_id = node_id
                            break
            
            # 如果硬编码ID不存在或不符合，使用fallback
            if not found_id:
                found_id = self.fallback_find_node_id(workflow, [predicate])
                if found_id:
                    self.log(f"[Workflow][Fallback] {node_name} node found: {found_id} (hardcoded IDs {hardcoded_ids} not found)")
            
            if not found_id:
                raise ValueError(f"工作流版本不匹配：找不到必需的 {node_name} 节点（尝试过硬编码ID: {hardcoded_ids}）")
            
            node_map[node_name] = found_id
        
        return node_map

    def update_workflow_params(self, workflow: Dict, length: int, 
                              fps: float, prompt_text: str = "", 
                              image_path: str = "", negative_prompt: str = "",
                              render_mode: str = "t2v", voice_over: str = "", 
                              bgm_mood: str = "", sfx: str = "", seed: int = None,
                              shot_id: str = None, shot_data: Dict = None) -> Dict:
        """更新工作流参数（同时兼容旧版 nodes+links 工作流和新版 API JSON）"""
        
        # 验证工作流节点（仅对新版 API JSON）
        node_map = {}
        if isinstance(workflow, dict) and not ('nodes' in workflow and isinstance(workflow.get('nodes'), list)):
            # 新版 API JSON，验证节点
            try:
                node_map = self.validate_workflow_nodes(workflow, render_mode)
            except ValueError as e:
                self.log(f"[Workflow][Error] {str(e)}")
                raise

        # 情况1：旧版 ComfyUI 编辑器导出的 workflow（包含 nodes/links）
        if isinstance(workflow, dict) and 'nodes' in workflow and isinstance(workflow['nodes'], list):
            # 更新视频长度（帧数）
            for node in workflow.get('nodes', []):
                if node.get('type') == 'PrimitiveInt' and node.get('title') == 'length':
                    if node.get('widgets_values'):
                        node['widgets_values'][0] = length
                        break
            
            # 更新帧率
            for node in workflow.get('nodes', []):
                if node.get('type') == 'PrimitiveFloat' and node.get('title') == 'Frame Rate':
                    if node.get('widgets_values'):
                        node['widgets_values'][0] = fps
                        break
            
            # 更新正面提示词
            if prompt_text:
                for node in workflow.get('nodes', []):
                    if node.get('type') == 'PrimitiveStringMultiline' and node.get('title') == 'Positive Prompt':
                        if node.get('widgets_values'):
                            node['widgets_values'][0] = prompt_text
                            break
            
            # 更新负面提示词（如果提供）
            if negative_prompt:
                for node in workflow.get('nodes', []):
                    if node.get('type') == 'PrimitiveStringMultiline' and node.get('title') == 'Negative Prompt':
                        if node.get('widgets_values'):
                            node['widgets_values'][0] = negative_prompt
                            break
            
            # 更新图片路径（I2V）
            if image_path:
                for node in workflow.get('nodes', []):
                    if node.get('type') == 'LoadImage':
                        if node.get('widgets_values'):
                            node['widgets_values'][0] = image_path
                            break

            return workflow

        # 情况2：新版 API JSON（如 T2VNEWAPI / I2VNEWAPI），顶层是各节点 id 映射
        api = workflow

        # 长度（T2V:5233 / I2V:5186）
        for key in ("5233", "5186"):
            node = api.get(key)
            if isinstance(node, dict):
                inputs = node.get("inputs", {})
                if "value" in inputs:
                    inputs["value"] = length

        # 帧率（T2V:5236 / I2V:5184）
        for key in ("5236", "5184"):
            node = api.get(key)
            if isinstance(node, dict):
                inputs = node.get("inputs", {})
                if "value" in inputs:
                    inputs["value"] = fps

        # 正面提示词（使用节点映射）
        if prompt_text:
            pos_node_id = node_map.get("pos_prompt", None)
            if pos_node_id and pos_node_id in api:
                node = api.get(pos_node_id)
                if isinstance(node, dict):
                    inputs = node.get("inputs", {})
                    if self.set_text_input(inputs, prompt_text, pos_node_id):
                        self.log(f"Injected positive prompt into node {pos_node_id}")
            else:
                # fallback 到硬编码ID
                for key in ("5225", "5175"):
                    node = api.get(key)
                    if isinstance(node, dict):
                        inputs = node.get("inputs", {})
                        if "value" in inputs:
                            inputs["value"] = prompt_text
                            break

        # 负面提示词注入（使用节点映射）
        # 定义强制无文字负面约束
        DEFAULT_NO_TEXT_NEG = "text, subtitles, captions, words, letters, typography, watermark, logo, signage, UI, numbers, symbols, readable text, gibberish text"
        
        # 生成 negative_prompt_final（去重）
        negative_prompt_stripped = (negative_prompt or "").strip()
        if not negative_prompt_stripped:
            negative_prompt_final = DEFAULT_NO_TEXT_NEG
        else:
            # 检查是否已包含核心关键词，如果没有则追加（去重）
            negative_prompt_lower = negative_prompt_stripped.lower()
            has_keywords = any(kw in negative_prompt_lower for kw in ["subtitles", "words", "text", "letters", "typography", "watermark", "logo"])
            if not has_keywords:
                negative_prompt_final = negative_prompt_stripped + ", " + DEFAULT_NO_TEXT_NEG
            else:
                # 已包含关键词，但确保完整覆盖
                negative_prompt_final = negative_prompt_stripped
                # 检查是否缺少某些关键词，补充缺失的
                missing_keywords = []
                for kw in ["symbols", "readable text", "gibberish text"]:
                    if kw not in negative_prompt_lower:
                        missing_keywords.append(kw)
                if missing_keywords:
                    negative_prompt_final = negative_prompt_final + ", " + ", ".join(missing_keywords)
        
        # 按 render_mode 分流注入（使用节点映射）
        render_mode_lower = render_mode.lower()
        if render_mode_lower == "t2v":
            target_node_id = node_map.get("neg_prompt", "5226")  # fallback 到硬编码ID
        elif render_mode_lower == "i2v":
            target_node_id = node_map.get("neg_prompt", "5198")  # fallback 到硬编码ID
        else:
            # 默认使用 T2V
            target_node_id = node_map.get("neg_prompt", "5226")
        
        # 日志：显示 negative prompt 处理过程
        if negative_prompt_stripped:
            self.log(f"[Negative] 用户提供的 negative_prompt: '{negative_prompt_stripped[:100]}...' (前100字符)" if len(negative_prompt_stripped) > 100 else f"[Negative] 用户提供的 negative_prompt: '{negative_prompt_stripped}'")
        else:
            self.log(f"[Negative] 未提供用户 negative_prompt，使用默认值")
        self.log(f"[Negative] 最终 negative_prompt_final: '{negative_prompt_final[:150]}...' (前150字符)" if len(negative_prompt_final) > 150 else f"[Negative] 最终 negative_prompt_final: '{negative_prompt_final}'")
        self.log(f"[Negative] 目标节点 ID (render_mode={render_mode}): {target_node_id}")
        
        # 注入负面提示词（兼容两种工作流结构）
        injected = False
        if target_node_id in api:
            node = api.get(target_node_id)
            if isinstance(node, dict):
                inputs = node.get("inputs", {})
                old_value = inputs.get("value") or inputs.get("text") or ""
                if self.set_text_input(inputs, negative_prompt_final, target_node_id):
                    injected = True
                    self.log(f"[Negative] ✓ 成功注入到节点 {target_node_id} (旧值: '{old_value[:50]}...' -> 新值: '{negative_prompt_final[:50]}...')")
        
        # 如果节点映射方式失败，尝试硬编码ID（兼容旧代码）
        if not injected:
            target_nodes = [target_node_id] if target_node_id else (["5226"] if render_mode_lower == "t2v" else ["5198"])
            self.log(f"[Negative][Warn] 节点映射方式失败，尝试硬编码节点: {target_nodes}")
            for key in target_nodes:
                node = api.get(key)
                if isinstance(node, dict):
                    inputs = node.get("inputs", {})
                    old_value = inputs.get("value") or inputs.get("text") or ""
                    if self.set_text_input(inputs, negative_prompt_final, node_id=key):
                        self.log(f"[Negative] ✓ 成功注入到节点 {key} via 硬编码方式 (render_mode={render_mode})")
                        self.log(f"[Negative]   旧值: '{old_value[:50]}...' -> 新值: '{negative_prompt_final[:50]}...'")
                        injected = True
                        break
        
        # 如果仍然失败，记录警告
        if not injected:
            self.log(f"[Negative][错误] 无法注入 negative prompt！目标节点 {target_node_id} 不存在或无法写入")

        # I2V 图片（使用节点映射）
        if image_path:
            image_node_id = node_map.get("image_input", "5180")  # fallback 到硬编码ID
            if image_node_id in api:
                node = api.get(image_node_id)
                if isinstance(node, dict):
                    inputs = node.get("inputs", {})
                    if "image" in inputs:
                        inputs["image"] = os.path.basename(image_path)

        # Enhancer system_prompt 必须非空，否则 Comfy 会报错
        # 检测是否为 I2V 模式（基于 shot 数据）
        is_i2v_mode = False
        if shot_data:
            # 判断1: render_mode 为 "i2v" 或存在 i2v_image_ref/image_ref
            is_i2v_mode = (shot_data.get("render_mode", "").lower() == "i2v") or \
                         bool(shot_data.get("i2v_image_ref")) or \
                         bool(shot_data.get("image_ref"))
        
        # 判断2: shot_id 是 3 或 4（视为 I2V 强锁）
        if shot_id:
            shot_id_normalized = str(shot_id).strip()
            if shot_id_normalized in ("3", "4"):
                is_i2v_mode = True
        
        # 判断3: 如果 render_mode 参数为 "i2v" 或 image_path 存在，也视为 I2V
        if not is_i2v_mode:
            is_i2v_mode = (render_mode.lower() == "i2v") or bool(image_path)
        
        # T2V 专用 system prompt
        t2v_prompt = (
            "You are a director-style prompt converter that supports BOTH TEXT-TO-VIDEO (T2V) and IMAGE-TO-VIDEO (I2V) workflows.\n"
            "The input is a structured JSON for a single shot. Sometimes an image (first frame) is also provided.\n\n"
            "TASK:\n"
            "Convert the shot JSON into ONE prompt with ONLY a VISUAL section (no AUDIO section). Use present tense and chronological order. Keep it grounded and filmable.\n\n"
            "MODE RULE:\n"
            "- If a reference image / first frame is provided, treat this as IMAGE-TO-VIDEO (I2V): analyze the image (subject, setting, elements, style, mood) and describe ONLY the CHANGES and MOTION from that image. Do NOT re-describe established static details unless needed to prevent ambiguity. In case of conflict between user intent in the shot and the image, prioritize the user intent while maintaining visual consistency by describing a realistic transition from the image to the intended motion/actions.\n"
            "- If no reference image is provided, treat this as TEXT-TO-VIDEO (T2V): establish the scene first (setting, time of day, lighting, camera framing/movement), then describe clear physical actions and visible motion.\n\n"
            "WRITING RULES (applies to both modes):\n"
            "- Use active, action-focused language in present tense (present-progressive verbs like \"is walking,\" \"is speaking\"). If no action is specified, describe natural subtle movements.\n"
            "- Maintain chronological flow using temporal connectors (e.g., \"as,\" \"then,\" \"while\").\n"
            "- Avoid abstract emotions; show them via behavior and body language.\n"
            "- Do NOT add new characters or objects not implied by the JSON (or visible in the reference image for I2V).\n"
            "- Keep camera movement filmable and realistic; avoid impossible camera moves.\n\n"
            "STRICT VISUAL CONSTRAINTS (TEXT BAN):\n"
            "- VISUALS must NOT include any readable text, subtitles, captions, on-screen text, UI elements, logos, or brand names.\n"
            "- VISUALS must NOT contain text-like shapes or readable symbols, including: screens with text, sticky notes, posters with text, signs, labels, book pages, UI overlays, HUD elements, or any alphanumeric patterns.\n"
            "- If the scene would normally contain such elements, describe them as blank, blurred, or abstract non-readable blocks.\n"
            "- Ignore on_screen_text for visuals; it will be burned in later via ASS subtitles. Do NOT output any on-screen captions/subtitles as part of the VISUAL.\n\n"
            "STRICT AUDIO BAN (workflow constraint):\n"
            "- Do NOT mention BGM, music, soundtrack, score, background music, audio bed, or any music-related terms.\n"
            "- Do NOT output any \"AUDIO:\" section.\n"
            "- The main video workflow must NEVER generate or describe any BGM/music output. BGM is generated separately by a dedicated BGM workflow based on JSON's bgm_prompt field.\n\n"
            "VOICE_OVER (optional, controlled):\n"
            "- If shot.voice_over exists and is non-empty, append a separate line starting with:\n"
            "  VOICE_OVER: \"...\"\n"
            "  The text inside quotes MUST match shot.voice_over word-for-word, preserving punctuation and capitalization.\n"
            "- Do NOT invent dialogue if shot.voice_over is empty. Do NOT add extra speech beyond the provided voice_over.\n\n"
            "OUTPUT FORMAT:\n"
            "- Output prompt text only. Do NOT output JSON. Do NOT add explanations.\n"
            "- Output ONE continuous paragraph starting with \"VISUAL:\" followed by the visual description.\n"
            "- If voice_over exists, add a new line starting with \"VOICE_OVER:\" followed by the quoted text.\n"
            "- Do NOT use an \"AUDIO:\" label anywhere.\n\n"
            "LENGTH:\n"
            "- Keep the VISUAL section concise (about 80–120 words).\n"
            "- If uncertain, prefer minimal, consistent motion over large changes to avoid scene cuts."
        )
        
        # I2V 专用 system prompt（完整独立，不依赖 T2V base）
        i2v_prompt = (
            "You are a director-style prompt converter and Creative Assistant for an IMAGE-TO-VIDEO (I2V) workflow. The input is a structured JSON for a single shot (plus a reference image as the first frame). Your job is to convert the shot into a concise, action-focused I2V prompt that preserves the reference image identity and composition.\n\n"
            "CRITICAL: This is IMAGE-TO-VIDEO, not text-to-video. The reference image MUST be preserved exactly.\n\n"
            "ABSOLUTE I2V IDENTITY LOCK: The video MUST preserve the exact same product from the reference image. Preserve exact silhouette, proportions, colors, materials, buttons, ports, screen layout, and stand structure.\n"
            "NO REDESIGN: Do NOT redesign the product. Do NOT change shape, proportions, colors, materials, buttons, ports, screen layout, or stand structure.\n"
            "NO DEFORMATION: Do NOT deform, warp, stretch, melt, mutate, or break proportions. Do NOT add extra parts, missing parts, extra buttons, missing buttons, wrong screen shape, wrong bezel, or wrong stand structure.\n\n"
            "SCENE LOCK: Do NOT change the environment. Do NOT introduce new props or objects. Do NOT add people or hands unless they are already visible in the reference image. Keep the background and surroundings exactly as shown in the reference.\n"
            "COMPOSITION LOCK: Maintain stable framing and perspective. Avoid large camera moves. Avoid wide-angle distortions. Keep the product in the same relative position within the frame.\n\n"
            "PROMPT CONTENT RULES:\n"
            "- Use the shot's user intent (e.g., shot.visual_prompt / user raw intent) to decide ONLY the motion, micro-actions, and camera movement that can realistically happen without changing identity, scene, or composition.\n"
            "- Describe ONLY changes from the reference image. Do NOT restate established static details (subject, setting, colors, layout) unless needed to prevent ambiguity.\n"
            "- Use active, action-focused present tense with chronological flow (e.g., \"as,\" \"then,\" \"while\"). Prefer visible actions over emotion words.\n\n"
            "MOTION RULE (MINIMAL ONLY): Choose ONE and keep it subtle:\n"
            "- subtle push-in OR slow pan OR gentle micro handheld.\n"
            "No cuts, no transitions, no scene changes. If unsure, keep the product static and move the camera slightly.\n\n"
            "TEXT BAN: Do NOT include any readable text, logos, brand names, UI elements, or on-screen text in the visuals. No text on screens, no signs, no labels.\n"
            "AUDIO BAN: Do NOT mention BGM, music, soundtrack, score, or any music-related terms. Do NOT output any AUDIO section.\n\n"
            "VOICE_OVER (optional): If shot.voice_over field exists and is non-empty, append a separate line starting with \"VOICE_OVER: \" followed by the exact voice-over text in English double quotes, word-for-word, preserving all punctuation and capitalization. Do NOT invent dialogue if voice_over is empty.\n\n"
            "OUTPUT FORMAT:\n"
            "- Output prompt text only. Do NOT output JSON. Do NOT add explanations.\n"
            "- Output ONE continuous paragraph starting with \"VISUAL:\" followed by the visual description.\n"
            "- If voice_over exists, add a new line starting with \"VOICE_OVER:\" followed by the quoted text.\n"
            "- Keep VISUAL concise: ~60–100 words."
        )
        
        # 根据模式选择对应的 prompt
        enhancer_system_prompt = i2v_prompt if is_i2v_mode else t2v_prompt
        
        # Enhancer 节点注入（使用节点映射）
        enhancer_node_id = node_map.get("enhancer", None)
        enhancer_updated = False
        prompt_source = "raw_prompt"  # 默认来源
        
        # 尝试使用节点映射的 enhancer
        if enhancer_node_id and enhancer_node_id in api:
            node = api.get(enhancer_node_id)
            if isinstance(node, dict):
                inputs = node.get("inputs", {})
                # 只有当字段存在时才写入
                if "system_prompt" in inputs:
                    inputs["system_prompt"] = enhancer_system_prompt
                    enhancer_updated = True
                    prompt_source = "enhancer_output"
                    # 日志确认使用的 prompt 模式
                    mode_suffix = "_I2V_LOCKED" if is_i2v_mode else ""
                    self.log(f"[Enhancer] system_prompt_mode=NO_AUDIO{mode_suffix}")
                    self.log(f"[Enhancer] i2v_lock_applied={is_i2v_mode}")
                # 设置 max_tokens（兼容 max_tokens 和 max_new_tokens）
                if "max_tokens" in inputs:
                    inputs["max_tokens"] = 512
                    enhancer_updated = True
                elif "max_new_tokens" in inputs:
                    inputs["max_new_tokens"] = 512
                    enhancer_updated = True
                else:
                    self.log(f"[Enhancer][Warn] node {enhancer_node_id} has no max_tokens or max_new_tokens field")
                # 确保 enhancer 节点启用（只有当字段存在时才写入）
                if "bypass" in inputs:
                    inputs["bypass"] = False
                    enhancer_updated = True
                elif "bypass" in node:
                    node["bypass"] = False
                    enhancer_updated = True
                # 如果不存在 bypass 字段，不写也不报警（静默处理）
                # 设置随机种子（如果提供）
                if seed is not None and "seed" in inputs:
                    inputs["seed"] = seed
                    self.log(f"[Enhancer] seed set to {seed}")
        
        # fallback 到硬编码ID
        if not enhancer_updated:
            for key in ("5227", "5192"):
                node = api.get(key)
                if isinstance(node, dict):
                    inputs = node.get("inputs", {})
                    if "system_prompt" in inputs:
                        inputs["system_prompt"] = enhancer_system_prompt
                        enhancer_updated = True
                        prompt_source = "enhancer_output"
                        # 日志确认使用的 prompt 模式
                        mode_suffix = "_I2V_LOCKED" if is_i2v_mode else ""
                        self.log(f"[Enhancer] system_prompt_mode=NO_AUDIO{mode_suffix} (fallback)")
                        self.log(f"[Enhancer] i2v_lock_applied={is_i2v_mode}")
                    if "max_tokens" in inputs:
                        inputs["max_tokens"] = 512
                        enhancer_updated = True
                    elif "max_new_tokens" in inputs:
                        inputs["max_new_tokens"] = 512
                        enhancer_updated = True
                    if "bypass" in inputs:
                        inputs["bypass"] = False
                        enhancer_updated = True
                    elif "bypass" in node:
                        node["bypass"] = False
                        enhancer_updated = True
                    # 设置随机种子（如果提供）
                    if seed is not None and "seed" in inputs:
                        inputs["seed"] = seed
                        self.log(f"[Enhancer] seed set to {seed} (fallback)")
        
        if enhancer_updated:
            self.log(f"[Enhancer] system_prompt updated; max_tokens=1024")
        
        self.log(f"Prompt source: {prompt_source}")

        return api
    
    def generate_bgm_audio(self, api: ComfyUIAPI, director_data: Dict, output_dir: str, base_seed: int) -> str:
        """生成 BGM 音频文件"""
        # 加载 BGM 工作流 JSON
        # 优先尝试绝对路径（按顺序，同时兼容有无空格的文件名），然后使用 resource_path（支持打包后）
        workflow_candidates = [
            r"E:\vf\BGMAPI (1).json",  # 有空格（优先）
            r"E:\vf\BGMAPI(1).json",  # 无空格
            r"E:\vf\bgmAPI.json",  # 小写版本
            os.path.abspath("BGMAPI (1).json"),
            os.path.abspath("BGMAPI(1).json"),
            os.path.abspath("bgmAPI.json"),
            os.path.join(os.path.dirname(__file__), "BGMAPI (1).json"),
            os.path.join(os.path.dirname(__file__), "BGMAPI(1).json"),
            os.path.join(os.path.dirname(__file__), "bgmAPI.json"),
            # 使用 resource_path（支持打包后的 exe）
            resource_path("BGMAPI (1).json"),
            resource_path("BGMAPI(1).json"),
            resource_path("bgmAPI.json"),
        ]
        
        bgm_workflow_path = next((p for p in workflow_candidates if os.path.exists(p)), None)
        
        if not bgm_workflow_path:
            self.log("[ERROR] BGM workflow not found. Tried:\n" + "\n".join(workflow_candidates))
            raise FileNotFoundError(f"找不到 BGM 工作流文件（尝试过 {len(workflow_candidates)} 个路径）")
        
        self.log(f"[INFO] Using BGM workflow: {bgm_workflow_path}")
        with open(bgm_workflow_path, 'r', encoding='utf-8') as f:
            workflow = json.load(f)
        
        # 构建 BGM prompt（直接从 Director JSON 读取，优先级：bgm_prompt > bgm_mood > 默认）
        bgm_prompt = director_data.get('bgm_prompt', '').strip()
        bgm_prompt_source = "json.bgm_prompt"
        
        if not bgm_prompt:
            # fallback 到 bgm_mood（从所有 shots 收集）
            shots = director_data.get('shots', [])
            bgm_moods = []
            for shot in shots:
                bgm_mood = shot.get('bgm_mood', '').strip()
                if bgm_mood:
                    bgm_moods.append(bgm_mood.lower())
            
            if bgm_moods:
                # 使用 build_bgm_prompt_from_director 从 bgm_mood 构建
                bgm_prompt = build_bgm_prompt_from_director(director_data)
                bgm_prompt_source = "json.bgm_mood(default)"
            else:
                # 最终 fallback：默认极简科技感广告 BGM
                bgm_prompt = "Minimal premium product ad background music, 100 BPM, modern clean mix, soft synth pad, warm pluck melody, muted percussion, gentle sidechain, subtle bass, spacious reverb, crisp highs, smooth dynamics, loopable, uplifting but calm, no vocals, no speech, no lyrics."
                bgm_prompt_source = "default_template"
        
        # 负向 prompt（优先从 JSON 读取，否则使用默认）
        bgm_negative_prompt = director_data.get('bgm_negative_prompt', '').strip()
        if not bgm_negative_prompt:
            bgm_negative_prompt = "vocals, singing, speech, rap, lyrics, no vocals, no speech, no lyrics, harsh distortion, heavy bass drop, aggressive drums, chaotic melody, lo-fi noise, crowd sounds, sirens, long intro, abrupt ending, clipping."
        
        # 时长（优先 bgm_seconds，否则从 director_data 推断）
        bgm_seconds = director_data.get('bgm_seconds', None)
        if bgm_seconds is not None:
            duration = float(bgm_seconds)
        else:
            duration = get_video_duration_from_director(director_data)
        
        # 日志
        self.log(f"[BGM] prompt source={bgm_prompt_source}")
        self.log(f"[BGM] prompt={bgm_prompt[:200]}..." if len(bgm_prompt) > 200 else f"[BGM] prompt={bgm_prompt}")
        self.log(f"[BGM] negative={bgm_negative_prompt[:200]}..." if len(bgm_negative_prompt) > 200 else f"[BGM] negative={bgm_negative_prompt}")
        self.log(f"[BGM] duration={duration:.1f}s")
        
        # 更新工作流参数
        # 正向 prompt：node 6
        if "6" in workflow:
            inputs = workflow["6"].get("inputs", {})
            if "text" in inputs:
                inputs["text"] = bgm_prompt
        
        # 负向 prompt：node 7
        if "7" in workflow:
            inputs = workflow["7"].get("inputs", {})
            if "text" in inputs:
                inputs["text"] = bgm_negative_prompt
        
        # 时长：node 11
        if "11" in workflow:
            inputs = workflow["11"].get("inputs", {})
            if "seconds" in inputs:
                inputs["seconds"] = int(round(duration))
        
        # 随机种子：node 3
        if "3" in workflow:
            inputs = workflow["3"].get("inputs", {})
            if "seed" in inputs:
                inputs["seed"] = base_seed + 999
        
        # 提交工作流
        self.log(f"提交 BGM 生成工作流（时长: {duration:.1f}秒）...")
        # 打印工作流参数快照
        self.log("[WF][Debug] ---- Workflow param snapshot (before queue) ----")
        results = scan_workflow_params(workflow)
        snapshot = format_workflow_param_snapshot(results)
        for line in snapshot.splitlines():
            self.log(line)
        # 告警检查
        if results:
            for record in results:
                params = record.get("params", {})
                if "strength" in params:
                    strength_val = params["strength"]
                    if isinstance(strength_val, (int, float)) and strength_val < 0.7:
                        node_id = record.get("node_id", "?")
                        self.log(f"[WF][Warn] Detected low strength={strength_val} at node {node_id}. This may cause product deformation.")
        else:
            self.log("[WF][Warn] No key params (strength/cfg/steps/seed/etc) found in workflow inputs. Verify node keys or class_type.")
        self.log("[WF][Debug] ---- End snapshot ----")
        prompt_id = api.queue_prompt(workflow)
        self.log(f"BGM 工作流已提交，Prompt ID: {prompt_id}")
        
        # 等待执行完成
        self.log("等待 BGM 生成完成...")
        history = api.wait_for_completion(
            prompt_id,
            log_callback=lambda msg, **kw: self.log(f"  {msg}", **kw),
            cancel_callback=lambda: self.should_stop
        )
        
        # 下载输出
        bgm_output_dir = os.path.join(output_dir, "bgm")
        os.makedirs(bgm_output_dir, exist_ok=True)
        outputs = api.download_outputs(history, bgm_output_dir, log_callback=self.log)
        
        # 找到音频文件（wav/mp3/m4a/aac/flac/ogg 任一）
        audio_file = find_audio_file(outputs)
        if not audio_file:
            raise RuntimeError(f"BGM 生成未找到音频文件。下载的文件: {list(outputs.keys())}")
        
        self.log(f"BGM 音频文件: {audio_file}")
        self.log(f"bgm_audio_path: {os.path.abspath(audio_file)}")
        return audio_file
    
    def start_generation(self):
        """开始生成视频"""
        # 使用固定的服务器IP地址
        ip_address = self.server_ip
        
        # 检查模式
        mode = self.mode_var.get() if hasattr(self, 'mode_var') else "batch"
        
        if mode == "director":
            # Director JSON 模式
            json_path = self.director_json_entry.get().strip()
            if not json_path:
                messagebox.showerror("错误", "请选择 Director JSON 文件!")
                return
            if not os.path.exists(json_path):
                messagebox.showerror("错误", f"Director JSON 文件不存在: {json_path}")
                return
            
            product_image = self.director_image_entry.get().strip()
            if not product_image:
                messagebox.showerror("错误", "请选择产品图片!")
                return
            if not os.path.exists(product_image):
                messagebox.showerror("错误", f"产品图片文件不存在: {product_image}")
                return

            # 批量次数
            try:
                batch_count = int(self.director_batch_entry.get().strip() or "1")
            except ValueError:
                batch_count = 1
            if batch_count <= 0:
                batch_count = 1

            # 字幕参数
            try:
                subtitle_fontsize = int(self.subtitle_fontsize_entry.get().strip() or "9")
            except ValueError:
                subtitle_fontsize = 9
            try:
                subtitle_margin_v = int(self.subtitle_margin_entry.get().strip() or "60")
            except ValueError:
                subtitle_margin_v = 60

            # 保存到实例，供生成函数使用
            self.director_batch_count = batch_count
            self.subtitle_fontsize = subtitle_fontsize
            self.subtitle_margin_v = subtitle_margin_v
            
            # 检查ffmpeg
            if not shutil.which("ffmpeg"):
                messagebox.showerror("错误", "未找到 ffmpeg 命令！\n请确保已安装 ffmpeg 并添加到 PATH 环境变量。")
                return
            
            # 重置停止标志
            self.should_stop = False
            self.execute_btn.config(state='disabled', text="生成中...")
            self.stop_btn.config(state='normal')
            
            thread = threading.Thread(
                target=self.generate_director_json,
                args=(ip_address, json_path, product_image)
            )
            thread.daemon = True
            thread.start()
        else:
            # 批量/参数模式
            try:
                length = int(self.length_entry.get())
                if length <= 0:
                    raise ValueError("帧数必须大于0")
            except ValueError:
                messagebox.showerror("错误", "请输入有效的帧数!")
                return
            
            try:
                fps = float(self.fps_entry.get())
                if fps <= 0:
                    raise ValueError("帧率必须大于0")
            except ValueError:
                messagebox.showerror("错误", "请输入有效的帧率!")
                return
            
            prompt_text = self.prompt_text.get("1.0", tk.END).strip()
            if not prompt_text:
                messagebox.showwarning("警告", "请输入提示词!")
                return
            
            workflow_type = self.workflow_type.get()
            image_path = ""
            if workflow_type == "i2v":
                image_path = self.image_path_entry.get().strip()
                if not image_path:
                    messagebox.showwarning("警告", "请选择输入图片!")
                    return
                if not os.path.exists(image_path):
                    messagebox.showerror("错误", f"图片文件不存在: {image_path}")
                    return
            
            # 禁用按钮，开始生成
            self.execute_btn.config(state='disabled', text="生成中...")
            
            thread = threading.Thread(
                target=self.generate_video,
                args=(ip_address, workflow_type, length, fps, prompt_text, image_path)
            )
            thread.daemon = True
            thread.start()
    
    def generate_video(self, ip_address: str, workflow_type: str, 
                      length: int, fps: float, prompt_text: str, image_path: str):
        """生成视频（后台线程）"""
        try:
            self.log(f"开始生成视频...")
            self.log(f"工作流类型: {workflow_type.upper()}")
            self.log(f"服务器地址: {ip_address}")
            self.log(f"视频帧数: {length}")
            self.log(f"帧率: {fps} fps")
            
            # 加载工作流
            self.log("加载工作流文件...")
            workflow = self.load_workflow(workflow_type)
            
            # 更新工作流参数
            self.log("更新工作流参数...")
            workflow = self.update_workflow_params(
                workflow, length, fps, prompt_text, image_path
            )
            
            # 连接服务器
            self.log(f"连接到ComfyUI服务器: {ip_address}")
            api = ComfyUIAPI(ip_address)
            
            # 提交工作流
            self.log("提交工作流到队列...")
            # 打印工作流参数快照
            self.log("[WF][Debug] ---- Workflow param snapshot (before queue) ----")
            results = scan_workflow_params(workflow)
            snapshot = format_workflow_param_snapshot(results)
            for line in snapshot.splitlines():
                self.log(line)
            # 告警检查
            if results:
                for record in results:
                    params = record.get("params", {})
                    if "strength" in params:
                        strength_val = params["strength"]
                        if isinstance(strength_val, (int, float)) and strength_val < 0.7:
                            node_id = record.get("node_id", "?")
                            self.log(f"[WF][Warn] Detected low strength={strength_val} at node {node_id}. This may cause product deformation.")
            else:
                self.log("[WF][Warn] No key params (strength/cfg/steps/seed/etc) found in workflow inputs. Verify node keys or class_type.")
            self.log("[WF][Debug] ---- End snapshot ----")
            prompt_id = api.queue_prompt(workflow)
            self.log(f"工作流已提交，Prompt ID: {prompt_id}")
            
            # 等待执行完成
            self.log("等待工作流执行...")
            history = api.wait_for_completion(prompt_id, log_callback=self.log)
            
            # 下载输出文件
            self.log("\n开始下载生成的文件...")
            output_dir = os.path.abspath("./outputs")
            outputs = api.download_outputs(history, output_dir, log_callback=self.log)
            
            if outputs:
                self.log(f"\n成功! 共下载 {len(outputs)} 个文件:")
                for name, path in outputs.items():
                    self.log(f"  {name}: {path}")
                
                self.root.after(0, lambda: messagebox.showinfo(
                    "成功", 
                    f"视频生成完成!\n共生成 {len(outputs)} 个文件\n保存目录: {output_dir}"
                ))
            else:
                self.log("\n警告: 未找到输出文件")
                self.root.after(0, lambda: messagebox.showwarning(
                    "警告", "生成完成但未找到输出文件"
                ))
                
        except FileNotFoundError as e:
            self.log(f"错误: {str(e)}")
            self.root.after(0, lambda: messagebox.showerror("错误", f"找不到文件: {str(e)}"))
        except requests.exceptions.ConnectionError:
            self.log(f"错误: 无法连接到ComfyUI服务器 {ip_address}")
            self.root.after(0, lambda: messagebox.showerror(
                "连接错误", 
                f"无法连接到ComfyUI服务器 {ip_address}\n请确保ComfyUI正在运行并且地址正确"
            ))
        except requests.exceptions.HTTPError as e:
            self.log(f"HTTP错误: {str(e)}")
            self.root.after(0, lambda: messagebox.showerror("HTTP错误", f"服务器返回错误: {str(e)}"))
        except Exception as e:
            self.log(f"错误: {str(e)}")
            self.root.after(0, lambda: messagebox.showerror("错误", f"生成失败: {str(e)}"))
        finally:
            self.root.after(0, lambda: self.execute_btn.config(
                state='normal', 
                text="开始生成视频"
            ))
    
    def generate_director_json(self, ip_address: str, json_path: str, product_image: str):
        """Director JSON 模式：逐shot生成、烧字幕、拼接"""
        try:
            batch_count = getattr(self, "director_batch_count", 1)

            for batch_index in range(batch_count):
                self.log("=" * 60)
                if batch_count > 1:
                    self.log(f"开始第 {batch_index + 1}/{batch_count} 次 Director JSON 生成...")
                else:
                    self.log("Director JSON 模式启动")
                self.log(f"JSON 文件: {json_path}")
                self.log(f"产品图片: {product_image}")
                
                # 加载Director JSON
                with open(json_path, 'r', encoding='utf-8') as f:
                    director_data = json.load(f)
                
                shots = director_data.get('shots', [])
                if not shots:
                    raise ValueError("Director JSON 中未找到 shots 数组")
                
                self.log(f"共 {len(shots)} 个 shot")
                
                # 时间轴校验（不阻断，只警告）
                video_meta = director_data.get('video_meta', {})
                validate_timeline(shots, video_meta, self.default_fps, log_callback=self.log)
                
                # 创建输出目录（每次批量一个单独目录，包含时间戳+随机短串，防止覆盖）
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                random_suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=4))
                output_base = os.path.abspath(f"./outputs/director_{timestamp}_{random_suffix}")
                os.makedirs(output_base, exist_ok=True)
                shots_dir = os.path.join(output_base, "shots")
                os.makedirs(shots_dir, exist_ok=True)
                
                # 生成 run_id（包含时间戳+batch_index，例如 20260130_153012_b1）
                batch_suffix = f"_b{batch_index + 1}" if batch_count > 1 else ""
                run_id = f"{timestamp}{batch_suffix}"
                
                # 连接API
                api = ComfyUIAPI(ip_address)
                
                # 处理每个shot
                processed_videos = []
                
                for idx, shot in enumerate(shots):
                    if self.should_stop:
                        self.log("用户取消，停止生成")
                        raise Exception("用户取消")
                    
                    shot_id = shot.get('shot_id', idx + 1)
                    self.log(f"\n--- 处理 Shot {shot_id} ({idx+1}/{len(shots)}) ---")
                    
                    # 解析字段
                    render_mode = shot.get('render_mode', 't2v').lower()
                    
                    # 强制 shot3/4 使用 I2V（硬性要求）
                    shot_id_str = str(shot_id).strip()
                    if shot_id_str in ("3", "4"):
                        if render_mode != "i2v":
                            self.log(f"  [强制I2V] Shot {shot_id} 的 render_mode 从 '{render_mode}' 强制改为 'i2v'")
                            render_mode = "i2v"
                        # 确保有 i2v_image_ref 或 image_ref 字段
                        if not shot.get('i2v_image_ref') and not shot.get('image_ref'):
                            self.log(f"  [强制I2V] Shot {shot_id} 缺少 i2v_image_ref/image_ref，自动设置为 'product'")
                            shot['i2v_image_ref'] = 'product'
                    
                    visual_prompt = shot.get('visual_prompt', '').strip()
                    negative_prompt = shot.get('negative_prompt') or shot.get('negative') or shot.get('neg_prompt') or ''
                    voice_over = shot.get('voice_over', '').strip()
                    bgm_mood = shot.get('bgm_mood', '').strip()
                    sfx = shot.get('sfx', '').strip()
                    on_screen_text = shot.get('on_screen_text', '').strip()
                    time_range = shot.get('time_range', '')
                    
                    # 构造完整的 prompt_text（不包含 on_screen_text，它只用于 ASS 烧录）
                    # 只包含 VISUAL 段落，可选 VOICE_OVER（不包含 BGM，BGM 由独立工作流生成）
                    
                    # VISUAL 段落
                    visual_parts = []
                    if visual_prompt:
                        visual_parts.append(visual_prompt)
                    # 强制禁止画面文字约束
                    visual_parts.append("No readable text, no subtitles, no captions, no UI overlays, no logos, no labels, no signs, no letters or numbers anywhere in the frame.")
                    visual_section = " ".join(visual_parts)
                    
                    # 构建 prompt_text：VISUAL + 可选的 VOICE_OVER
                    if voice_over:
                        prompt_text = f"VISUAL: {visual_section} VOICE_OVER: \"{voice_over}\""
                    else:
                        prompt_text = f"VISUAL: {visual_section}"
                    
                    # 解析时间范围
                    if time_range:
                        start_sec, end_sec = parse_time_range_to_seconds(time_range)
                        duration_sec = end_sec - start_sec
                    else:
                        duration_sec = None
                        start_sec = 0
                        end_sec = 0
                    
                    # 获取fps和length
                    fps = shot.get('fps') or shot.get('frame_rate') or self.default_fps
                    if duration_sec is not None:
                        length = max(1, round(duration_sec * fps))
                    else:
                        length = shot.get('length') or shot.get('frames') or self.default_length
                        duration_sec = length / fps
                        if not time_range:
                            end_sec = start_sec + duration_sec
                    
                    # 日志
                    self.log(f"  render_mode: {render_mode}")
                    self.log(f"  duration: {duration_sec:.2f}s")
                    self.log(f"  fps: {fps}")
                    self.log(f"  length: {length} frames")
                    if voice_over:
                        self.log(f"  voice_over: {voice_over[:50]}...")
                    if bgm_mood:
                        self.log(f"  bgm_mood: {bgm_mood}")
                    if sfx:
                        self.log(f"  sfx: {sfx}")
                    if on_screen_text:
                        self.log(f"  字幕: {on_screen_text[:50]}...")
                    
                    # 选择工作流
                    workflow = self.load_workflow(render_mode)
                    
                    # 准备参数
                    image_path_for_shot = ""
                    if render_mode == "i2v":
                        # I2V模式使用产品图片
                        image_path_for_shot = product_image
                        if not os.path.exists(image_path_for_shot):
                            raise FileNotFoundError(f"I2V shot {shot_id} 需要的产品图片不存在: {image_path_for_shot}")
                    
                    # 为每个 shot 生成随机种子（确保每次生成结果不同）
                    shot_seed = random.randint(100000000, 999999999)
                    self.log(f"  [Seed] Shot {shot_id} 使用随机种子: {shot_seed}")
                    
                    # 更新工作流参数（传入完整的 prompt_text 和音频字段，以及随机种子、shot_id 和 shot_data）
                    workflow = self.update_workflow_params(
                        workflow, length, fps, prompt_text, image_path_for_shot, negative_prompt,
                        render_mode=render_mode, voice_over=voice_over, bgm_mood=bgm_mood, sfx=sfx, seed=shot_seed,
                        shot_id=shot_id, shot_data=shot
                    )
                    
                    # I2V shot3/4 防变形注入（在 queue_prompt 之前）
                    workflow = apply_i2v_anti_deform_lock(workflow, shot_id, render_mode, log_callback=self.log)
                    
                    # 日志：打印最终送入工作流的 prompt_text
                    self.log(f"  最终 prompt_text (前280字): {prompt_text[:280]}...")
                    if voice_over:
                        self.log(f"  [检测到 voice_over 非空，将生成人声]")
                    # 注意：bgm_mood 仅用于后续 BGM 工作流，不参与视频工作流 prompt
                    
                    # 提交并等待
                    self.log(f"  提交工作流到队列...")
                    # 打印工作流参数快照
                    self.log("[WF][Debug] ---- Workflow param snapshot (before queue) ----")
                    results = scan_workflow_params(workflow)
                    snapshot = format_workflow_param_snapshot(results)
                    for line in snapshot.splitlines():
                        self.log(line)
                    # 告警检查
                    if results:
                        for record in results:
                            params = record.get("params", {})
                            if "strength" in params:
                                strength_val = params["strength"]
                                if isinstance(strength_val, (int, float)) and strength_val < 0.7:
                                    node_id = record.get("node_id", "?")
                                    self.log(f"[WF][Warn] Detected low strength={strength_val} at node {node_id}. This may cause product deformation.")
                    else:
                        self.log("[WF][Warn] No key params (strength/cfg/steps/seed/etc) found in workflow inputs. Verify node keys or class_type.")
                    self.log("[WF][Debug] ---- End snapshot ----")
                    prompt_id = api.queue_prompt(workflow)
                    
                    self.log(f"  等待执行完成 (Prompt ID: {prompt_id})...")
                    history = api.wait_for_completion(
                        prompt_id,
                        log_callback=lambda msg, **kw: self.log(f"    {msg}", **kw),
                        cancel_callback=lambda: self.should_stop
                    )
                    
                    # 下载输出
                    shot_output_dir = os.path.join(shots_dir, f"shot_{shot_id}")
                    os.makedirs(shot_output_dir, exist_ok=True)
                    outputs = api.download_outputs(history, shot_output_dir, log_callback=self.log)
                    
                    # 找到视频文件（优先 mp4，若多个选最新）
                    video_file = find_video_file(outputs, log_callback=self.log)
                    if not video_file:
                        raise RuntimeError(f"Shot {shot_id} 未找到视频文件。下载的文件: {list(outputs.keys())}")
                    
                    self.log(f"  视频文件: {video_file}")
                    self.log(f"  选中文件路径: {os.path.abspath(video_file)}")
                    
                    # 确保是mp4格式（用于后续拼接）
                    shot_video_mp4 = os.path.join(shot_output_dir, f"shot_{shot_id}.mp4")
                    if os.path.splitext(video_file)[1].lower() != '.mp4':
                        self.log(f"  转码为 MP4...")
                        transcode_to_mp4(video_file, shot_video_mp4, log_callback=self.log)
                    else:
                        # 如果已经是mp4，直接复制或移动
                        if video_file != shot_video_mp4:
                            shutil.copy2(video_file, shot_video_mp4)
                    
                    # 如果有字幕，烧录（字幕时间相对于shot视频本身，从0到duration_sec）
                    final_shot_video = shot_video_mp4
                    if on_screen_text:
                        self.log(f"  烧录字幕: {on_screen_text[:50]}...")
                        ass_file = os.path.join(shot_output_dir, f"shot_{shot_id}.ass")
                        with open(ass_file, 'w', encoding='utf-8') as f:
                            # 字幕时间相对于shot视频：从0到duration_sec
                            font_size = getattr(self, "subtitle_fontsize", 9)
                            margin_v = getattr(self, "subtitle_margin_v", 60)
                            f.write(create_ass_subtitle(on_screen_text, 0.0, duration_sec, font_size, margin_v))
                        
                        shot_video_with_sub = os.path.join(shot_output_dir, f"shot_{shot_id}_sub.mp4")
                        final_shot_video = burn_subtitle_ffmpeg(shot_video_mp4, ass_file, shot_video_with_sub, log_callback=self.log)
                    
                    # 检测音频流（每个 shot 生成完成后立刻检测）
                    audio_streams = check_audio_streams(final_shot_video, log_callback=self.log)
                    if audio_streams == 0:
                        self.log(f"[Audio][Warn] Shot {shot_id} has no audio stream")
                    
                    processed_videos.append(final_shot_video)
                    self.log(f"  Shot {shot_id} 完成")
            
                # 拼接所有片段
                self.log(f"\n--- 拼接 {len(processed_videos)} 个片段 ---")
                final_output = os.path.join(output_base, "final_with_subtitles.mp4")
                concat_videos_ffmpeg(processed_videos, final_output, log_callback=self.log)
                
                # 检测最终视频的音频流（concat 完成后检测）
                self.log(f"检测最终视频音频流...")
                final_audio_streams = check_audio_streams(final_output, log_callback=self.log)
                if final_audio_streams == 0:
                    self.log(f"[Audio][Warn] Final video has no audio stream (voice over not generated by workflow)")

                # 创建带时间戳的最终输出文件夹
                simple_outputs_dir = os.path.abspath("./outputs")
                os.makedirs(simple_outputs_dir, exist_ok=True)
                
                # 创建带时间戳的输出文件夹（每个 final 视频一个文件夹）
                final_output_dir = os.path.join(simple_outputs_dir, f"final_{run_id}")
                os.makedirs(final_output_dir, exist_ok=True)
                
                # 最终输出文件路径（在带时间戳的文件夹中）
                final_result_path = None
                
                # 生成 BGM 并混音（在 final.mp4 成功生成之后）
                try:
                    self.log(f"\n--- 生成 BGM 音频 ---")
                    # 使用随机 base_seed（与视频生成保持一致）
                    base_seed = random.randint(100000000, 999999999)
                    bgm_audio_path = self.generate_bgm_audio(api, director_data, output_base, base_seed)
                    
                    # 将 bgm 混入 final.mp4 输出最终成片 final_with_bgm.mp4
                    self.log(f"\n--- 混音 BGM 到视频 ---")
                    BGM_VOLUME = 0.25
                    final_with_bgm = os.path.join(output_base, "final_with_bgm.mp4")
                    
                    final_with_bgm = mix_bgm_into_video(final_output, bgm_audio_path, final_with_bgm, BGM_VOLUME, log_callback=self.log)
                    
                    # 混音完成后，复制到最终输出文件夹
                    final_result_path = os.path.join(final_output_dir, "final.mp4")
                    shutil.copy2(final_with_bgm, final_result_path)
                    
                    # 检测音频流
                    final_audio_streams = ffprobe_audio_streams(final_result_path)
                    self.log(f"最终视频 audio_streams: {final_audio_streams}")
                    
                except FileNotFoundError as e:
                    self.log(f"[Warn] BGM 生成失败（文件未找到）: {str(e)}")
                    self.log(f"[Warn] 跳过 BGM，使用含人声的视频作为最终结果")
                    # BGM 失败时，使用含人声的视频作为最终结果
                    final_result_path = os.path.join(final_output_dir, "final.mp4")
                    shutil.copy2(final_output, final_result_path)
                except RuntimeError as e:
                    self.log(f"[Warn] BGM 混音失败: {str(e)}")
                    self.log(f"[Warn] 跳过 BGM，使用含人声的视频作为最终结果")
                    # BGM 失败时，使用含人声的视频作为最终结果
                    final_result_path = os.path.join(final_output_dir, "final.mp4")
                    shutil.copy2(final_output, final_result_path)
                except Exception as e:
                    self.log(f"[Warn] BGM 处理异常: {str(e)}")
                    import traceback
                    self.log(traceback.format_exc())
                    self.log(f"[Warn] 跳过 BGM，使用含人声的视频作为最终结果")
                    # BGM 失败时，使用含人声的视频作为最终结果
                    final_result_path = os.path.join(final_output_dir, "final.mp4")
                    shutil.copy2(final_output, final_result_path)
                
                # 输出最终结果
                self.log(f"\n{'='*60}")
                self.log(f"Director JSON 处理完成!")
                self.log(f"[OUTPUT] 最终视频: {final_result_path}")
                
                # 构建弹窗消息（只显示最终结果）
                msg = f"Director JSON 处理完成!\n\n最终视频:\n{final_result_path}"
                
                self.root.after(0, lambda m=msg: messagebox.showinfo("成功", m))
            
        except Exception as e:
            error_msg = str(e)
            self.log(f"\n错误: {error_msg}")
            import traceback
            self.log(traceback.format_exc())
            self.root.after(0, lambda: messagebox.showerror("错误", f"Director JSON 处理失败:\n{error_msg}"))
        finally:
            self.root.after(0, lambda: self.execute_btn.config(
                state='normal',
                text="开始生成视频"
            ))
            self.root.after(0, lambda: self.stop_btn.config(state='disabled'))


if __name__ == '__main__':
    root = tk.Tk()
    app = ComfyUIApp(root)
    root.mainloop()

