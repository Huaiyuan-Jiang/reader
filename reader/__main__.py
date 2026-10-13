"""python -m reader：打开书架。

`if __name__ == "__main__":` 不能去掉：PDF 出图的子进程（multiprocessing，spawn）会重新导入主模块，
没有这道门，子进程也会去开一个 reader。freeze_support() 给以后打包成 exe 用（没打包时什么都不做）。
"""

import multiprocessing
import sys

from reader.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
