#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/locks.py
注册工程通用内核级原子排他锁 (Atomic Claim Lock)
沉淀自 ufo-register 工业级多并发实战：
利用操作系统文件系统层的原子创建 (os.O_CREAT | os.O_EXCL)，
提供微秒级开销的零竞态并发资源排他认领，彻底杜绝多 Worker 邮箱撞车。
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path



class AtomicClaimLock:
    """操作系统内核级原子排他锁。
    
    使用范式:
        lock = AtomicClaimLock(claim_dir="results/.claims", resource_id=email)
        if not lock.acquire():
            # 已被其他 Worker 认领，直接跳过
            return
        try:
            # 业务处理 (如向目标站发信)
            ...
        except Exception:
            # 失败主动释放，允许后续重试
            lock.release()
            raise
    """

    def __init__(self, claim_dir: str, resource_id: str, timeout_seconds: int = 180):
        self.claim_dir = os.path.abspath(claim_dir)
        self.resource_id = resource_id.strip()
        self.timeout_seconds = timeout_seconds
        os.makedirs(self.claim_dir, exist_ok=True)
        
        # 安全转义资源标识为文件名。
        # 仅做字符替换是不够的：`a.b@x.com` / `a_b@x.com` 都会被折叠成 `a_b_x_com`，
        # 于是【两个不同资源共用同一把锁】—— 后到的会被误判为"已被认领"而跳过，
        # 等于凭空丢号。故追加 seed 的 SHA1 前 8 位做不可逆消歧后缀。
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", self.resource_id.lower())
        disambig = hashlib.sha1(self.resource_id.encode("utf-8")).hexdigest()[:8]
        self.lock_file = os.path.join(self.claim_dir, f"{safe_name}_{disambig}.lock")
        self._acquired = False

    def acquire(self) -> bool:
        """尝试原子抢占锁。若已被占用且未超时，返回 False；若抢占成功返回 True。"""
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{os.getpid()}----{time.time()}----{self.resource_id}\n")
            self._acquired = True
            return True
        except FileExistsError:
            # 检查是否为死锁/超时残留
            try:
                mtime = os.path.getmtime(self.lock_file)
                if time.time() - mtime > self.timeout_seconds:
                    owner_pid = 0
                    try:
                        owner_line = Path(self.lock_file).read_text(encoding="utf-8", errors="replace").split("----", 1)[0]
                        owner_pid = int(owner_line.strip())
                    except (OSError, ValueError):
                        pass
                    if owner_pid and _pid_is_alive(owner_pid):
                        return False
                    os.remove(self.lock_file)
                    return self.acquire()
            except Exception:
                pass
            return False

    def release(self):
        """主动释放锁（通常在发信失败或异常退出时调用）。"""
        try:
            if os.path.exists(self.lock_file):
                os.remove(self.lock_file)
            self._acquired = False
        except Exception:
            pass

    def __enter__(self) -> "AtomicClaimLock":
        """进入 with 块，返回锁对象自身。

        曾误写为 `return self.acquire()` —— 于是 `with lock as ctx` 里的 ctx 是
        True/False 布尔值，`ctx.release()` 直接 AttributeError。两者语义也不同：
        acquire() 的 bool 是"我抢到了吗"（False 是正常竞争结果，不该抛异常），
        而 __enter__ 契约要求返回被管理的对象（as 子句拿到它）。

        抢不到锁时【显式抛 BlockingIOError】，而不是"宽容地进块"：
        with 的语义是"进入受管理的临界区"，若抢不到却让代码进块，
        **块内业务照跑** —— 锁就完全失效了（比返回 bool 更危险）。
        单独调用 acquire() 的旧范式不受影响，仍返回 bool。
        """
        if not self.acquire():
            raise BlockingIOError(
                f"资源已被其他 Worker 认领: {self.resource_id} ({self.lock_file})"
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 无论正常退出还是异常退出，都必须释放锁！
        # 曾误写为 "if exc_type is not None: release()" —— 导致正常走完 with 时
        # 锁文件永久残留，该资源被死锁（下次 acquire 返回 False 直到超时）。
        self.release()
        return False


# 进程内线程锁：按【锁文件路径】分别持锁，而不是一把全局大锁。
# 旧实现用单个全局 _THREAD_LOCK 并在整个 with 期间持有 —— 结果**任意两个写不同文件的线程**
# 也被强制串行（实测 6 线程写 6 个不同文件、各 0.3s，总耗时 1.81s 而非 ~0.3s）。
# 在多 Worker 注册流水线里，每个账号都在写自己的台账/产物，这个全局锁把全部磁盘写串了起来。
def _pid_is_alive(pid: int) -> bool:
    """只把确认已退出的持锁进程视为可回收。"""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def atomic_write_text_safe(path: str | Path, text: str) -> None:
    """用进程/线程唯一临时文件原子替换文本，避免共享 .tmp 互踩。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def load_json_dict_strict(path: str | Path) -> dict:
    """读取 JSON 对象；损坏与不存在严格区分，损坏文件备份后抛错。"""
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        backup = target.with_suffix(target.suffix + ".corrupt")
        try:
            shutil.copy2(target, backup)
        except OSError:
            pass
        raise ValueError(f"JSON 文件损坏: {target}") from exc
    if not isinstance(data, dict):
        backup = target.with_suffix(target.suffix + ".corrupt")
        try:
            shutil.copy2(target, backup)
        except OSError:
            pass
        raise ValueError(f"JSON 顶层必须是对象: {target}")
    return data


_THREAD_LOCKS: "dict[str, threading.Lock]" = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(lock_path: str) -> threading.Lock:
    """返回某个锁文件对应的进程内线程锁（同文件复用同一把，不同文件彼此独立）。"""
    with _THREAD_LOCKS_GUARD:
        lk = _THREAD_LOCKS.get(lock_path)
        if lk is None:
            lk = threading.Lock()
            _THREAD_LOCKS[lock_path] = lk
        return lk


@contextlib.contextmanager
def interprocess_file_lock(lock_file_path: Path, timeout: float = 10.0):
    """跨进程与跨线程双重安全文件排他锁。
    
    1. 进程内使用 threading.Lock() 拦截多线程竞争；
    2. 进程间使用操作系统底层文件排他锁 (Windows: msvcrt / POSIX: fcntl)；
    3. 支持超时轮询重试，防止并发写坏产物文件。
    """
    lock_file = lock_file_path.with_suffix(lock_file_path.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # 只对【同一个 lock_file】互斥；不同目标文件可并发写。
    with _thread_lock_for(str(lock_file.resolve())):
        fd = None
        start_time = time.time()
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fd = open(lock_file, "a+b")
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fd = open(lock_file, "a+")
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (IOError, OSError, PermissionError):
                if fd:
                    try:
                        fd.close()
                    except Exception:
                        pass
                    fd = None
                if time.time() - start_time > timeout:
                    # ★ 超时后禁止写入（清单第 4 条）：原行为是打 WARN 后照样 `yield`，
                    #   而 `fd` 已是 None —— 等于**无锁写入**共享台账。
                    #   两个终端并行跑时会把对方的行盖掉。现改为抛异常，
                    #   让调用方决定重试或记失败，绝不降级写入。
                    raise TimeoutError(
                        f"文件锁等待 {timeout}s 超时，已拒绝写入（避免无锁写坏 {lock_file}）"
                    )
                time.sleep(0.05)

        try:
            yield
        finally:
            if fd:
                try:
                    if sys.platform == "win32":
                        import msvcrt
                        fd.seek(0)
                        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                finally:
                    try:
                        fd.close()
                    except Exception:
                        pass
