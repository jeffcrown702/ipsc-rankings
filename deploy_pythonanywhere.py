#!/usr/bin/env python
"""
IPSC Rankings - PythonAnywhere Deploy Guide
===========================================
Step-by-step instructions for deploying on PythonAnywhere (free, no credit card)
"""

GUIDE = """
═══════════════════════════════════════════════════
  IPSC 實時排名系統 — PythonAnywhere 部署指南
═══════════════════════════════════════════════════

Step 1: 註冊 PythonAnywhere
━━━━━━━━━━━━━━━━━━━━━━━━━
  1. 去 https://www.pythonanywhere.com
  2. Sign up → 填 Email + Username + Password
  3. ✅ 不需信用卡

Step 2: 打開 Bash Console
━━━━━━━━━━━━━━━━━━━━━━━━━
  1. Login 後，Click Dashboard → "Bash" console
  2. 會見到一個終端機

Step 3: Clone GitHub Repo
━━━━━━━━━━━━━━━━━━━━━━━━━━
  喺 Bash 入面逐條打：

  git clone https://github.com/jeffcrown702/ipsc-rankings.git
  cd ipsc-rankings

Step 4: 建立 Virtualenv
━━━━━━━━━━━━━━━━━━━━━━━━━
  mkvirtualenv --python=python3.11 ipsc-venv
  pip install -r requirements.txt

Step 5: 設定 Web App
━━━━━━━━━━━━━━━━━━━━━
  1. Click 右上角 username → "Web"
  2. Click "Add a new web app"
  3. Next → 揀 "Manual configuration"
  4. Python version: 3.11
  5. Next

Step 6: 設定 WSGI file
━━━━━━━━━━━━━━━━━━━━━━━
  1. 喺 Web 頁面，搵 "Code" section
  2. Click "WSGI configuration file" 嘅 link
  3. Delete 所有內容，貼上以下：

================================================================
import sys, os
path = '/home/YOUR_USERNAME/ipsc-rankings'
if path not in sys.path:
    sys.path.append(path)
os.chdir(path)
from wsgi import application
================================================================

  4. Replace YOUR_USERNAME 做你嘅 PythonAnywhere username
  5. Save

Step 7: 設定 Static Files (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  URL: /static/
  Directory: /home/YOUR_USERNAME/ipsc-rankings/static/

Step 8: Reload
━━━━━━━━━━━━━━━
  1. 返去 Web 頁面
  2. Click 綠色 "Reload" 掣

Step 9: Open Website
━━━━━━━━━━━━━━━━━━━
  https://YOUR_USERNAME.pythonanywhere.com

═══════════════════════════════════════════════════
  完成！
═══════════════════════════════════════════════════
"""

if __name__ == "__main__":
    print(GUIDE)
