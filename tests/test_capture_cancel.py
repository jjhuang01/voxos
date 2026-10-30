"""设备停止产帧时，取消不能等阻塞 read 永远返回。"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

from voxos import audio


def test_microphone_wait_can_be_cancelled_without_a_frame(monkeypatch):
    """等待帧时用 Event 唤醒；取消后释放设备，不调用阻塞 read。"""
    waiting = threading.Event()
    cancelled = threading.Event()
    ended = threading.Event()
    errors = []

    class Stream:
        blocksize = 512
        closed = False
        reads = 0

        @property
        def read_available(self):
            waiting.set()
            return 0

        def start(self):
            return None

        def read(self, size):
            self.reads += 1
            raise AssertionError("没有帧时不应调用阻塞read")

        def stop(self):
            return None

        def close(self):
            self.closed = True

    stream = Stream()
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kwargs: stream))

    def capture():
        try:
            with audio.MicSource(cancel=cancelled) as source:
                next(iter(source))
        except Exception as error:
            errors.append(error)
        finally:
            ended.set()

    worker = threading.Thread(target=capture, daemon=True)
    worker.start()
    try:
        assert waiting.wait(1), "未进入可取消等待"
        cancelled.set()
        assert ended.wait(0.5), "取消仍在等待设备帧"
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert stream.closed and stream.reads == 0
    finally:
        cancelled.set()
        worker.join(timeout=1)
