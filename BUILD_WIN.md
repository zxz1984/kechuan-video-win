# 可乐口播视频生成器 - Windows 打包指南

## 用户拿到的文件

打包完后，Windows 用户下载这一个文件即可：

```
可乐口播视频生成器_Setup_v2.10.56.exe  (~80-150 MB)
```

**双击 → 下一步 → 安装到 `C:\Program Files\可乐口播视频生成器\` → 桌面有快捷方式**

## 两种触发方式

### 方式 A：手动触发（调试用）

```
1. 打开 GitHub Actions 页面：https://github.com/zxz1984/kechuan-video-win/actions
2. 左侧点 "打包 Windows 安装包"
3. 右边 "Run workflow" 按钮 → 选 main 分支 → 点绿色 Run workflow
4. 等 10-15 分钟 → 完成后页面底部 "Artifacts" 下载
```

### 方式 B：打 tag 自动触发（发布用）

```bash
# 在 Mac 上
git tag v2.10.56
git push --tags
```

GitHub Actions 自动跑完 → **Releases 页面** 自动生成安装包下载链接。

## 打包流程（GitHub Actions 内部）

```
Step 1: 拉代码
Step 2: 装 Python 3.12
Step 3: 装 Inno Setup (choco)
Step 4: 装 ffmpeg (choco)
Step 5: 拷贝 ffmpeg.exe 到 bin/
Step 6: 装 PyInstaller + 项目依赖
Step 7: PyInstaller 用 _win.spec 打包成文件夹
Step 8: Inno Setup 把文件夹打成 setup.exe
Step 9: 上传到 Release 或 Artifacts
```

## 手动打包（出问题时的备用方案）

### 在 Windows 上用现成脚本

```
1. 装 Python 3.12：https://www.python.org/downloads/
2. 双击项目里的 build_win.bat
3. 等 5 分钟 → dist\可乐口播视频生成器_win\ 可双击运行
```

### 用 Inno Setup 单独做安装包

```
1. 装 Inno Setup：https://jrsoftware.org/isdl.php
2. 跑完 PyInstaller 后
3. 右键 scripts\build_installer.iss → Compile
4. 出来的 setup.exe 在 dist\
```

## 文件清单

```
.github/workflows/build-win.yml     # GitHub Actions 配置（云端自动打包）
scripts/build_installer.iss          # Inno Setup 安装包脚本
可乐口播视频生成器_win.spec          # PyInstaller Windows spec（已有）
build_win.bat                        # 一键打包脚本（已有，给 Windows 用户用）
```

## 故障排查

| 症状 | 排查 |
|------|------|
| Actions 跑失败 | 看 Actions 页面日志，看是哪一步错了 |
| 双击 .exe 闪退 | 检查是否装了 VC++ 运行库（Windows 自带，大多数情况 OK）|
| 找不到 ffmpeg | 看 `bin/ffmpeg.exe` 是否在安装目录里 |
| 中文乱码 | Windows 区域设置改成"中文（简体，中国）"|

## 版本号升级流程

1. 改 `可乐口播视频生成器_win.spec`（如果有变化）
2. 改 `scripts/build_installer.iss` 第 6 行 `#define MyAppVersion`
3. 改 `main.py` 里 `APP_VERSION = "x.xx.xx"`
4. git commit + git tag v2.10.57 + git push --tags
