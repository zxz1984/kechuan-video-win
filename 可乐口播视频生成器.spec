# -*- mode: python ; coding: utf-8 -*-
# v1.77：把 tkinter 用的 tcl-tk 框架和 _tkinter.so 一起打进 .app
# 不然用户机器没装 Homebrew tcl-tk 的话，import tkinter 会失败
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[
        # ffmpeg/ffprobe 必须用 binaries= 而非 datas=
        # 不然 PyInstaller 会把它们当 bundle 处理，打成 bin/ffmpeg/ffmpeg 这种嵌套结构
        # 嵌套结构会被 macOS Gatekeeper 判"已损坏"
        ('bin/ffmpeg', 'bin'),
        ('bin/ffprobe', 'bin'),
    ] + collect_dynamic_libs('tkinter'),
    datas=[
        ('config.yaml', '.'),
        ('material_library.json', '.'),
        ('action_prompts.json', '.'),
        ('personas.json', '.'),
        ('stats_history.json', '.'),
    ] + collect_data_files('tkinter'),
    hiddenimports=['yaml', 'tkinter', 'requests', 'urllib3', 'merge_tab', 'pydub', 'pydub.utils', 'pydub.audio_segment', 'audioop'],

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pandas', 'PIL.ImageQt'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='可乐口播视频生成器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='可乐口播视频生成器',
)
app = BUNDLE(
    coll,
    name='可乐口播视频生成器.app',
    icon='icon.icns',
    bundle_identifier=None,
)
