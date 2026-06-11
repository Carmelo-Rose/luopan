"""pytest 根配置：将项目根目录加入 sys.path。"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
