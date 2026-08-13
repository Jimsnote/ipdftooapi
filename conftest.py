import os
import sys

# 让 `import app...` 在 pytest 从 apps/api 运行时可用。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
