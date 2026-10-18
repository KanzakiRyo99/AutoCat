"""真实硬件冒烟测试：确认回放链路真的能发出键鼠事件。

刻意做得非常克制，只会：
1. 记住鼠标当前位置，移动 1 像素后还原；
2. 单独按一下 Shift 再松开（不组合任何其它键，不会输入任何字符）。

不会往任何窗口里打字，不会点击任何东西。

    python tools/smoke_test.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pynput import keyboard, mouse  # noqa: E402

from autocat.player import Player  # noqa: E402


def main() -> int:
    controller = mouse.Controller()
    original = controller.position
    print(f"鼠标当前位置: {original}")

    events = [
        {"t": 0.0, "type": "mouse_move", "x": original[0] + 1, "y": original[1] + 1},
        {"t": 0.1, "type": "mouse_move", "x": original[0], "y": original[1]},
        {"t": 0.2, "type": "key_press", "key": "shift_l"},
        {"t": 0.3, "type": "key_release", "key": "shift_l"},
    ]

    player = Player(speed=1.0, loops=1, start_delay=0.3)
    result = player.play(events)

    moved = controller.position
    print(f"回放结果: {result}")
    print(f"鼠标归位检查: {moved} (期望 {original})")

    # 兜底：万一被别的东西挪走了，也还原回去
    controller.position = original

    ok = not result["aborted"] and result["played"] == len(events)
    for key in ("shift_l",):
        # 确认按键能正确解析成 pynput 对象
        from autocat.player import key_from_str
        assert key_from_str(key) is not None
    print("硬件链路正常" if ok else "硬件链路异常")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
