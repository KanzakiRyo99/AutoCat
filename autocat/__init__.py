"""AutoCat 包。

Windows 键鼠自动化工具：录制 / 回放 / 定时执行。
"""

from .recorder import Recorder
from .player import Player, PlaybackAborted
from .scheduler import Scheduler, Task
from .storage import load_recording, save_recording

__version__ = "0.3.2"
__author__ = "KanzakiRyo99"
__license__ = "MIT"

__all__ = [
    "Recorder",
    "Player",
    "PlaybackAborted",
    "Scheduler",
    "Task",
    "load_recording",
    "save_recording",
    "__version__",
    "__author__",
    "__license__",
]
