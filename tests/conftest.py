"""pytest 共用設定：把專案根目錄加入 sys.path，讓測試能匯入 endpoints 套件。"""
import os
import sys

# 專案根目錄（tests/ 的上一層）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
