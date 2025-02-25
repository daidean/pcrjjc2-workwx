import os
import json
import signal
import sys
import httpx
import asyncio

from dotenv import load_dotenv
from datetime import datetime


def init_pcrjjc2():
    """
    初始化pcrjjc2模块

    1、通过提前引入，规避模块中的init执行

    2、调整模块版本信息写入路径，避免pcrjjc2仓库产生修改（强迫症）

    返回所需的B站SDK客户端和PCR客户端
    """

    import sys
    import types
    import importlib.util

    module_spec = importlib.util.find_spec("pcrjjc2")
    module = types.ModuleType(module_spec.name)
    module.__path__ = module_spec.submodule_search_locations
    # 1、通过提前引入，规避模块中的init执行
    sys.modules[module_spec.name] = module

    import pcrjjc2.pcrclient as client

    # 2、调整模块版本信息写入路径，避免pcrjjc2仓库产生修改（强迫症）
    client.config = os.path.join(os.path.dirname(__file__), "pcr_version.txt")

    return client.bsdkclient, client.pcrclient


bsdkclient, pcrclient = init_pcrjjc2()


class WorkWx:
    def __init__(self, webhook: str):
        """
        封装企业微信WebHook机器人，提供发送消息接口。
        通过队列缓存消息，降低发送频率（WebHook限速20次/分钟）
        """
        self.webhook = webhook
        self.client = httpx.AsyncClient()
        self.queue = asyncio.Queue()

    async def send_message(self, message: str, delay=False) -> None:
        # 打印日志
        print(f"workwx: {message}")

        # 增加时间和标记
        message = f"{datetime.now()}\n【PCR】{message}"

        # 延迟消息仅推送进队列，暂不发送
        if delay:
            await self.queue.put(message)
            return

        # 非延迟消息则归集队列中的消息
        messages = []
        while not self.queue.empty():
            messages.append(await self.queue.get())
        # 追加本次的消息
        messages.append(message)

        # 调用接口发送消息
        await self.client.post(
            self.webhook,
            json={
                "msgtype": "text",
                "text": {"content": "\n\n".join(messages)},
            },
        )


class HiddenPrints:
    """
    上下文期间，关闭print打印信息
    """

    async def __aenter__(self):
        self._original_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._original_stdout


