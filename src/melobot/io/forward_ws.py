import asyncio
import time
from itertools import count
from logging import DEBUG

import websockets
from websockets.exceptions import ConnectionClosed

from ..base.abc import AbstractConnector, BotLife
from ..base.exceptions import BotConnectFailed, get_better_exc
from ..base.tools import get_rich_str, to_task
from ..base.typing import TYPE_CHECKING, Any, ModuleType, Type

if TYPE_CHECKING:
    import websockets.client

    from ..base.abc import BotAction


class ForwardWsConn(AbstractConnector):
    """正向 websocket 连接器

    .. admonition:: 注意
       :class: caution

       在 melobot 中，正向 websocket 连接器会开启一个 ws 客户端。这个客户端只能和一个服务端通信。

       本连接器目前暂不支持鉴权。
    """

    def __init__(
        self,
        connect_host: str,
        connect_port: int,
        max_retry: int = -1,
        retry_delay: float = 4.0,
        cd_time: float = 0.2,
        allow_reconnect: bool = False,
    ) -> None:
        """初始化一个正向 websocket 连接器

        :param connect_host: 连接的 host
        :param connect_port: 连接的 port
        :param max_retry: 连接最大重试次数，默认 -1 代表无限次重试
        :param retry_delay: 连接重试间隔时间
        :param cd_time: 行为操作冷却时间（用于防止风控）
        :param allow_reconnect: 是否在断连后重连。默认为 `False`，即服务端断线直接停止 bot；若为 `True`，则会按照 `max_retry`, `retry_delay` 不断尝试重连，重连成功前时所有行为操作将阻塞。
        """
        super().__init__(cd_time)
        #: 连接失败最大重试次数
        self.max_retry: int = max_retry
        #: 连接失败重试间隔
        self.retry_delay: float = retry_delay if retry_delay > 0 else 0
        #: ws 连接的 url（形如：ws://xxx:xxx）
        self.url = f"ws://{connect_host}:{connect_port}"
        #: 连接对象
        self.conn: "websockets.client.WebSocketClientProtocol"

        self._send_queue: asyncio.Queue["BotAction"] = asyncio.Queue()
        self._pre_send_time = time.time()
        self._client_close: asyncio.Future[Any]
        self._conn_ready = asyncio.Event()
        self._allow_reconn = allow_reconnect
        self._reconn_flag = False
        self._run_lock = asyncio.Lock()

    async def _run(self) -> None:
        """运行客户端"""
        async with self._run_lock:
            self._client_close = asyncio.Future()
            created_flag = False
            iter = count(0) if self.max_retry < 0 else range(self.max_retry + 1)

            for _ in iter:
                try:
                    self.conn = await websockets.connect(
                        self.url,
                        logger=self.logger if self.logger.level == DEBUG else None,
                    )
                    created_flag = True
                    break
                except Exception as e:
                    self.logger.warning(
                        f"ws 连接建立失败，{self.retry_delay}s 后自动重试。错误：{e}"
                    )
                    await asyncio.sleep(self.retry_delay)
            if not created_flag:
                raise BotConnectFailed("连接重试已达最大重试次数，已放弃建立连接")

            try:
                self.logger.info("连接器与 OneBot 实现程序建立了 ws 连接")
                self._conn_ready.set()
                to_task(self._listen)
                await self._client_close
            finally:
                # 默认关闭等待时长是 10s，某些服务端对“礼貌的连接关闭”不响应
                # 不等待它们, CLOSE anyway!!! :)
                self.conn.close_timeout = 0.01
                await self.conn.close()
                await self.conn.wait_closed()
                self.logger.info("ws 客户端连接已关闭")

    def _close(self) -> None:
        """关闭连接"""
        if self._client_close.done():
            return
        else:
            # 当 ctrl-c 发生后运行至此，或当 alive 任务被取消后运行至此
            # 无论是否指定允许重连，都要立刻重设 _allow_reconn。以此保证 _listen 结束后不会再发起 _reconnect
            self._allow_reconn = False
            self._client_close.set_result(True)

    async def _reconnect(self) -> None:
        """关闭已经无效的连接，随后开始尝试建立新连接"""
        self._conn_ready.clear()
        self._client_close.set_result(True)
        self._reconn_flag = True
        to_task(self._run())

    async def __aenter__(self) -> "ForwardWsConn":
        to_task(self._run())
        return self

    async def __aexit__(
        self, exc_type: Type[Exception], exc_val: Exception, exc_tb: ModuleType
    ) -> bool:
        self._close()
        return await super().__aexit__(exc_type, exc_val, exc_tb)

    async def _send(self, action: "BotAction") -> None:
        """发送一个 action 给连接器。实际上是先提交到 send_queue."""
        await self._ready_signal.wait()
        await self._conn_ready.wait()

        if self.slack:
            self.logger.debug(f"action {id(action)} 因 slack 状态被丢弃")
            return
        await self._send_queue.put(action)
        self.logger.debug(f"action {id(action)} 已成功加入发送队列")

    async def _send_queue_watch(self) -> None:
        """真正的发送方法。从 send_queue 提取 action 并按照一些处理步骤操作."""
        await self._ready_signal.wait()

        try:
            while True:
                action = await self._send_queue.get()
                await self._conn_ready.wait()
                if self.logger.level == DEBUG:
                    self.logger.debug(
                        f"action {id(action)} 准备发送，结构如下：\n"
                        + get_rich_str(action.__dict__)
                    )
                await self._bot_bus.emit(BotLife.ACTION_PRESEND, action, wait=True)
                self.logger.debug(f"action {id(action)} presend hook 已完成")
                action_str = action.flatten()
                wait_time = self.cd_time - (time.time() - self._pre_send_time)
                self.logger.debug(f"action {id(action)} 冷却等待：{wait_time}")
                await asyncio.sleep(wait_time)
                await self.conn.send(action_str)
                self.logger.debug(f"action {id(action)} 已发送")
                self._pre_send_time = time.time()
        except asyncio.CancelledError:
            self.logger.debug("连接器发送队列监视任务已被结束")
        except ConnectionClosed:
            self.logger.error(
                "连接器与 OneBot 实现程序的通信已经停止，无法再执行行为操作"
            )

    async def _listen(self) -> None:
        """从 OneBot 实现程序接收一个事件，并处理"""
        await self._ready_signal.wait()
        await self._conn_ready.wait()
        if not self._reconn_flag:
            await self._bot_bus.emit(BotLife.FIRST_CONNECTED)
            self.logger.debug("FIRST_CONNECTED hook 已完成")
        else:
            await self._bot_bus.emit(BotLife.RECONNECTED)
            self.logger.debug("RECONNECTED hook 已完成")

        try:
            while True:
                try:
                    raw_event = await self.conn.recv()
                    self.logger.debug(f"收到事件，未格式化的字符串：\n{raw_event}")
                    if raw_event == "":
                        continue
                    event = self._event_builder.build(raw_event)
                    if self.logger.level == DEBUG:
                        self.logger.debug(
                            f"event {id(event)} 构建完成，结构：\n"
                            + get_rich_str(event.raw)
                        )
                    if event.is_resp_event():
                        to_task(self._resp_dispatcher.respond(event))  # type: ignore
                    else:
                        to_task(self._common_dispatcher.dispatch(event))  # type: ignore
                except ConnectionClosed:
                    raise
                except Exception as e:
                    self.logger.error("bot 连接器监听任务抛出异常")
                    self.logger.error(f"异常点 raw_event：{raw_event}")
                    self.logger.error("异常回溯栈：\n" + get_better_exc(e))
                    self.logger.error("异常点局部变量：\n" + get_rich_str(locals()))
        except asyncio.CancelledError:
            self.logger.debug("连接器监听任务已停止")
        except ConnectionClosed:
            self.logger.debug("连接器与 OneBot 实现程序的通信已经停止")
        finally:
            if self._client_close.done():
                return
            if not self._allow_reconn:
                self._close()
                return
            await self._reconnect()

    async def _alive_tasks(self) -> list[asyncio.Task]:
        async def alive():
            try:
                while True:
                    await self._client_close
                    if not self._allow_reconn:
                        return
                    await self._conn_ready.wait()
            except asyncio.CancelledError:
                self._close()

        to_task(self._send_queue_watch())
        return [to_task(alive())]
