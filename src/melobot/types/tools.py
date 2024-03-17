import asyncio as aio
import inspect
import io
import os
import pathlib
import time
from asyncio import iscoroutine, iscoroutinefunction
from contextlib import asynccontextmanager
from functools import wraps

import rich

from .exceptions import BotBaseUtilsError, BotRuntimeError
from .typing import T1, T2, T3, Any, Callable, Coroutine, Optional, P, T


class Singleton:
    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, "__instance__"):
            cls.__instance__ = super(Singleton, cls).__new__(cls)
        return cls.__instance__


class AsyncTwinEvent(aio.Event):
    """
    孪生 Event，会和绑定的一方时刻保持状态相反。
    """

    def __init__(self) -> None:
        super().__init__()
        self._twin: Optional[AsyncTwinEvent] = None

    def bind(self, twin: "AsyncTwinEvent") -> None:
        self._twin = twin
        if self.is_set():
            super(AsyncTwinEvent, self._twin).clear()
        else:
            super(AsyncTwinEvent, self._twin).set()

    def set(self) -> None:
        super().set()
        if self._twin:
            super(AsyncTwinEvent, self._twin).clear()

    def clear(self) -> None:
        super().clear()
        if self._twin:
            super(AsyncTwinEvent, self._twin).set()


def get_twin_event() -> tuple[aio.Event, aio.Event]:
    """
    获得两个时刻保持状态相反的 asyncio.Event。
    获得的第一个为 unset，另一个为 set
    """
    a, b = AsyncTwinEvent(), AsyncTwinEvent()
    a.bind(b)
    b.bind(a)
    return a, b


class RWController:
    """
    异步读写控制器。提供异步安全的读写上下文
    """

    def __init__(self, read_limit: Optional[int] = None) -> None:
        write_semaphore = aio.Semaphore(1)
        if read_limit:
            read_semaphore = aio.Semaphore(read_limit)
        else:
            read_semaphore = None
        read_num = 0
        read_num_lock = aio.Lock()

        @asynccontextmanager
        async def safe_read():
            nonlocal read_num, read_semaphore, write_semaphore, read_num_lock
            if read_semaphore:
                await read_semaphore.acquire()
            async with read_num_lock:
                if read_num == 0:
                    await write_semaphore.acquire()
                read_num += 1
            try:
                yield
            finally:
                async with read_num_lock:
                    read_num -= 1
                    if read_num == 0:
                        write_semaphore.release()
                    if read_semaphore:
                        read_semaphore.release()

        @asynccontextmanager
        async def safe_write():
            nonlocal write_semaphore
            await write_semaphore.acquire()
            try:
                yield
            finally:
                write_semaphore.release()

        self.safe_read = safe_read
        self.safe_write = safe_write


class IdWorker:
    """
    雪花算法生成 ID
    """

    def __init__(self, datacenter_id, worker_id, sequence=0) -> None:
        self.MAX_WORKER_ID = -1 ^ (-1 << 3)
        self.MAX_DATACENTER_ID = -1 ^ (-1 << 5)
        self.WOKER_ID_SHIFT = 12
        self.DATACENTER_ID_SHIFT = 12 + 3
        self.TIMESTAMP_LEFT_SHIFT = 12 + 3 + 5
        self.SEQUENCE_MASK = -1 ^ (-1 << 12)
        self.STARTEPOCH = 1064980800000
        # sanity check
        if worker_id > self.MAX_WORKER_ID or worker_id < 0:
            raise ValueError("worker_id 值越界")
        if datacenter_id > self.MAX_DATACENTER_ID or datacenter_id < 0:
            raise ValueError("datacenter_id 值越界")
        self.worker_id = worker_id
        self.datacenter_id = datacenter_id
        self.sequence = sequence
        self.last_timestamp = -1

    def __gen_timestamp(self) -> int:
        """
        生成整数时间戳
        """
        return int(time.time() * 1000)

    def get_id(self) -> int:
        """
        获取新 ID
        """
        timestamp = self.__gen_timestamp()

        # 时钟回拨
        if timestamp < self.last_timestamp:
            raise ValueError(f"时钟回拨，{self.last_timestamp} 前拒绝 id 生成请求")
        if timestamp == self.last_timestamp:
            self.sequence = (self.sequence + 1) & self.SEQUENCE_MASK
            if self.sequence == 0:
                timestamp = self.__til_next_millis(self.last_timestamp)
        else:
            self.sequence = 0
        self.last_timestamp = timestamp
        new_id = (
            ((timestamp - self.STARTEPOCH) << self.TIMESTAMP_LEFT_SHIFT)
            | (self.datacenter_id << self.DATACENTER_ID_SHIFT)
            | (self.worker_id << self.WOKER_ID_SHIFT)
            | self.sequence
        )
        return new_id

    def __til_next_millis(self, last_timestamp) -> int:
        """
        等到下一毫秒
        """
        timestamp = self.__gen_timestamp()
        while timestamp <= last_timestamp:
            timestamp = self.__gen_timestamp()
        return timestamp


