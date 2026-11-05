"""`_行为增量验证.py` 的子进程：只做 N 次 `apply`，用来验跨进程文件锁。

为什么单独一个文件：跨进程锁必须真的两个进程才好验，而在测试里拼一段带引号的
子进程源码等于给自己埋转义坑（本文件第一版就踩过）。参数：

```
python _行为增量验证_子进程.py <root> <at> <axis> <step> <times>
```
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys


def main() -> int:
    if len(sys.argv) < 6:
        print("用法：_行为增量验证_子进程.py <root> <at> <axis> <step> <times>")
        return 2
    here = pathlib.Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("vec_verify", here / "_行为增量验证.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # 只跑模块级定义，不会进 __main__
    module._bootstrap()                      # noqa: SLF001 - 复用同一套离线桩

    from ai_chat import persona_vector as vec, settings
    from ai_chat.persona_workspace import use

    settings.store.load()
    settings.store._runtime["vector_max_step"] = 0.02    # noqa: SLF001
    settings.store._runtime["vector_daily_limit"] = 1.0  # noqa: SLF001

    root = pathlib.Path(sys.argv[1])
    at = float(sys.argv[2])
    axis, step, times = sys.argv[3], float(sys.argv[4]), int(sys.argv[5])
    busy = 0
    with use(root, at=at):
        for _ in range(times):
            got = vec.apply({axis: step})
            if got.get("why") == "busy":
                busy += 1
    print(f"applied {times - busy}/{times}")
    return 0 if busy == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