class PcrWatcher:
    def __init__(self, account: dict, watch_ids: list[int], workwx: WorkWx):
        # 构造客户端
        bli_client = bsdkclient(account, self._verify_captcha, self._verify_error)
        pcr_client = pcrclient(bli_client)

        self.pcr_client = pcr_client
        self.pcr_cache = {watch_id: (0, 0) for watch_id in watch_ids}
        self.workwx = workwx

        # 监听事件，用于停止服务时传递信号给协程
        self.watch_event = asyncio.Event()
        signal.signal(signal.SIGTERM, self.watch_stop)

    async def _verify_captcha(self, gt, challenge, userid) -> None:
        """
        自动过码参考：https://github.com/lulu666lulu/pcrjjc
        """
        verify_url = f"https://pcrd.tencentbot.top/geetest_renew?captcha_type=1"
        verify_url += f"&challenge={challenge}&gt={gt}&userid={userid}&gs=1"

        verify_header = {
            "Content-Type": "application/json",
            "User-Agent": "pcrjjc2/1.0.0",
        }

        verify_count = 0

        async with httpx.AsyncClient(headers=verify_header) as client:
            while verify_count < 3:
                verify_count += 1

                await self.workwx.send_message(
                    f"PCR登录验证：自动过码第{verify_count}次尝试", delay=True
                )

                # 传递验证码参数给平台
                verify_resp = await client.get(verify_url, timeout=5)
                verify_data = json.loads(verify_resp.content)
                verify_uuid = verify_data["uuid"]

                # 拿到验证码对应的uuid
                query_url = f"https://pcrd.tencentbot.top/check/{verify_uuid}"
                query_count = 0

                while query_count < 10:
                    query_count += 1

                    # 通过uuid查询平台过码进度
                    query_resp = await client.get(query_url)
                    query_data: dict = json.loads(query_resp.content)

                    # 若响应表示还在队列中，则最多等待30秒后再重新查询进度
                    if query_queue := query_data.get("queue_num"):
                        query_queue = int(query_queue)
                        query_waittime = min(query_queue, 3) * 10

                        await self.workwx.send_message(
                            "登录验证：自动过码队列中", delay=True
                        )
                        await self.workwx.send_message(
                            f"当前位置：{query_queue}，等待{query_waittime}秒"
                        )

                        await asyncio.sleep(query_waittime)
                        continue

                    # 获取过码进度
                    if query_info := query_data.get("info"):
                        # 过码异常则重新传参等进度
                        if query_info in ["fail", "url invalid"]:
                            await self.workwx.send_message("登录验证：自动过码失败")
                            break

                        # 正在过码则等待5秒后重新查询
                        elif query_info == "in running":
                            await self.workwx.send_message(
                                "登录验证：自动过码运行中", delay=True
                            )
                            await asyncio.sleep(5)
                            continue

                        # 过码成功则返回相应参数
                        elif "validate" in query_info:
                            await self.workwx.send_message(
                                "登录验证：自动过码成功", delay=True
                            )
                            return (
                                query_info["challenge"],
                                query_info["gt_user_id"],
                                query_info["validate"],
                            )
            else:
                await self.workwx.send_message("登录验证：自动过码超时")

    async def _verify_error(self, message: str) -> None:
        await self.workwx.send_message(f"登录失败：{message}")

    async def login(self) -> None:
        """
        登录账号
        """
        while self.pcr_client.shouldLogin:
            await self.workwx.send_message("登录中", delay=True)
            # pcrjjc2模块登录过程中会打印账号密码，此处临时屏蔽print输出
            async with HiddenPrints():
                await self.pcr_client.login()
            await self.workwx.send_message("登录成功")

    async def query(self, userid: int) -> dict:
        """
        调用接口查询用户信息
        """
        query_resp = await self.pcr_client.callapi(
            "/profile/get_profile",
            {"target_viewer_id": userid},
        )
        return query_resp

    async def watch(self) -> None:
        print("pcrclient: jjc watcher")
        print("pcrclient: server starting...")

        # 仅在监听事件未传递停止信号时循环监听
        while not self.watch_event.is_set():
            try:
                # 监听开始，登录b站账号
                try:
                    await self.login()
                except Exception as e:
                    await self.workwx.send_message(f"登录异常：{e}")
                    await asyncio.sleep(3)
                    continue

                try:
                    # 记录查询错误的次数
                    self.user_query_error_count = 0
                    # 记录上报错误的次数
                    self.user_notify_error_count = 0

                    # 登录成功后，启动查询循环，获取用户信息
                    while (
                        not self.watch_event.is_set()  # 未出现停止信号
                        and self.user_notify_error_count < 3  # 异常上报次数未超过阈值
                    ):
                        # 每轮查询中，是否存在排名变动
                        user_rank_has_changed = False

                        # 遍历待监听用户ID以及排名
                        for user_id, (
                            user_jjc_rank,
                            user_pjjc_rank,
                        ) in self.pcr_cache.items():
                            try:
                                # 获取用户当前排名进行对比
                                query_resp = await self.query(user_id)
                                query_info = query_resp["user_info"]

                                # 查询成功则将查询错误次数和上报错误次数清零
                                self.user_query_error_count = 0
                                self.user_notify_error_count = 0

                                query_user_name = query_info["user_name"]
                                query_jjc_rank = query_info["arena_rank"]
                                query_pjjc_rank = query_info["grand_arena_rank"]

                                change_jjc = user_jjc_rank != query_jjc_rank
                                change_pjjc = user_pjjc_rank != query_pjjc_rank

                                # jjc和pjjc无变动时跳过
                                if not change_jjc and not change_pjjc:
                                    continue

                                # 有变动时，根据情况进行提醒
                                user_rank_has_changed = True
                                change_message = f"排名变动：{query_user_name}"

                                if change_jjc:
                                    change_jjc_diff = user_jjc_rank - query_jjc_rank
                                    change_jjc_symbol = (
                                        "↓" if change_jjc_diff < 0 else "↑"
                                    )
                                    change_message += "\n普通竞技场"
                                    change_message += f"（ {change_jjc_symbol} {abs(change_jjc_diff)} ）："
                                    change_message += (
                                        f"{user_jjc_rank} ➜ {query_jjc_rank}"
                                    )

                                if change_pjjc:
                                    change_pjjc_diff = user_pjjc_rank - query_pjjc_rank
                                    change_pjjc_symbol = (
                                        "↓" if change_pjjc_diff < 0 else "↑"
                                    )
                                    change_message += "\n公主竞技场"
                                    change_message += f"（ {change_pjjc_symbol} {abs(change_pjjc_diff)} ）："
                                    change_message += (
                                        f"{user_pjjc_rank} ➜ {query_pjjc_rank}"
                                    )

                                # 更新用户排名
                                self.pcr_cache[user_id] = (
                                    query_jjc_rank,
                                    query_pjjc_rank,
                                )

                                # 可能涉及监听多用户，变动提醒临时缓存
                                # 因为个人使用，监听用户量少，所以优先遍历完所有用户再统一发送提醒
                                # 如果是监听大量用户，这个地方可能得优化下
                                await self.workwx.send_message(
                                    change_message, delay=True
                                )

                            except Exception as e:
                                # 异常若提示返回标题，则直接重新登录
                                if "回到标题界面" in str(e):
                                    self.user_notify_error_count = 3
                                    break

                                # 记录异常次数
                                self.user_query_error_count += 1
                                if self.user_query_error_count < 3:
                                    # 异常次数在阈值内则先跳过
                                    continue

                                # 异常次数超过阈值则发送通知
                                await self.workwx.send_message(f"用户信息查询失败：{e}")
                                # 并记录上报次数并清空查询错误次数
                                self.user_query_error_count = 0
                                self.user_notify_error_count += 1
                                break

                        # 遍历完所有用户后，存在变动就汇总发送
                        if user_rank_has_changed:
                            await self.workwx.send_message("监听用户存在排名变动")

                        # 查询循环间隔
                        await asyncio.sleep(3)

                except Exception as e:
                    print(f"workwx error: {e}")

                finally:
                    # 登录循环间隔
                    await asyncio.sleep(3)

            except (asyncio.CancelledError, KeyboardInterrupt):
                # 接收到停止意图则传递停止信号
                print(f"pcrclient: receive stop event")
                self.watch_stop()

    def watch_stop(self, *args) -> None:
        """
        通过监听事件，传递停止服务信号
        """
        print("pcrclient: server stoping...")
        self.watch_event.set()
        print("pcrclient: server stoped!")


async def main():
    workwx = WorkWx(os.environ["WORKWX_WEBHOOK"])
    await workwx.send_message("竞技场击剑，启动中")

    pcr_account = {"platform": 2, "channel": 1}
    pcr_account["account"] = os.environ["PCR_USERNAME"]
    pcr_account["password"] = os.environ["PCR_USERPASS"]

    with open(os.environ["PCR_WATCH_PATH"]) as fp:
        pcr_watch_ids = json.load(fp)
        # 将监听用户ID转成整型
        pcr_watch_ids = [int(watch_id) for watch_id in pcr_watch_ids]

    pcr_watcher = PcrWatcher(pcr_account, pcr_watch_ids, workwx)
    await pcr_watcher.watch()


if __name__ == "__main__":
    load_dotenv(override=True)
    asyncio.run(main())
