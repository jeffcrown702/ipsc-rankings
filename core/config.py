"""IPSC 實時排名系統 - 配置文件"""
import os

# 目標網站
BASE_URL = "https://hkg.as.ipscess.org/portal"

# Scraper 設定
SCRAPER_INTERVAL_MINUTES = 5  # 定時抓取間隔（分鐘）
MAX_COMPETITOR_NUMBER = 500   # 最大選手編號
EMPTY_STREAK_LIMIT = 5       # 連續空號幾多次就停止

# 可識別的 Category
CATEGORIES = ["Lady", "Junior", "S. Junior", "Senior", "S. Senior"]

# 可識別的 Class (級別)
CLASSES = ["G", "M", "A", "B", "C", "U"]

# 可識別的 Division (大組)
DIVISIONS = [
    "Open", "Standard", "Production", "Revolver",
    "Classic", "Production Optics", "Optics"
]

# 數據庫
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ipsc_rankings.db")

# FastAPI
API_HOST = "0.0.0.0"
API_PORT = 8010
