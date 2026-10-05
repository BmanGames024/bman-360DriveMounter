
from __future__ import annotations

import bisect
import datetime
import errno
import os
import re
import string
import struct
import sys
import threading
import time
from array import array
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

SECTOR = 512
IS_WINDOWS = sys.platform == "win32"
MAX_FILE_SIZE = 0xFFFFFFFF
MAX_NAME = 42
VALID_NAME_CHARS = set(string.ascii_letters + string.digits + " !#$%&'()-.@[]^_`{}~")

XBOX360_LAYOUT = [
    ("System Cache",      0x000080000, 0x080000000),
    ("Game Cache",        0x080080000, 0x0A0E30000),
    ("System Extended",   0x10C080000, 0x00CE30000),
    ("System Extended 2", 0x118EB0000, 0x008000000),
    ("Compatibility",     0x120EB0000, 0x010000000),
    ("Content",           0x130EB0000, None),
]

USB_FOLDER = "Xbox360"
USB_DATA_OFFSET = 0x20000000


class FatxError(Exception):
    pass


def _err(code: int, msg: str = "") -> OSError:
    return OSError(code, msg or os.strerror(code))


if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _DeviceIoControl = _k32.DeviceIoControl
    _DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    _DeviceIoControl.restype = wintypes.BOOL

    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

    def _ioctl(fobj, code: int, inbuf: bytes = b"", outsize: int = 1024) -> bytes:
        handle = msvcrt.get_osfhandle(fobj.fileno())
        out = ctypes.create_string_buffer(outsize)
        returned = wintypes.DWORD()
        inb = ctypes.create_string_buffer(inbuf, len(inbuf)) if inbuf else None
        ok = _DeviceIoControl(handle, code, inb, len(inbuf), out, outsize,
                              ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return out.raw[:returned.value]

    def _win_disk_size(fobj) -> int:
        return struct.unpack("<q", _ioctl(fobj, IOCTL_DISK_GET_LENGTH_INFO, outsize=8)[:8])[0]

    def _win_disk_model(fobj) -> str:
        try:
            query = struct.pack("<II4x", 0, 0)
            out = _ioctl(fobj, IOCTL_STORAGE_QUERY_PROPERTY, query, 1024)
            vendor_off, product_off = struct.unpack_from("<II", out, 12)

            def cstr(off):
                if not off or off >= len(out):
                    return ""
                return out[off:out.index(b"\0", off)].decode("ascii", "ignore").strip()

            return " ".join(s for s in (cstr(vendor_off), cstr(product_off)) if s)
        except OSError:
            return ""


class BlockDevice:
    BLOCK = 64 * 1024
    DIRECT_THRESHOLD = 256 * 1024

    def __init__(self, path: str, writable: bool = False, cache_blocks: int = 512):
        self.path = path
        self.writable = writable
        self._f = open(path, "r+b" if writable else "rb", buffering=0)
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, bytearray] = OrderedDict()
        self._cache_max = cache_blocks
        self.model = ""
        if IS_WINDOWS and path.startswith("\\\\.\\"):
            self.size = _win_disk_size(self._f)
            self.model = _win_disk_model(self._f)
        else:
            self._f.seek(0, os.SEEK_END)
            self.size = self._f.tell()

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass

    def fsync(self):
        if self.writable:
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass

    def _raw_read(self, start: int, length: int) -> bytes:
        self._f.seek(start)
        chunks, got = [], 0
        while got < length:
            data = self._f.read(length - got)
            if not data:
                break
            chunks.append(data)
            got += len(data)
        return b"".join(chunks)

    def _raw_write(self, start: int, data) -> None:
        self._f.seek(start)
        view = memoryview(data)
        while len(view):
            n = self._f.write(view)
            if not n:
                raise _err(errno.EIO, "Disk write failed")
            view = view[n:]

    def _block(self, idx: int) -> bytearray:
        blk = self._cache.get(idx)
        if blk is not None:
            self._cache.move_to_end(idx)
            return blk
        start = idx * self.BLOCK
        length = min(self.BLOCK, self.size - start)
        blk = bytearray(self._raw_read(start, length) if length > 0 else b"")
        blk.extend(b"\0" * (self.BLOCK - len(blk)))
        self._cache[idx] = blk
        if len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        return blk

    def _read_locked(self, offset: int, length: int) -> bytes:
        if length >= self.DIRECT_THRESHOLD:
            a_start = offset - offset % SECTOR
            a_end = min(-(-(offset + length) // SECTOR) * SECTOR, self.size)
            data = self._raw_read(a_start, max(a_end - a_start, 0))
            data = data[offset - a_start: offset - a_start + length]
            return data.ljust(length, b"\0")
        out = bytearray()
        end = offset + length
        while offset < end:
            idx, inner = divmod(offset, self.BLOCK)
            take = min(self.BLOCK - inner, end - offset)
            out += self._block(idx)[inner:inner + take]
            offset += take
        return bytes(out)

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        with self._lock:
            return self._read_locked(offset, length)

    def write(self, offset: int, data) -> None:
        if not self.writable:
            raise _err(errno.EROFS)
        if not data:
            return
        end = offset + len(data)
        if offset < 0 or end > self.size:
            raise _err(errno.EIO, "Write outside the disk")
        with self._lock:
            a_start = offset - offset % SECTOR
            a_end = -(-end // SECTOR) * SECTOR
            if a_start == offset and a_end == end:
                buf = data
            else:
                buf = bytearray(a_end - a_start)
                buf[:SECTOR] = self._read_locked(a_start, SECTOR)
                if a_end - SECTOR > a_start:
                    buf[-SECTOR:] = self._read_locked(a_end - SECTOR, SECTOR)
                buf[offset - a_start: offset - a_start + len(data)] = data
            self._raw_write(a_start, buf)
            for idx in range(a_start // self.BLOCK, (a_end - 1) // self.BLOCK + 1):
                blk = self._cache.get(idx)
                if blk is None:
                    continue
                b0 = idx * self.BLOCK
                s, e = max(a_start, b0), min(a_end, b0 + self.BLOCK)
                blk[s - b0:e - b0] = buf[s - a_start:e - a_start]


def usb_data_files(folder: str) -> list[str]:
    found: dict[int, str] = {}
    for name in os.listdir(folder):
        m = re.match(r"^Data(\d{4})$", name, re.IGNORECASE)
        path = os.path.join(folder, name)
        if m and os.path.isfile(path):
            found[int(m.group(1))] = path
    if 0 not in found:
        raise FatxError(f"No Data0000 file in {folder}")
    for i in range(max(found) + 1):
        if i not in found:
            raise FatxError(f"Data{i:04d} is missing from {folder}")
    return [found[i] for i in range(len(found))]


class SplitFileDevice(BlockDevice):
    def __init__(self, folder: str, writable: bool = False, cache_blocks: int = 512):
        self.path = folder
        self.writable = writable
        self.model = "USB storage"
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._cache_max = cache_blocks
        self._files: list = []
        self._starts: list[int] = []
        self._lengths: list[int] = []
        pos = 0
        try:
            for path in usb_data_files(folder):
                f = open(path, "r+b" if writable else "rb", buffering=0)
                self._files.append(f)
                length = f.seek(0, os.SEEK_END)
                self._starts.append(pos)
                self._lengths.append(length)
                pos += length
        except OSError:
            self.close()
            raise
        self.size = pos

    def close(self):
        for f in self._files:
            try:
                f.close()
            except OSError:
                pass

    def fsync(self):
        if self.writable:
            for f in self._files:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

    def _spans(self, start: int, length: int):
        i = max(bisect.bisect_right(self._starts, start) - 1, 0)
        while length > 0 and i < len(self._files):
            inner = start - self._starts[i]
            take = min(self._lengths[i] - inner, length)
            if take > 0:
                yield self._files[i], inner, take
                start += take
                length -= take
            i += 1

    def _raw_read(self, start: int, length: int) -> bytes:
        chunks = []
        for f, inner, take in self._spans(start, length):
            f.seek(inner)
            got = 0
            while got < take:
                data = f.read(take - got)
                if not data:
                    break
                chunks.append(data)
                got += len(data)
        return b"".join(chunks)

    def _raw_write(self, start: int, data) -> None:
        view = memoryview(data)
        for f, inner, take in self._spans(start, len(view)):
            f.seek(inner)
            part, view = view[:take], view[take:]
            while len(part):
                n = f.write(part)
                if not n:
                    raise _err(errno.EIO, "Disk write failed")
                part = part[n:]
        if len(view):
            raise _err(errno.EIO, "Write outside the disk")


ATTR_DIRECTORY = 0x10


def fatx_decode_time(v: int) -> float:
    if v in (0, 0xFFFFFFFF):
        return 0.0
    try:
        return datetime.datetime(
            ((v >> 25) & 0x7F) + 1980, (v >> 21) & 0x0F, (v >> 16) & 0x1F,
            (v >> 11) & 0x1F, (v >> 5) & 0x3F, min((v & 0x1F) * 2, 59),
        ).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def fatx_encode_time(ts: float | None = None) -> int:
    try:
        t = datetime.datetime.fromtimestamp(time.time() if ts is None else ts)
    except (ValueError, OSError, OverflowError):
        return 0
    if t.year < 1980:
        return 0
    return (min(t.year - 1980, 127) << 25) | (t.month << 21) | (t.day << 16) | \
           (t.hour << 11) | (t.minute << 5) | (t.second // 2)


fatx_time = fatx_decode_time


@dataclass(eq=False)
class DirEntry:
    name: str
    attributes: int
    first_cluster: int
    size: int
    ctime_raw: int = 0
    mtime_raw: int = 0
    atime_raw: int = 0
    record: int = -1
    parent: int = 0
    valid: int | None = None

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & ATTR_DIRECTORY)

    @property
    def created(self) -> float:
        return fatx_decode_time(self.ctime_raw)

    @property
    def modified(self) -> float:
        return fatx_decode_time(self.mtime_raw)

    @property
    def accessed(self) -> float:
        return fatx_decode_time(self.atime_raw)


def _norm(path: str) -> str:
    return "/" + "/".join(p for p in path.replace("\\", "/").split("/") if p)


def _split(path: str) -> tuple[str, str]:
    norm = _norm(path)
    parent, _, name = norm.rpartition("/")
    return parent or "/", name


def check_name(name: str) -> None:
    if not name or name in (".", ".."):
        raise _err(errno.EINVAL, "Invalid name")
    if len(name) > MAX_NAME:
        raise _err(errno.ENAMETOOLONG, f"Xbox names can be at most {MAX_NAME} characters")
    if any(ch not in VALID_NAME_CHARS for ch in name):
        raise _err(errno.EINVAL, f"Name contains characters the Xbox can't store: {name!r}")


class FatxVolume:
    FAT_CHUNK = 256 * 1024

    def __init__(self, dev: BlockDevice, offset: int, size: int | None = None, name: str = "FATX",
                 usb: bool = False):
        self.dev = dev
        self.offset = offset
        self.size = size if size else dev.size - offset
        self.name = name
        self.writable = dev.writable

        hdr = dev.read(offset, 0x1000)
        magic = hdr[:4]
        if magic == b"XTAF":
            self._e = ">"
        elif magic == b"FATX":
            self._e = "<"
        else:
            raise FatxError(f"No FATX header at offset 0x{offset:X}")

        self.volume_id, spc, self.root_cluster = struct.unpack_from(self._e + "III", hdr, 4)
        if spc == 0 or spc & (spc - 1) or spc > 1024:
            raise FatxError(f"Invalid sectors-per-cluster value {spc}")
        self.root_cluster = self.root_cluster or 1

        self.cluster_size = spc * SECTOR
        self.cluster_count = self.size // self.cluster_size
        self.fat_entry_size = 2 if self.cluster_count < 0xFFF0 else 4
        fat_bytes = (self.cluster_count * self.fat_entry_size + 0xFFF) & ~0xFFF
        if usb:
            self.fat_entry_size = 4
            fat_bytes = ((self.cluster_count + 1) * 4 + 0xFFF) & ~0xFFF
        self.fat_offset = offset + 0x1000
        self.data_offset = self.fat_offset + fat_bytes
        self.max_cluster = min((offset + self.size - self.data_offset) // self.cluster_size,
                               self.cluster_count - 1)
        if self.max_cluster < 1 or self.root_cluster > self.max_cluster:
            raise FatxError("Partition too small or corrupt header")

        self._fat_fmt = self._e + ("H" if self.fat_entry_size == 2 else "I")
        self._fat_tc = "H" if self.fat_entry_size == 2 else "I"
        self._eoc = 0xFFF0 if self.fat_entry_size == 2 else 0xFFFFFFF0
        self._eoc_value = 0xFFFF if self.fat_entry_size == 2 else 0xFFFFFFFF
        self._per_chunk = self.FAT_CHUNK // self.fat_entry_size

        self._lock = threading.RLock()
        self._fat_chunks: OrderedDict[int, bytearray] = OrderedDict()
        self._fat_dirty: set[tuple[int, int]] = set()
        self._dir_cache: dict[int, list[DirEntry]] = {}
        self._path_cache: dict[str, DirEntry | None] = {}
        self._chain_cache: OrderedDict[int, list[int]] = OrderedDict()
        self._pending: set[DirEntry] = set()
        self._alloc_hint = 1
        self.free_clusters: int | None = None
        self.root = DirEntry("", ATTR_DIRECTORY, self.root_cluster, 0)

    @property
    def lock(self):
        return self._lock

    def _fat_chunk(self, ci: int) -> bytearray:
        chunk = self._fat_chunks.get(ci)
        if chunk is not None:
            self._fat_chunks.move_to_end(ci)
            return chunk
        if len(self._fat_chunks) >= 256:
            if self._fat_dirty:
                self._commit_fat()
            self._fat_chunks.popitem(last=False)
        chunk = bytearray(self.dev.read(self.fat_offset + ci * self.FAT_CHUNK, self.FAT_CHUNK))
        self._fat_chunks[ci] = chunk
        return chunk

    def _fat_get(self, cluster: int) -> int:
        ci, inner = divmod(cluster * self.fat_entry_size, self.FAT_CHUNK)
        return struct.unpack_from(self._fat_fmt, self._fat_chunk(ci), inner)[0]

    def _fat_set(self, cluster: int, value: int) -> None:
        if not 1 <= cluster <= self.max_cluster:
            raise _err(errno.EIO, f"Refusing to write FAT entry for cluster {cluster}")
        ci, inner = divmod(cluster * self.fat_entry_size, self.FAT_CHUNK)
        struct.pack_into(self._fat_fmt, self._fat_chunk(ci), inner, value)
        self._fat_dirty.add((ci, inner // SECTOR))

    def _commit_fat(self) -> None:
        if not self._fat_dirty:
            return
        by_chunk: dict[int, list[int]] = {}
        for ci, s in self._fat_dirty:
            by_chunk.setdefault(ci, []).append(s)
        for ci, sectors in by_chunk.items():
            chunk = self._fat_chunks[ci]
            sectors.sort()
            start = prev = sectors[0]
            for s in sectors[1:] + [None]:
                if s is not None and s == prev + 1:
                    prev = s
                    continue
                self.dev.write(self.fat_offset + ci * self.FAT_CHUNK + start * SECTOR,
                               chunk[start * SECTOR:(prev + 1) * SECTOR])
                if s is not None:
                    start = prev = s
        self._fat_dirty.clear()

    def _chain(self, first: int) -> list[int]:
        """Full cluster chain starting at `first` (cached; callers may extend it in place)."""
        if first == 0:
            return []
        cached = self._chain_cache.get(first)
        if cached is not None:
            self._chain_cache.move_to_end(first)
            return cached
        clusters: list[int] = []
        if 1 <= first <= self.max_cluster:
            c = first
            while True:
                clusters.append(c)
                if len(clusters) > self.max_cluster:
                    raise FatxError("Cluster chain loop detected")
                n = self._fat_get(c)
                if n == 0 or n >= self._eoc or n > self.max_cluster:
                    break
                c = n
        self._chain_cache[first] = clusters
        if len(self._chain_cache) > 128:
            self._chain_cache.popitem(last=False)
        return clusters

    def chain(self, first: int, limit: int | None = None) -> list[int]:
        with self._lock:
            c = self._chain(first)
            return list(c if limit is None else c[:limit])

    def _alloc(self, n: int) -> list[int]:
        """Allocate n free clusters, linked together and terminated."""
        found: list[int] = []
        c, wrapped = self._alloc_hint, False
        try:
            while len(found) < n:
                if c > self.max_cluster:
                    if wrapped:
                        raise _err(errno.ENOSPC, "The Xbox drive is full")
                    wrapped, c = True, 1
                    continue
                ci = c // self._per_chunk
                base = ci * self._per_chunk
                arr = array(self._fat_tc, self._fat_chunk(ci))
                lo, hi = c - base, min(self._per_chunk, self.max_cluster - base + 1)
                while len(found) < n:
                    try:
                        i = arr.index(0, lo, hi)
                    except ValueError:
                        lo = hi
                        break
                    found.append(base + i)
                    self._fat_set(base + i, self._eoc_value)
                    lo = i + 1
                c = base + lo
        except OSError:
            for cl in found:
                self._fat_set(cl, 0)
            raise
        for a, b in zip(found, found[1:]):
            self._fat_set(a, b)
        self._alloc_hint = found[-1] + 1 if found else self._alloc_hint
        if self.free_clusters is not None:
            self.free_clusters -= len(found)
        return found

    def _free_clusters(self, clusters: list[int]) -> None:
        for c in clusters:
            self._fat_set(c, 0)
        if self.free_clusters is not None:
            self.free_clusters += len(clusters)
        if clusters:
            self._alloc_hint = min(self._alloc_hint, min(clusters))

    def _free_chain(self, first: int) -> None:
        if first:
            self._free_clusters(list(self._chain(first)))
            self._chain_cache.pop(first, None)

    def cluster_offset(self, cluster: int) -> int:
        return self.data_offset + (cluster - 1) * self.cluster_size

    def _parse_dir(self, cluster: int) -> list[DirEntry]:
        entries: list[DirEntry] = []
        e = self._e
        for c in self._chain(cluster):
            base = self.cluster_offset(c)
            data = self.dev.read(base, self.cluster_size)
            for off in range(0, self.cluster_size, 64):
                nl = data[off]
                if nl in (0x00, 0xFF):
                    return entries
                if nl == 0xE5 or nl > MAX_NAME:
                    continue
                name = data[off + 2: off + 2 + nl].decode("latin-1")
                first, size, ct, mt, at = struct.unpack_from(e + "IIIII", data, off + 0x2C)
                entries.append(DirEntry(name, data[off + 1], first, size, ct, mt, at,
                                        record=base + off, parent=cluster))
        return entries

    def _list(self, cluster: int) -> list[DirEntry]:
        cached = self._dir_cache.get(cluster)
        if cached is None:
            cached = self._dir_cache[cluster] = self._parse_dir(cluster)
        return cached

    def list_dir(self, cluster: int) -> list[DirEntry]:
        with self._lock:
            return list(self._list(cluster))

    def _find_child(self, parent: DirEntry, name: str) -> DirEntry | None:
        low = name.lower()
        return next((x for x in self._list(parent.first_cluster) if x.name.lower() == low), None)

    def resolve(self, path: str) -> DirEntry | None:
        norm = _norm(path)
        key = norm.lower()
        with self._lock:
            if key in self._path_cache:
                return self._path_cache[key]
            entry: DirEntry | None = self.root
            for part in [p for p in norm.split("/") if p]:
                if entry is None or not entry.is_dir:
                    entry = None
                    break
                entry = self._find_child(entry, part)
            self._path_cache[key] = entry
            return entry

    def _get(self, path: str) -> DirEntry:
        e = self.resolve(path)
        if e is None:
            raise _err(errno.ENOENT)
        return e

    def _write_record(self, entry: DirEntry) -> None:
        if entry.record < 0:
            return
        n = entry.name.encode("ascii")
        rec = bytes([len(n), entry.attributes]) + n.ljust(MAX_NAME, b"\xff") + struct.pack(
            self._e + "IIIII", entry.first_cluster, entry.size,
            entry.ctime_raw, entry.mtime_raw, entry.atime_raw)
        self.dev.write(entry.record, rec)

    def _add_entry(self, parent: DirEntry, entry: DirEntry) -> None:
        """Place `entry` into a free slot of `parent` and write it."""
        chain = self._chain(parent.first_cluster)
        slots = [self.cluster_offset(c) + off for c in chain for off in range(0, self.cluster_size, 64)]
        target = None
        for i, rec in enumerate(slots):
            b = self.dev.read(rec, 1)[0]
            if b == 0xE5:
                target = rec
                break
            if b in (0x00, 0xFF):
                target = rec
                if i + 1 < len(slots) and self.dev.read(slots[i + 1], 1)[0] not in (0x00, 0xFF):
                    self.dev.write(slots[i + 1], b"\xff")
                break
        if target is None:
            new = self._alloc(1)[0]
            self.dev.write(self.cluster_offset(new), b"\xff" * self.cluster_size)
            self._fat_set(chain[-1], new)
            chain.append(new)
            target = self.cluster_offset(new)
        entry.record = target
        entry.parent = parent.first_cluster
        self._write_record(entry)
        self._list(parent.first_cluster).append(entry)
        self._path_cache.clear()

    def _drop_record(self, entry: DirEntry, record: int, parent_cluster: int) -> None:
        self.dev.write(record, b"\xe5")
        lst = self._dir_cache.get(parent_cluster)
        if lst is not None:
            lst[:] = [x for x in lst if x is not entry]
        self._path_cache.clear()

    def _delete(self, entry: DirEntry) -> None:
        self._free_chain(entry.first_cluster)
        self._pending.discard(entry)
        if entry.is_dir:
            self._dir_cache.pop(entry.first_cluster, None)
        self._drop_record(entry, entry.record, entry.parent)

    def _io(self, entry: DirEntry, offset: int, length: int, data=None):
        """Read (data None) or write `data` at offset using the file's cluster chain."""
        chain = self._chain(entry.first_cluster)
        cs = self.cluster_size
        out = bytearray() if data is None else None
        pos, end = offset, offset + length
        while pos < end:
            idx, inner = divmod(pos, cs)
            if idx >= len(chain):
                if data is not None:
                    raise _err(errno.EIO, "Cluster chain shorter than file")
                break
            j = idx
            while j + 1 < len(chain) and chain[j + 1] == chain[j] + 1 and (j + 1) * cs < end:
                j += 1
            take = min((j + 1) * cs - pos, end - pos)
            disk = self.cluster_offset(chain[idx]) + inner
            if data is None:
                out += self.dev.read(disk, take)
            else:
                self.dev.write(disk, data[pos - offset: pos - offset + take])
            pos += take
        if out is not None:
            return bytes(out).ljust(length, b"\0")

    def _zero(self, entry: DirEntry, start: int, end: int) -> None:
        step = 1024 * 1024
        zeros = bytes(step)
        pos = start
        while pos < end:
            n = min(step, end - pos)
            self._io(entry, pos, n, zeros[:n])
            pos += n

    def _ensure_clusters(self, entry: DirEntry, nbytes: int) -> None:
        need = -(-nbytes // self.cluster_size)
        chain = self._chain(entry.first_cluster)
        if len(chain) >= need:
            return
        new = self._alloc(need - len(chain))
        if chain:
            self._fat_set(chain[-1], new[0])
            chain.extend(new)
        else:
            entry.first_cluster = new[0]
            self._chain_cache[new[0]] = list(new)

    def _shrink(self, entry: DirEntry, nbytes: int) -> None:
        need = -(-nbytes // self.cluster_size)
        chain = self._chain(entry.first_cluster)
        if len(chain) <= need:
            return
        if need == 0:
            self._free_chain(entry.first_cluster)
            entry.first_cluster = 0
            return
        extra = chain[need:]
        self._fat_set(chain[need - 1], self._eoc_value)
        del chain[need:]
        self._free_clusters(extra)

    def read_file(self, entry: DirEntry, offset: int, length: int) -> bytes:
        with self._lock:
            if offset >= entry.size or length <= 0:
                return b""
            length = min(length, entry.size - offset)
            valid = entry.size if entry.valid is None else entry.valid
            real = max(0, min(offset + length, valid) - offset)
            data = self._io(entry, offset, real) if real else b""
            return data.ljust(length, b"\0")

    @contextmanager
    def _mutate(self):
        if not self.writable:
            raise _err(errno.EROFS)
        with self._lock:
            try:
                yield
            finally:
                self._commit_fat()

    def _settle_valid(self, entry: DirEntry) -> None:
        if entry.valid is not None and entry.valid >= entry.size:
            entry.valid = None
            self._pending.discard(entry)

    def create(self, path: str, directory: bool = False) -> DirEntry:
        with self._mutate():
            ppath, name = _split(path)
            check_name(name)
            parent = self._get(ppath)
            if not parent.is_dir:
                raise _err(errno.ENOTDIR)
            if self._find_child(parent, name):
                raise _err(errno.EEXIST)
            now = fatx_encode_time()
            first = 0
            if directory:
                first = self._alloc(1)[0]
                self.dev.write(self.cluster_offset(first), b"\xff" * self.cluster_size)
            entry = DirEntry(name, ATTR_DIRECTORY if directory else 0, first, 0, now, now, now)
            try:
                self._add_entry(parent, entry)
            except OSError:
                if first:
                    self._free_chain(first)
                raise
            if directory:
                self._dir_cache[first] = []
            return entry

    def mkdir(self, path: str) -> DirEntry:
        return self.create(path, directory=True)

    def unlink(self, path: str) -> None:
        with self._mutate():
            e = self._get(path)
            if e.is_dir:
                raise _err(errno.EISDIR)
            self._delete(e)

    def rmdir(self, path: str) -> None:
        with self._mutate():
            e = self._get(path)
            if e is self.root:
                raise _err(errno.EBUSY)
            if not e.is_dir:
                raise _err(errno.ENOTDIR)
            if self._list(e.first_cluster):
                raise _err(errno.ENOTEMPTY)
            self._delete(e)

    def rename(self, old: str, new: str) -> None:
        with self._mutate():
            src = self._get(old)
            if src is self.root:
                raise _err(errno.EBUSY)
            ppath, name = _split(new)
            check_name(name)
            nparent = self._get(ppath)
            if not nparent.is_dir:
                raise _err(errno.ENOTDIR)
            if src.is_dir and (_norm(new).lower() + "/").startswith(_norm(old).lower() + "/") \
                    and _norm(new).lower() != _norm(old).lower():
                raise _err(errno.EINVAL, "Can't move a folder into itself")
            target = self._find_child(nparent, name)
            if target is not None and target is not src:
                if target.is_dir:
                    if not src.is_dir:
                        raise _err(errno.EISDIR)
                    if self._list(target.first_cluster):
                        raise _err(errno.ENOTEMPTY)
                elif src.is_dir:
                    raise _err(errno.ENOTDIR)
                self._delete(target)
            if nparent.first_cluster == src.parent:
                src.name = name
                self._write_record(src)
                self._path_cache.clear()
            else:
                old_record, old_parent = src.record, src.parent
                src.name = name
                self._add_entry(nparent, src)
                self._drop_record(src, old_record, old_parent)

    def truncate(self, path: str, length: int) -> None:
        if length > MAX_FILE_SIZE:
            raise _err(errno.EFBIG, "Xbox files can't be larger than 4 GB")
        with self._mutate():
            e = self._get(path)
            if e.is_dir:
                raise _err(errno.EISDIR)
            old = e.size
            if length > old:
                self._ensure_clusters(e, length)
                e.valid = old if e.valid is None else min(e.valid, old)
                self._pending.add(e)
            elif length < old:
                self._shrink(e, length)
                if e.valid is not None:
                    e.valid = min(e.valid, length)
            e.size = length
            self._settle_valid(e)
            e.mtime_raw = fatx_encode_time()
            self._write_record(e)

    def write_file(self, path: str, data: bytes, offset: int) -> int:
        end = offset + len(data)
        if end > MAX_FILE_SIZE:
            raise _err(errno.EFBIG, "Xbox files can't be larger than 4 GB")
        with self._mutate():
            e = self._get(path)
            if e.is_dir:
                raise _err(errno.EISDIR)
            if not data:
                return 0
            self._ensure_clusters(e, max(end, e.size))
            valid = e.size if e.valid is None else e.valid
            if offset > valid:
                self._zero(e, valid, offset)
                valid = offset
            self._io(e, offset, len(data), data)
            if end > e.size:
                if e.valid is not None:
                    e.valid = max(valid, end)
                e.size = end
            elif e.valid is not None:
                e.valid = max(valid, end)
            self._settle_valid(e)
            e.mtime_raw = fatx_encode_time()
            self._write_record(e)
            return len(data)

    def set_times(self, path: str, atime: float | None = None, mtime: float | None = None) -> None:
        with self._mutate():
            e = self._get(path)
            if e is self.root:
                return
            e.atime_raw = fatx_encode_time(atime)
            e.mtime_raw = fatx_encode_time(mtime)
            self._write_record(e)

    def finish_file(self, path: str) -> None:
        """Called when a file is closed: fill any never-written gap with zeros."""
        if not self.writable:
            return
        with self._mutate():
            e = self.resolve(path)
            if e is not None and e in self._pending:
                self._zero(e, e.valid, e.size)
                e.valid = None
                self._pending.discard(e)

    def sync(self) -> None:
        if not self.writable:
            return
        with self._lock:
            for e in list(self._pending):
                self._zero(e, e.valid, e.size)
                e.valid = None
            self._pending.clear()
            self._commit_fat()
            self.dev.fsync()

    def count_free_clusters(self) -> int:
        """Count free clusters (1..max_cluster). Call before serving requests."""
        with self._lock:
            self._commit_fat()
            es = self.fat_entry_size
            start, end = es, (self.max_cluster + 1) * es
            step = 4 * 1024 * 1024
            free = 0
            for pos in range(start, end, step):
                chunk = self.dev.read(self.fat_offset + pos, min(step, end - pos))
                free += array(self._fat_tc, chunk).count(0)
            self.free_clusters = free
            return free


@dataclass
class PartitionInfo:
    source: str
    disk: str
    name: str
    offset: int
    size: int

    @property
    def key(self) -> str:
        return f"{self.source}|{self.offset}"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def find_partitions(dev: BlockDevice, disk_desc: str) -> list[PartitionInfo]:
    found: list[PartitionInfo] = []

    def valid(off, size):
        try:
            FatxVolume(dev, off, size)
            return True
        except (FatxError, OSError, struct.error):
            return False

    if isinstance(dev, SplitFileDevice):
        off = USB_DATA_OFFSET
        if dev.size <= off + 0x2000 or dev.read(off, 4) != b"XTAF":
            return []
        try:
            FatxVolume(dev, off, dev.size - off, usb=True)
        except (FatxError, OSError, struct.error):
            return []
        return [PartitionInfo(dev.path, disk_desc, "Content", off, dev.size - off)]

    if dev.read(0, 4) in (b"XTAF", b"FATX") and valid(0, dev.size):
        return [PartitionInfo(dev.path, disk_desc, "Partition", 0, dev.size)]

    for name, off, size in devkit_partitions(dev):
        if dev.read(off, 4) == b"XTAF" and valid(off, size):
            found.append(PartitionInfo(dev.path, disk_desc, name, off, size))

    taken = [(p.offset, p.offset + p.size) for p in found]
    for name, off, size in XBOX360_LAYOUT:
        if off + 0x1000 > dev.size or any(a <= off < b for a, b in taken):
            continue
        if dev.read(off, 4) != b"XTAF":
            continue
        sz = size if size else dev.size - off
        sz = min(sz, dev.size - off)
        if valid(off, sz):
            found.append(PartitionInfo(dev.path, disk_desc, name, off, sz))
    return found


DEVKIT_MAGIC = 0x00020000
DEVKIT_TABLE = [("Devkit Content", 0x08), ("Devkit Dashboard", 0x10)]


def devkit_partitions(dev: BlockDevice) -> list[tuple[str, int, int]]:
    """Read a devkit partition table (big-endian, sector units) if the drive has one."""
    table = dev.read(0, 0x18)
    if struct.unpack_from(">I", table, 0)[0] != DEVKIT_MAGIC:
        return []
    parts = []
    for name, pos in DEVKIT_TABLE:
        sector, count = struct.unpack_from(">II", table, pos)
        off, size = sector * SECTOR, count * SECTOR
        if not off or off + 0x1000 > dev.size:
            continue
        if not size or off + size > dev.size:
            size = dev.size - off
        parts.append((name, off, size))
    return parts


def scan_physical_drives(max_drives: int = 32) -> tuple[list[PartitionInfo], bool]:
    """Return (partitions, access_denied). Windows only."""
    parts: list[PartitionInfo] = []
    denied = False
    if not IS_WINDOWS:
        return parts, denied
    for i in range(max_drives):
        path = f"\\\\.\\PhysicalDrive{i}"
        try:
            dev = BlockDevice(path)
        except PermissionError:
            denied = True
            continue
        except OSError:
            continue
        try:
            desc = f"Disk {i}: {dev.model or 'Unknown'} ({human_size(dev.size)})"
            parts.extend(find_partitions(dev, desc))
        except OSError:
            pass
        finally:
            dev.close()
    return parts, denied


def scan_image(path: str) -> list[PartitionInfo]:
    dev = BlockDevice(path)
    try:
        desc = f"Image: {os.path.basename(path)} ({human_size(dev.size)})"
        return find_partitions(dev, desc)
    finally:
        dev.close()


def scan_usb_drives() -> list[PartitionInfo]:
    parts: list[PartitionInfo] = []
    if not IS_WINDOWS:
        return parts
    _k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _k32.GetDriveTypeW.restype = wintypes.UINT
    old_mode = _k32.SetErrorMode(1)
    try:
        mask = _k32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            root = f"{letter}:\\"
            if not (mask >> i) & 1 or _k32.GetDriveTypeW(root) not in (2, 3):
                continue
            folder = root + USB_FOLDER
            try:
                if not os.path.isfile(os.path.join(folder, "Data0000")):
                    continue
                dev = SplitFileDevice(folder)
            except (FatxError, OSError):
                continue
            try:
                desc = f"USB drive {letter}: ({human_size(dev.size)})"
                parts.extend(find_partitions(dev, desc))
            except OSError:
                pass
            finally:
                dev.close()
    finally:
        _k32.SetErrorMode(old_mode)
    return parts
