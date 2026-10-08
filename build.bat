@echo off
rem 打包脚本：生成 dist\微信群言论监控助手\ 目录版 exe
cd /d %~dp0
"C:\Users\chenl\.workbuddy\binaries\python\envs\default\Scripts\pyinstaller.exe" ^
  --noconfirm ^
  --noconsole ^
  --clean ^
  --name "WeChatMonitor" ^
  --collect-all rapidocr_onnxruntime ^
  --hidden-import PySide6.QtSvg ^
  main.py
echo.
echo 打包完成: dist\WeChatMonitor\WeChatMonitor.exe
pause
