"""
外形監視の検証

マクドナルド側が落ちていることに、利用者からの報告で気付くのでは遅い。
状態が変わったときだけ通知すること（毎回出すと本当の異常が埋もれる）も
併せて確かめる。
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import config
from core import breaker
from services import monitor

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"  ✅ {name}")
    else:    fail += 1; print(f"  ❌ {name}  {extra}")


class World:
    def __init__(self):
        self.down = set()
        self.status = 200


class FakeClient:
    def __init__(self, w): self.w = w
    async def get(self, url, timeout=None):
        g = url.split("data.cat.")[1].split(".")[0]
        if g in self.w.down:
            raise httpx.ConnectError("接続できません")
        return httpx.Response(self.w.status, json={},
                              request=httpx.Request("GET", url))


def install(w):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session():
        yield FakeClient(w)
    monitor.catalog_session = session


async def main():
    w = World()
    install(w)
    monitor.reset()
    breaker.groups.reset_all()

    print("\n[1] 全部生きているとき")
    r = await monitor.check()
    check("全グループを見る", len(r.results) == len(config.MCD_GROUPS), len(r.results))
    check("全部正常", r.healthy == len(r.results), r.healthy)
    check("変化が無ければ通知しない ★", monitor.format_report(r) == "", monitor.format_report(r))
    check("応答時間を測る", all(h.latency_ms >= 0 for h in r.results))

    print("\n[2] 1回の失敗では騒がない ★")
    w.down = {"group-f"}
    r = await monitor.check()
    check("まだ落ちたと判断しない ★", not r.became_down, [h.name for h in r.became_down])
    check("通知しない", monitor.format_report(r) == "")

    print("\n[3] 続けて失敗したら知らせる")
    r = await monitor.check()
    check("落ちたと判断する", [h.name for h in r.became_down] == ["group-f"],
          [h.name for h in r.became_down])
    text = monitor.format_report(r)
    check("通知文に名前が入る", "group-f" in text, text[:80])
    check("理由も入る", "ConnectError" in text, text[:120])

    print("\n[4] 落ちたままなら重ねて通知しない ★")
    r = await monitor.check()
    check("2回目は通知しない ★", monitor.format_report(r) == "", monitor.format_report(r))
    check("状態は落ちたまま",
          monitor._state["group-f"].state == monitor.DOWN,
          monitor._state["group-f"].state)

    print("\n[5] 復帰したら知らせる")
    w.down = set()
    r = await monitor.check()
    check("復帰を検出する", [h.name for h in r.became_up] == ["group-f"],
          [h.name for h in r.became_up])
    check("通知文に出る", "復帰" in monitor.format_report(r))
    r = await monitor.check()
    check("復帰後は重ねて通知しない", monitor.format_report(r) == "")

    print("\n[6] 復帰したら止めていた経路も戻す ★")
    monitor.reset(); breaker.groups.reset_all()
    for _ in range(breaker.FAILURES_TO_OPEN):
        breaker.groups.record_failure("group-h", "timeout")
    check("経路が止まっている", not breaker.groups.allows("group-h"))
    w.down = {"group-h"}
    await monitor.check(); await monitor.check()
    w.down = set()
    await monitor.check()
    check("復帰で経路も戻る ★", breaker.groups.allows("group-h"))

    print("\n[7] 5xx は落ちている扱い、4xx は生きている扱い")
    monitor.reset()
    w.status = 404
    await monitor.check(); r = await monitor.check()
    check("404 でもサーバーは動いていると見る", r.healthy == len(r.results), r.healthy)
    w.status = 503
    await monitor.check(); r = await monitor.check()
    check("503 は落ちている扱い", r.healthy == 0, r.healthy)
    check("全部落ちたと分かる", r.all_down)
    check("通知文で強く知らせる", "すべての配信元" in monitor.format_report(r))
    w.status = 200

    print("\n[8] 最後に応答した時刻を覚えている")
    monitor.reset()
    await monitor.check()
    last = monitor._state["group-f"].last_ok
    check("時刻が入る", last > 0, last)
    w.down = {"group-f"}
    await monitor.check(); await monitor.check()
    check("落ちても最後の時刻は残る",
          monitor._state["group-f"].last_ok == last,
          monitor._state["group-f"].last_ok)
    w.down = set()

    print("\n[9] 注文に影響しない作りか")
    import inspect
    src = inspect.getsource(monitor)
    check("認証を使わない", "ensure_auth" not in src and "pick_account" not in src)
    check("読み取りだけ", ".post(" not in src)
    check("一覧を確認できる", len(monitor.snapshot()) == len(config.MCD_GROUPS))

    print(f"\n{'='*46}\n  成功 {ok} 件 / 失敗 {fail} 件\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
