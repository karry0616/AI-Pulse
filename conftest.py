"""pytest 全局配置：把项目根目录加入 sys.path，测试可直接 import 顶层模块。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
