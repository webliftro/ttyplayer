import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs macOS or Linux")


class FakeOverlapped:
    """_winapi's Overlapped for FakeWinapi: a connect that completes once mpv opens the pipe, or a read."""

    def __init__(self, winapi, handle, data=None):
        self.winapi, self.handle, self.data = winapi, handle, data
        self.event = ("event", handle)
        self.cancelled = False

    def GetOverlappedResult(self, wait):
        assert wait
        if self.cancelled:  # Windows is done with the cancelled operation only now
            self.winapi.log.append(("cancelled", self.handle))
            self.winapi.pending.discard(self.handle)
            return 0, self.winapi.ERROR_OPERATION_ABORTED
        return len(self.data or b""), 0

    def getbuffer(self):
        return memoryview(self.data)

    def cancel(self):
        self.winapi.log.append(("cancel", self.handle))
        self.winapi.pending.add(self.handle)
        self.cancelled = True


class FakeWinapi:
    """Stands in for _winapi: one pipe mpv opens (connected), writes to (written) and closes (gone)."""

    PIPE_ACCESS_INBOUND, FILE_FLAG_FIRST_PIPE_INSTANCE, FILE_FLAG_OVERLAPPED = 0x1, 0x80000, 0x40000000
    PIPE_WAIT = NULL = WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    ERROR_OPERATION_ABORTED = 995

    def __init__(self):
        self.log = []
        self.handles = 0
        self.connected = self.gone = False
        self.written = bytearray()
        self.taken = False  # another process created the name first
        self.pending = set()  # handles with a connect cancelled but not yet waited for

    def CreateNamedPipe(self, name, open_mode, pipe_mode, max_instances, out_size, in_size, timeout, security):
        if self.taken:
            raise PermissionError(13, "Access is denied")
        self.handles += 1
        self.connected = self.gone = False
        self.log.append(("create", f"pipe{self.handles}"))
        self.made = (name, open_mode, pipe_mode, max_instances, out_size, in_size, timeout, security)
        return f"pipe{self.handles}"

    def ConnectNamedPipe(self, handle, overlapped=False):
        assert overlapped  # a blocking connect would hang until mpv plays something
        self.log.append(("connect", handle))
        return FakeOverlapped(self, handle)

    def WaitForSingleObject(self, event, milliseconds):
        assert milliseconds == 0
        return self.WAIT_OBJECT_0 if self.connected else self.WAIT_TIMEOUT

    def PeekNamedPipe(self, handle):
        if self.gone and not self.written:
            raise BrokenPipeError(32, "The pipe has been ended")
        return len(self.written), 0

    def ReadFile(self, handle, size, overlapped=False):
        assert overlapped  # the pipe was created overlapped
        if self.gone and not self.written:
            raise BrokenPipeError(32, "The pipe has been ended")
        data = bytes(self.written[:size])
        del self.written[:size]
        return FakeOverlapped(self, handle, data), 0

    def CloseHandle(self, handle):
        assert handle not in self.pending, "closed while Windows may still use the cancelled connect"
        self.log.append(("close", handle))


@pytest.fixture
def fake_winapi(monkeypatch):
    winapi = FakeWinapi()
    monkeypatch.setitem(sys.modules, "_winapi", winapi)
    return winapi