ID_WORKER = IdWorker(1, 1, 0)


def get_id() -> str:
    """
    获取一个全局唯一 id，由 melobot 内部 id 生成器提供
    """
    return str(ID_WORKER.get_id())


def get_rich_str(obj: object) -> str:
    """
    返回使用 rich 格式化的 object
    """
    sio = io.StringIO()
    rich.print(obj, file=sio)
    return sio.getvalue().strip("\n")


def this_dir(*relative_path: str) -> str:
    """
    包内 py 脚本可通过该方法获取所在目录的绝对路径。
    提供参数，还可拼接路径。

    请注意：

    1. 使用 `this_dir()`，只能这样导入：（导入语句后可以使用 as 子句）

    ```python
    from melobot import this_dir
    # 或
    from melobot.types.tools import this_dir
    ```

    2. 若 `B.py` 从 `A.py` 导入包含 `this_dir()` 调用的结构，
    导入前 `this_dir()` 必须已运行，而不能延迟求值

    `A.py` 中：
    ```python
    class Foo:
        DIR = this_dir()
        LAMBDA_DIR = lambda: this_dir()
        GET_DIR = lambda: this_dir()
    OUTER_DIR = Foo.LAMBDA_DIR()  # Ok
    ```

    `B.py` 中：
    ```python
    from .A import Foo, OUTER_DIR
    Foo.OUTER_DIR  # Ok
    Foo.DIR        # Ok
    Foo.GET_DIR()  # Error
    ```
    """
    cur_finfo: inspect.FrameInfo | None = None
    cur_idx: int
    caller_path: str | None = None
    stacks = inspect.stack()

    for idx, finfo in enumerate(stacks):
        if finfo.function == "this_dir" and os.path.samefile(finfo.filename, __file__):
            cur_finfo, cur_idx = finfo, idx
    if cur_finfo is None:
        raise BotRuntimeError("this_dir 定位失败，请检查本函数使用方式是否正确")

    for idx, finfo in enumerate(stacks[cur_idx + 1 :]):
        if finfo.function == "<module>":
            for val in finfo.frame.f_locals.values():
                if val is __dir_inspector__:
                    caller_path = finfo.filename
                    break
            if caller_path is not None:
                break
    if caller_path is None:
        raise BotRuntimeError("this_dir 定位失败，请检查本函数使用方式是否正确")

    return str(
        pathlib.Path(caller_path).parent.joinpath(*relative_path).resolve(strict=True)
    )


__dir_inspector__ = this_dir


def to_async(func: Callable[[], T]) -> Callable[[], Coroutine[Any, Any, T]]:
    """
    异步包装器，将一个同步函数包装为异步函数。保留返回值。
    如果需要传参使用 partial 包裹
    """

    async def wrapper():
        return func()

    return wrapper


def to_coro(func: Callable[[], T]) -> Coroutine[Any, Any, T]:
    """
    协程包装器，将一个同步函数包装为协程。保留返回值。
    如果需要传参使用 partial 包裹
    """

    f = to_async(func)
    return f()


