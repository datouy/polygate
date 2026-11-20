"""客户端治理：虚拟 Key、模型白名单、配额与预算。

网关此前只有一把 ``auth.api_key``：所有业务方共用，无法区分调用来源、
无法限流单个业务方、无法约束成本。本模块提供：

* 虚拟 Key：给每个业务方签发独立 Key（``auth.client_keys``），可随时增删，
  支持热重载（走 ``RuntimeState.reload``，无需重启）。
* 模型白名单：每个 Key 只能调用允许的模型。
* 配额：RPM（内存令牌桶，进程内即时生效）、日调用数、月调用数（查请求日志库）。
* 预算：月累计成本上限；配置 SQLite 时在调用上游前原子预占，与 ``pricing`` 同币种。
* 用量：内存统计（``Stats.calls_by_client``）+ 持久化（requests/costs 表的
  ``client`` 列），面板与 ``/v1/status`` 可见。

持久化口径说明：日/月配额查 SQLite，重启不丢失；RPM 用内存令牌桶，
多进程部署时按「进程数 × 配置值」放大（与上游限流同理，文档注明）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .budget import BudgetLedger, estimate_cost_upper_bound


@dataclass
class ClientPolicy:
    """单个虚拟 Key 的策略。"""

    name: str
    models: set[str] = field(default_factory=set)  # 空 = 允许全部模型
    rpm: float = 0  # 每分钟请求上限，0 = 不限
    rpm_burst: float = 0  # 突发上限，0 = 取 rpm
    daily_calls: int = 0  # 每日逻辑请求上限，0 = 不限
    monthly_calls: int = 0  # 每月逻辑请求上限，0 = 不限
    monthly_budget: float = 0  # 每月成本上限（pricing 同币种），0 = 不限
    id: str = ""  # 托管虚拟 Key ID；兼容 YAML 时为空
    key_prefix: str = ""  # 只用于管理面板识别，不是完整 Key
    expires_at: float | None = None


def parse_client_keys(auth_cfg: dict | None) -> dict[str, ClientPolicy]:
    """解析 ``auth.client_keys``，返回 ``{key: ClientPolicy}``。

    配置示例::

        auth:
          api_key: "change-me"      # 主 Key（兼容保留，client 名记为 default）
          client_keys:
            - key: "sk-biz-a"       # 必填，签发给业务方的 Key
              name: "业务A"          # 可选，面板展示名（默认取 key 脱敏）
              models: ["deepseek-chat"]  # 可选白名单
              rpm: 60               # 可选
              daily_calls: 10000    # 可选
              monthly_budget: 50.0  # 可选
    """
    out: dict[str, ClientPolicy] = {}
    items = (auth_cfg or {}).get("client_keys") or []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        models = item.get("models") or []
        try:
            out[key] = ClientPolicy(
                name=str(item.get("name") or ""),
                models=set(models) if isinstance(models, list) else set(),
                rpm=float(item.get("rpm") or 0),
                rpm_burst=float(item.get("rpm_burst") or item.get("rpm") or 0),
                daily_calls=int(item.get("daily_calls") or 0),
                monthly_calls=int(item.get("monthly_calls") or 0),
                monthly_budget=float(item.get("monthly_budget") or 0),
            )
        except (TypeError, ValueError):
            continue
    return out


def _day_start_ts(now: float | None = None) -> float:
    """今天 0 点（本地时间）的时间戳。"""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, t.tm_wday, t.tm_yday, t.tm_isdst))


def _month_start_ts(now: float | None = None) -> float:
    """本月 1 日 0 点（本地时间）的时间戳。"""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, t.tm_wday, t.tm_yday, t.tm_isdst))


class QuotaChecker:
    """配额与预算检查（无状态，读 DB + 内存限流器）。

    ``store`` / ``cost`` 可为 None（未配置 db_path 时）：此时日/月配额与预算
    无法跨重启累计，降级为只检查 RPM 与模型白名单，并在 ``/v1/status`` 中
    明确标注，避免运营误以为配额生效。
    """

    def __init__(self, limiter=None, store=None, cost=None, budget: BudgetLedger | None = None):
        self._limiter = limiter
        self._store = store
        self._cost = cost
        self._budget = budget
        self._usage_lock = threading.Lock()
        self._usage_cache: dict[str, dict] = {}

    def _usage_for(self, client: str, refresh: bool = False) -> dict:
        """按客户端/日/月加载持久化调用统计，再在进程内累加。

        日/月调用次数的进程内缓存不是跨进程原子计数器；预算则另走
        :class:`BudgetLedger`，通过共享 SQLite 租约保证跨进程预占原子性。
        """
        day = _day_start_ts()
        month = _month_start_ts()
        if not refresh:
            with self._usage_lock:
                cached = self._usage_cache.get(client)
                if cached and cached["day"] == day and cached["month"] == month:
                    return cached
        loaded = {"day": day, "month": month, "daily": 0, "monthly": 0, "spent": 0.0}
        if self._store is not None:
            loaded["daily"] = self._store.count_requests(client=client, since=day)
            loaded["monthly"] = self._store.count_requests(client=client, since=month)
        if self._cost is not None:
            loaded["spent"] = self._cost.spend_since(client=client, since=month)
        with self._usage_lock:
            if not refresh:
                current = self._usage_cache.get(client)
                if current and current["day"] == day and current["month"] == month:
                    return current
            self._usage_cache[client] = loaded
            return loaded

    def reserve(self, policy: ClientPolicy, client: str) -> str | None:
        """Atomically reserve one logical request before checking its limits."""
        if policy is None or (not policy.daily_calls and not policy.monthly_calls and not policy.monthly_budget):
            return None
        usage = self._usage_for(client)
        with self._usage_lock:
            if policy.daily_calls > 0 and usage["daily"] >= policy.daily_calls:
                return f"client daily quota exceeded ({usage['daily']}/{policy.daily_calls})"
            if policy.monthly_calls > 0 and usage["monthly"] >= policy.monthly_calls:
                return f"client monthly quota exceeded ({usage['monthly']}/{policy.monthly_calls})"
            if self._budget is None and policy.monthly_budget > 0 and usage["spent"] >= policy.monthly_budget:
                return f"client monthly budget exceeded ({usage['spent']:.4f}/{policy.monthly_budget:g})"
            # Failed calls still consume a logical request quota, matching the
            # pre-existing request-log based semantics.
            usage["daily"] += 1
            usage["monthly"] += 1
        return None

    def reserve_budget(
        self,
        policy: ClientPolicy,
        client: str,
        model: str,
        payload: dict,
        pricing: dict,
        settings: dict | None,
        *,
        completion_is_relevant: bool = True,
    ) -> tuple[str | None, dict | None, dict]:
        """按成本上限预占预算；返回 (拒绝原因, ticket, 当前用量)。"""
        if self._budget is None or policy is None or not policy.monthly_budget or policy.monthly_budget <= 0:
            return None, None, {}
        cfg = settings or {}
        estimate = estimate_cost_upper_bound(
            payload,
            pricing,
            model,
            default_max_output_tokens=int(cfg.get("default_max_output_tokens", 4096)),
            bytes_per_token=float(cfg.get("bytes_per_token", 2.0)),
            completion_is_relevant=completion_is_relevant,
        )
        period = _month_start_ts()
        baseline = self._cost.spend_since(client=client, since=period) if self._cost is not None else 0.0
        if estimate <= 0:
            info = self._budget.usage(client, period, baseline)
            return None, None, info
        reservation = self._budget.reserve(
            client=client,
            period_start=period,
            limit=policy.monthly_budget,
            reserved_cost=estimate,
            baseline_cost=baseline,
            ttl_seconds=float(cfg.get("reservation_ttl", 900.0)),
        )
        info = {
            "used": reservation.used,
            "limit": reservation.limit,
            "reserved": reservation.reserved,
            "estimate": estimate,
        }
        if reservation.reason or reservation.lease is None:
            return reservation.reason or "client monthly budget exceeded", None, info
        return None, {
            "id": reservation.lease.id,
            "reserved": reservation.lease.reserved_cost,
            "expires_at": reservation.lease.expires_at,
        }, info

    def settle_budget(self, ticket: dict | None, actual_cost: float) -> None:
        if self._budget is None or not ticket:
            return
        self._budget.settle(str(ticket.get("id") or ""), max(0.0, float(actual_cost)))

    def release_budget(self, ticket: dict | None, actual_cost: float = 0.0) -> None:
        if self._budget is None or not ticket:
            return
        self._budget.settle(str(ticket.get("id") or ""), max(0.0, float(actual_cost)), released=True)

    def settle_cost(self, client: str, amount: float) -> None:
        if not client or amount <= 0:
            return
        usage = self._usage_for(client)
        with self._usage_lock:
            usage["spent"] += amount

    def reset_cached_usage(self) -> None:
        with self._usage_lock:
            self._usage_cache.clear()

    def check_model(self, policy: ClientPolicy, model: str) -> str | None:
        """模型白名单检查；返回 None 表示通过，否则返回拒绝原因。"""
        if policy.models and model not in policy.models:
            return f"model '{model}' not allowed for this client key"
        return None

    def check_rpm(self, policy: ClientPolicy, client: str, limiter=None) -> str | None:
        """RPM 检查（内存令牌桶）；返回 None 表示通过。"""
        limiter = limiter if limiter is not None else self._limiter
        if not policy.rpm or policy.rpm <= 0 or limiter is None:
            return None
        burst = policy.rpm_burst if policy.rpm_burst > 0 else policy.rpm
        if not limiter.allow(f"client-rpm:{client}", policy.rpm / 60.0, burst):
            return f"client rpm limit exceeded ({policy.rpm:g}/min)"
        return None

    def check_daily(self, policy: ClientPolicy, client: str) -> str | None:
        if not policy.daily_calls or policy.daily_calls <= 0:
            return None
        if self._store is None:
            return None
        used = self._usage_for(client, refresh=True)["daily"]
        if used >= policy.daily_calls:
            return f"client daily quota exceeded ({used}/{policy.daily_calls})"
        return None

    def check_monthly_calls(self, policy: ClientPolicy, client: str) -> str | None:
        if not policy.monthly_calls or policy.monthly_calls <= 0:
            return None
        if self._store is None:
            return None
        used = self._usage_for(client, refresh=True)["monthly"]
        if used >= policy.monthly_calls:
            return f"client monthly quota exceeded ({used}/{policy.monthly_calls})"
        return None

    def check_budget(self, policy: ClientPolicy, client: str) -> str | None:
        if not policy.monthly_budget or policy.monthly_budget <= 0:
            return None
        if self._cost is None:
            return None
        if self._budget is not None:
            period = _month_start_ts()
            baseline = self._cost.spend_since(client=client, since=period)
            used = self._budget.usage(client, period, baseline)["used"]
            if used >= policy.monthly_budget:
                return f"client monthly budget exceeded ({used:.4f}/{policy.monthly_budget:g})"
            return None
        spent = self._usage_for(client, refresh=True)["spent"]
        if spent >= policy.monthly_budget:
            return f"client monthly budget exceeded ({spent:.4f}/{policy.monthly_budget:g})"
        return None

    def check_all(self, policy: ClientPolicy, client: str, model: str) -> str | None:
        """依次执行全部检查，返回第一条拒绝原因；全部通过返回 None。"""
        checks = (
            self.check_model(policy, model),
            self.check_rpm(policy, client),
            self.check_daily(policy, client),
            self.check_monthly_calls(policy, client),
            self.check_budget(policy, client),
        )
        for reason in checks:
            if isinstance(reason, str):
                return reason
        return None

    def usage(self, policy: ClientPolicy, client: str) -> dict:
        """返回配额/预算的当前用量（供面板展示）；无持久化后端时返回空 dict。"""
        out: dict = {}
        usage = self._usage_for(client)
        if self._store is not None and policy.daily_calls:
            out["daily_calls"] = {
                "used": usage["daily"],
                "limit": policy.daily_calls,
            }
        if self._store is not None and policy.monthly_calls:
            out["monthly_calls"] = {
                "used": usage["monthly"],
                "limit": policy.monthly_calls,
            }
        if self._cost is not None and policy.monthly_budget:
            if self._budget is not None:
                period = _month_start_ts()
                baseline = self._cost.spend_since(client=client, since=period)
                budget = self._budget.usage(client, period, baseline)
                out["monthly_budget"] = {
                    "used": round(budget["used"], 6),
                    "settled": round(budget["settled"], 6),
                    "reserved": round(budget["reserved"], 6),
                    "limit": policy.monthly_budget,
                }
            else:
                out["monthly_budget"] = {
                    "used": round(usage["spent"], 6),
                    "limit": policy.monthly_budget,
                }
        return out
