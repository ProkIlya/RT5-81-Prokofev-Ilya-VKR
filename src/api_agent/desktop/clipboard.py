"""Windows clipboard для обычной консоли; другие терминалы используют OSC52."""
import ctypes
import os


def copy_windows(text):
    if os.name!='nt':return False
    user=ctypes.WinDLL('user32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    user.OpenClipboard.argtypes=[ctypes.c_void_p];user.OpenClipboard.restype=ctypes.c_int
    user.EmptyClipboard.restype=ctypes.c_int
    user.SetClipboardData.argtypes=[ctypes.c_uint,ctypes.c_void_p];user.SetClipboardData.restype=ctypes.c_void_p
    kernel.GlobalAlloc.argtypes=[ctypes.c_uint,ctypes.c_size_t];kernel.GlobalAlloc.restype=ctypes.c_void_p
    kernel.GlobalLock.argtypes=[ctypes.c_void_p];kernel.GlobalLock.restype=ctypes.c_void_p
    kernel.GlobalUnlock.argtypes=[ctypes.c_void_p]
    kernel.GlobalFree.argtypes=[ctypes.c_void_p];kernel.GlobalFree.restype=ctypes.c_void_p
    raw=(text+'\0').encode('utf-16-le');handle=kernel.GlobalAlloc(2,len(raw))
    if not handle:return False
    opened=False
    try:
        pointer=kernel.GlobalLock(handle)
        if not pointer:return False
        try:ctypes.memmove(pointer,raw,len(raw))
        finally:kernel.GlobalUnlock(handle)
        opened=bool(user.OpenClipboard(None))
        if not opened or not user.EmptyClipboard():return False
        if not user.SetClipboardData(13,handle):return False
        handle=None # Память теперь принадлежит Windows.
        return True
    finally:
        if opened:user.CloseClipboard()
        if handle:kernel.GlobalFree(handle)