def to_task(obj: Callable[[], T] | Coroutine[Any, Any, T]) -> aio.Task[T]:
    """
    任务包装器，将一个同步函数或异步函数或协程包装为任务。
    保留返回值。如果需要传参使用 partial 包裹
    """
    if iscoroutine(obj):
        return aio.create_task(obj)
    elif iscoroutinefunction(obj):
        return aio.create_task(obj())
    else:
        return aio.create_task(to_coro(obj))  # type: ignore


def lock(callback: Callable[[], Coroutine[Any, Any, T1]]):
    """
    锁装饰器，可以为被装饰的异步函数/方法加锁。
    在获取锁冲突时，调用 callback 获得一个回调并执行。回调执行完毕后直接返回
    """
    alock = aio.Lock()
    if not callable(callback):
        raise BotBaseUtilsError(
            f"lock 装饰器的 callback 参数不可调用，callback 值为：{callback}"
        )

    def deco_func(
        func: Callable[P, Coroutine[Any, Any, T2]]
    ) -> Callable[P, Coroutine[Any, Any, T1 | T2]]:
        @wraps(func)
        async def wrapped_func(*args, **kwargs) -> T1 | T2:
            if alock.locked():
                cb = callback()
                if not iscoroutine(cb):
                    raise BotBaseUtilsError(
                        f"lock 装饰器的 callback 返回的不是协程，返回的回调为：{cb}"
                    )
                return await cb
            async with alock:
                return await func(*args, **kwargs)

        return wrapped_func

    return deco_func


def cooldown(
    busy_callback: Callable[[], Coroutine[Any, Any, T1]],
    cd_callback: Optional[Callable[[float], Coroutine[Any, Any, T2]]] = None,
    interval: float = 5,
):
    """
    冷却装饰器，可以为被装饰的异步函数/方法添加 cd 时间。
    cd_callback 的类型：接受一个 float 参数（cd 剩余时间），
    返回一个协程的 Callable 对象。

    如果被装饰方法已有一个在运行，此时会直接调用 busy_callback 生成一个回调并执行。
    回调执行完毕后直接返回。

    如果被装饰方法没有正在运行的，但在冷却完成前被调用，且此时 cd_callback 不为 None，
    会使用 cd_callback 生成一个回调并执行。如果此时 cd_callback 为 None，
    被装饰方法会持续等待直至冷却结束再执行
    """
    alock = aio.Lock()
    pre_finish_t = time.time() - interval - 1
    if not callable(busy_callback):
        raise BotBaseUtilsError(
            f"cooldown 装饰器的 busy_callback 参数不可调用，busy_callback 值为：{busy_callback}"
        )
    if cd_callback is not None and not callable(cd_callback):
        raise BotBaseUtilsError(
            f"cooldown 装饰器的 cd_callback 参数不可调用，cd_callback 值为：{cd_callback}"
        )

    def deco_func(
        func: Callable[P, Coroutine[Any, Any, T3]]
    ) -> Callable[P, Coroutine[Any, Any, T1 | T2 | T3]]:
        @wraps(func)
        async def wrapped_func(*args, **kwargs) -> T1 | T2 | T3:
            nonlocal pre_finish_t
            if alock.locked():
                busy_cb = busy_callback()
                if not iscoroutine(busy_cb):
                    raise BotBaseUtilsError(
                        f"cooldown 装饰器的 busy_callback 返回的不是协程，返回的回调为：{busy_cb}"
                    )
                return await busy_cb

            async with alock:
                duration = time.time() - pre_finish_t
                if duration > interval:
                    ret = await func(*args, **kwargs)
                    pre_finish_t = time.time()
                    return ret

                remain_t = interval - duration
                if cd_callback is not None:
                    cd_cb = cd_callback(remain_t)
                    if not iscoroutine(cd_cb):
                        raise BotBaseUtilsError(
                            f"cooldown 装饰器的 cd_callback 返回的不是协程，返回的回调为：{cd_cb}"
                        )
                    return await cd_cb
                else:
                    await aio.sleep(remain_t)
                    ret = await func(*args, **kwargs)
                    pre_finish_t = time.time()
                    return ret

        return wrapped_func

    return deco_func


