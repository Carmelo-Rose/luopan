"""
启动入口（解决 Windows 下 module path 问题）。
直接 python run.py [args] 即可。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main

if __name__ == "__main__":
    main()
