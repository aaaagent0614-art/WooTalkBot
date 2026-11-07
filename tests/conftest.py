"""讓 tests/ 能 import repo 根目錄的 wootalk_app。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