def semaphore(callback: Callable[[], Coroutine[Any, Any, T1]], value: int = -1):
    """
    信号量装饰器，可以为被装饰的异步函数/方法添加信号量控制。
    在信号量无法立刻获取时，将调用 callback 获得回调并执行。回调执行完毕后直接返回
    """
    a_semaphore = aio.Semaphore(value)
    if not callable(callback):
        raise BotBaseUtilsError(
            f"semaphore 装饰器的 callback 参数不可调用，callback 值为：{callback}"
        )

    def deco_func(
        func: Callable[P, Coroutine[Any, Any, T2]]
    ) -> Callable[P, Coroutine[Any, Any, T1 | T2]]:
        @wraps(func)
        async def wrapped_func(*args, **kwargs) -> T1 | T2:
            if a_semaphore.locked():
                cb = callback()
                if not iscoroutine(cb):
                    raise BotBaseUtilsError(
                        f"semaphore 装饰器的 callback 返回的不是协程，返回的回调为：{cb}"
                    )
                return await cb
            async with a_semaphore:
                return await func(*args, **kwargs)

        return wrapped_func

    return deco_func


def timelimit(callback: Callable[[], Coroutine[Any, Any, T1]], timeout: float = 5):
    """
    时间限制装饰器，可以为被装饰的异步函数/方法添加超时控制。
    在超时之后，将调用 callback 获得回调并执行，同时取消原任务
    """
    if not callable(callback):
        raise BotBaseUtilsError(
            f"timelimit 装饰器的 callback 参数不可调用，callback 值为：{callback}"
        )

    def deco_func(func: Callable[P, Coroutine[Any, Any, T2]]) -> Callable[P, Coroutine[Any, Any, T1 | T2]]:
        @wraps(func)
        async def wrapped_func(*args, **kwargs) -> T1 | T2:
            try:
                return await aio.wait_for(func(*args, **kwargs), timeout)
            except aio.TimeoutError:
                cb = callback()
                if not iscoroutine(cb):
                    raise BotBaseUtilsError(
                        f"timelimit 装饰器的 callback 返回的不是协程，返回的回调为：{cb}"
                    )
                return await cb

        return wrapped_func

    return deco_func


def call_later(callback: Callable[[], None], delay: float):
    """
    在指定的 delay 后调度一个 callback 执行。注意这个 callback 应该是同步方法
    """
    return aio.get_running_loop().call_later(delay, callback)


def call_at(callback: Callable[[], None], timestamp: float):
    """
    在指定的时间戳后调度一个 callback 执行。注意这个 callback 应该是同步方法

    当 timestamp <= 当前时刻将立即执行
    """
    if timestamp <= time.time():
        return aio.get_running_loop().call_soon(callback)
    else:
        return aio.get_running_loop().call_later(timestamp - time.time(), callback)


def async_later(callback: Coroutine[Any, Any, T], delay: float) -> aio.Future[T]:
    """
    在指定的 delay 后调度一个 callback 执行。注意这个 callback 应该是协程。
    返回一个 future 对象，可用于等待结果
    """

    async def async_cb(fut: aio.Future) -> Any:
        try:
            await aio.sleep(delay)
            res = await callback
            fut.set_result(res)
        except aio.CancelledError:
            callback.close()

    fut: aio.Future[Any] = aio.Future()
    aio.create_task(async_cb(fut))
    return fut


def async_at(callback: Coroutine[Any, Any, T], timestamp: float) -> aio.Future[T]:
    """
    在指定的 timestamp 调度一个 callback 执行。注意这个 callback 应该是协程。
    返回一个 future 对象，可用于等待结果

    当 timestamp <= 当前时刻将立即执行
    """
    if timestamp <= time.time():
        return async_later(callback, 0)
    else:
        return async_later(callback, timestamp - time.time())


def async_interval(
    callback: Callable[[], Coroutine[Any, Any, Any]], interval: float
) -> aio.Task[None]:
    """
    以指定的 interval 调度一个回调执行。callback 是协程产生器。
    返回一个 task 对象用于取消回调
    """

    async def interval_cb():
        try:
            while True:
                coro = callback()
                await aio.sleep(interval)
                await coro
        except aio.CancelledError:
            coro.close()

    t = aio.create_task(interval_cb())
    return t
