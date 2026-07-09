# -*- mode: python ; coding: utf-8 -*-
# Windows 版 PyInstaller spec（保留 Mac 版未变，便于双平台维护）

import sys
import os

block_cipher = None

# 图标：有 .ico 就用，没有就用默认
icon_path = None
if os.path.exists('appicon.ico'):
    icon_path = 'appicon.ico'

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.yaml', '.'),
        ('bin/ffmpeg.exe', 'bin'),
        ('personas.json', '.'),
        ('material_library.json', '.'),
        ('action_prompts.json', '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 体积优化：砍掉 Python 不会用到的大模块
        'tkinter.test',
        'unittest',
        'pydoc',
        'doctest',
        'lib2to3',
        'pygame.tests',
        'PIL.ImageQt',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'gtk',
    ],
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
    upx_exclude=['ffmpeg.exe'],  # 别压坏外部二进制
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['ffmpeg.exe'],
    name='可乐口播视频生成器_win',
)
