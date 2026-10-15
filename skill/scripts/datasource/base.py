"""数据源抽象基类与注册表。"""

from __future__ import annotations

import abc
from typing import Optional

from .schema import Result, SourceName


class DataSourceError(Exception):
    """数据源错误（网络/权限/字段缺失）。"""


class DataSource(abc.ABC):
    """数据源适配器基类。

    子类只需实现 `fetch()`（返回原始 DataFrame/dict 列表），
    映射与元信息封装由 `fetch_dataset()` 统一处理。
    """

    name: SourceName = "tushare"
    priority: int = 100          # 越小越优先（用于自动降级）
    supports_hk: bool = False
    supports_us: bool = False

    @abc.abstractmethod
    def available(self) -> bool:
        """该源当前是否可用（已配置 Token 等）。"""

    @abc.abstractmethod
    def fetch(
        self,
        api: str,
        code: str,
        *,
        periods: Optional[list[str]] = None,
        limit: int = 100,
        **kwargs,
    ) -> list[dict]:
        """取原始数据。返回 [{源字段名: 值}]，字段名是**该源的原生名**。"""

    def field_overrides(self, dataset_name: str, rows: list[dict]) -> dict[str, str]:
        """可选：根据实际返回行动态覆盖字段映射（业务名 -> 该源字段名）。

        用例：AkShare 现金流量表走东财源时列名是英文，走新浪源时是中文。
        """
        return {}

    def fetch_dataset(self, dataset_name: str, code: str, **kwargs) -> Result:
        """取数据集并按统一 schema 映射。业务代码用这个。"""
        from .schema import get_dataset
        from .mapper import map_records

        ds = get_dataset(dataset_name)

        if code.endswith(".HK") and not self.supports_hk:
            raise DataSourceError(f"{self.name} 不支持港股（{code}）")
        if code.endswith(".US") and not self.supports_us:
            raise DataSourceError(f"{self.name} 不支持美股（{code}）")

        raw = self.fetch(ds.tushare_api, code, **kwargs)
        overrides = self.field_overrides(dataset_name, raw)
        records = map_records(raw, ds, self.name, overrides=overrides)
        warnings: list[str] = []
        if not records:
            warnings.append(f"{self.name} 返回空数据（{dataset_name} @ {code}）")

        import datetime as _dt
        return Result(
            dataset=dataset_name,
            source=self.name,
            records=records,
            fetched_at=_dt.datetime.now().isoformat(timespec="seconds"),
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, DataSource] = {}


def register(source):
    """注册适配器。可传类（自动实例化）或实例。"""
    inst = source() if isinstance(source, type) else source
    _REGISTRY[inst.name] = inst
    return source


def get_adapter(name: str) -> DataSource:
    _ensure_loaded()
    if name not in _REGISTRY:
        raise DataSourceError(f"未注册的数据源 {name!r}；可用：{list(_REGISTRY)}")
    return _REGISTRY[name]


def list_sources(only_available: bool = False) -> list[str]:
    _ensure_loaded()
    out = []
    for name, src in sorted(_REGISTRY.items(), key=lambda kv: kv[1].priority):
        if only_available and not src.available():
            continue
        out.append(name)
    return out


_LOADED = False


def _ensure_loaded() -> None:
    """惰性导入所有适配器（避免循环导入）。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from .adapters import tushare_adapter  # noqa: F401
    from .adapters import zzshare_adapter  # noqa: F401
    from .adapters import akshare_adapter  # noqa: F401
