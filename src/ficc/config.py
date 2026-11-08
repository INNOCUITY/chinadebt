"""配置加载与校验。

设计原则
--------
1. **所有金融口径参数只在 `config/*.yaml` 里**，代码中不得硬编码数值。
2. 用 pydantic 做**严格校验**（`extra="forbid"`）：YAML 里多一个拼写错误的键
   就会立刻报错，而不是静默生效。
3. 配置对象是**不可变**的（frozen），避免运行期被意外改写。
4. `Config.hash()` 给出配置指纹，参与 `run_id` 计算，保证回测可复现。

用法
----
>>> from ficc.config import get_config
>>> cfg = get_config()
>>> cfg.backtest.start_date
datetime.date(2019, 1, 28)
>>> cfg.paths.abs("processed")          # 绝对路径
PosixPath('/path/to/china_debt/data/processed')
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 项目根目录：src/ficc/config.py -> parents[0]=ficc, [1]=src, [2]=项目根
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "config.yaml"


class _Model(BaseModel):
    """所有配置模型的基类：严格、不可变、禁止多余键。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# 各配置节
# --------------------------------------------------------------------------
class ProjectConfig(_Model):
    name: str
    seed: int = 42
    timezone: str = "Asia/Shanghai"


class PathsConfig(_Model):
    raw: str = "data/raw"
    interim: str = "data/interim"
    processed: str = "data/processed"
    metadata: str = "data/metadata"
    artifacts: str = "artifacts"
    reports: str = "reports"

    def abs(self, key: str) -> Path:
        """把配置里的相对路径解析为绝对路径（相对项目根）。"""
        if key not in self.model_dump():
            raise KeyError(f"未知路径键: {key!r}，可用: {sorted(self.model_dump())}")
        p = Path(getattr(self, key))
        return p if p.is_absolute() else PROJECT_ROOT / p

    def ensure(self) -> None:
        """创建所有输出目录（幂等）。"""
        for key in self.model_dump():
            self.abs(key).mkdir(parents=True, exist_ok=True)


class HttpConfig(_Model):
    timeout_s: float = 40.0
    retries: int = 3
    backoff_s: float = 1.5
    throttle_s: float = 0.0
    user_agent: str = "Mozilla/5.0"


class DataConfig(_Model):
    primary_source: str = "chinabond"
    fallback_sources: list[str] = Field(default_factory=lambda: ["akshare", "eastmoney"])
    curve_name: str = "中债国债收益率曲线(到期)"
    yc_def_id: str = "2c9081e50a2f9606010a3068cae70001"
    start_date: date
    end_date: date | None = None
    tenors: list[float]
    known_gaps: list[dict[str, Any]] = Field(default_factory=list)
    trading_calendar: str = "CN_IB"
    refresh: str = "daily"
    #: 数据新鲜度阈值（自然日）：最新数据落后今天超过该值则告警（R06）
    max_staleness_days: int = Field(default=5, ge=0)
    #: 在线抓取时是否强制刷新「当年」文件（历史完整年度复用不可变快照，R06）
    refresh_current_year: bool = True
    http: HttpConfig = Field(default_factory=HttpConfig)
    save_raw_xlsx: bool = True

    @field_validator("tenors")
    @classmethod
    def _check_tenors(cls, v: list[float]) -> list[float]:
        if len(v) < 4:
            raise ValueError("tenors 至少需要 4 个期限点才能拟合曲线")
        if any(t <= 0 for t in v):
            raise ValueError("tenors 必须为正数")
        if sorted(v) != list(v):
            raise ValueError("tenors 必须严格递增")
        return v


class BackfillConfig(_Model):
    enabled: bool = False
    tenors: list[float] = Field(default_factory=lambda: [2.0])
    start: date
    end: date
    throttle_s: float = 0.3
    validate_against: float = 10.0

    @field_validator("end")
    @classmethod
    def _check_range(cls, v: date, info: Any) -> date:
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError(f"backfill.end({v}) 不能早于 start({start})")
        return v


class CurveConfig(_Model):
    fit_method: Literal["linear", "hermite", "ns", "nss"] = "nss"
    ns_tau0: float = 1.5
    nss_tau1: float = 6.0
    weights: str = "inverse_maturity_bucket"
    smoothness_lambda: float = 0.0
    anchor_short_end: bool = True
    min_valid_tenors: int = 4
    pca_tenors: list[float]
    pca_window: int = 250


class ReconciliationConfig(_Model):
    enabled: bool = True
    max_price_error_cny: float = 0.05


class PricingConfig(_Model):
    day_count: str = "ACT/ACT"
    day_count_exchange: str = "ACT/365"
    face_value: float = 100.0
    settlement_lag_days: int = 1
    price_convention: str = "clean_quote_full_pnl"
    frequency: int = 2
    frequency_by_tenor: dict[str, int | str]
    accrual_formula: dict[str, str]
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)


class MarketImpactConfig(_Model):
    enabled: bool = False
    coef_bp_per_pct_adv: float = 0.0


class CostsConfig(_Model):
    fee_bp: float = 0.2
    settlement_fee_cny_per_trade: float = 135.0
    spread_bp_active: float = 0.35
    spread_bp_illiquid: float = 1.5
    spread_bp_ultra_long: float = 3.0
    repo_rate_source: Literal["FDR007", "FR007", "const"] = "FDR007"
    repo_rate_fallback_const: float = 0.01318
    repo_rate_sensitivity_bp: list[float] = Field(default_factory=lambda: [0.0, 25.0, 50.0])
    repo_haircut: float = 0.0
    short_leg_cost_bp: float = 0.0
    market_impact: MarketImpactConfig = Field(default_factory=MarketImpactConfig)


class SyntheticConfig(_Model):
    face_value: float = 100.0
    coupon_rule: str = "par"
    maturities: list[float]


class RollingConfig(_Model):
    """合成券滚动政策（§12.12-B 冻结版，roll_v1）。

    - 触发：任一腿**剩余期限 < roll_trigger_years** 时，在该交易日显式卖出旧券并
      平价发行新券（票面=当日该期限曲线 YTM、起息=触发日、到期=起息+票面期限）；
    - 版本号写入报告，滚动前后基准不可混比（新的滚动基准单独版本化）。
    """

    enabled: bool = False
    roll_trigger_years: float = 1.0
    version: str = "roll_v1_trigger1y_parissue"

    @field_validator("roll_trigger_years")
    @classmethod
    def _check_trigger(cls, v: float) -> float:
        if not (v > 0.0):
            raise ValueError("roll_trigger_years 必须为正数")
        return float(v)


class PortfolioConfig(_Model):
    short_leg_mode: Literal["over_under_weight", "futures", "irs"] = "over_under_weight"
    allow_short_cash_bond: bool = False
    max_single_bond_weight: float = 0.3
    # 可交易合成券期限（与 synthetic.maturities 对应；基准与策略共用同一宇宙）
    universe: list[float] = Field(default_factory=lambda: [2.0, 5.0, 10.0, 30.0])
    rolling: RollingConfig = Field(default_factory=RollingConfig)
    synthetic: SyntheticConfig

    @field_validator("universe")
    @classmethod
    def _check_universe(cls, v: list[float]) -> list[float]:
        if len(v) < 2:
            raise ValueError("universe 至少需要 2 个期限才能做曲线交易")
        if any(t <= 0 for t in v):
            raise ValueError("universe 期限必须为正数")
        if sorted(v) != list(v):
            raise ValueError("universe 必须严格递增")
        return v


class ConstraintsConfig(_Model):
    """组合约束（Phase 2 第 3 步）。默认值即"相对基准 DV01 中性"的验收口径。"""

    # 相对基准 DV01 中性：单桶 |KRD_p − KRD_b| ≤ relative_krd_tol × 基准总 DV01
    relative_krd_tol: float = 0.30
    # 总 DV01 相对基准的允许区间（倍数）
    total_dv01_ratio_min: float = 0.70
    total_dv01_ratio_max: float = 1.30
    # 组合久期相对基准的允许偏离（相对值，0.20 = ±20%）
    duration_tol: float = 0.20
    # 现金预算（占 NAV 比例）：负值 = 允许的回购融资上限。
    # 例：-0.20 表示现金不得低于 -20% × NAV（约 0.2 倍杠杆由回购支撑）。
    min_cash_fraction: float = -0.20
    # 最小成交面值（元）：低于该值不做调仓，避免碎单
    min_trade_lot: float = 1_000_000.0
    # 调仓死区（单位 lot）：|Δface| 小于该值不交易，抑制"老化/取整"造成的日常抖单
    rebalance_band_lots: float = 2.0
    # 允许的日换手率上限（成交面值 / NAV），超过则报警
    max_daily_turnover: float = 2.0


class FuturesConfig(_Model):
    enabled: bool = False
    margin_ratio: dict[str, float] = Field(default_factory=dict)
    broker_margin_addon: float = 0.025
    multiplier_cny_per_point: float = 10_000.0
    fee_cny_per_lot: float = 3.0
    ctd_switch_model: str = "simple"


class BacktestConfig(_Model):
    start_date: date
    end_date: date | None = None
    execution: Literal["next_close", "next_open"] = "next_close"
    signal_lag_days: int = 1
    annual_days: int = 250
    accrual_days: int = 365
    init_cash: float = 1e8
    max_leverage: float = 2.0
    rebalance: Literal["daily", "weekly"] = "daily"
    # 基准版本：buyhold（原买入持有固定面值）| rolling（滚动阶梯，单独版本化）
    benchmark_version: Literal["buyhold", "rolling"] = "buyhold"
    roll_down_step_years: float = 1.0

    @field_validator("end_date")
    @classmethod
    def _check_range(cls, v: date | None, info: Any) -> date | None:
        start = info.data.get("start_date")
        if v is not None and start is not None and v <= start:
            raise ValueError(f"backtest.end_date({v}) 必须晚于 start_date({start})")
        return v


class SteepenerConfig(_Model):
    short_tenor: float
    long_tenor: float
    zscore_window: int = 60
    entry_z: float = 1.0
    exit_z: float = 0.25
    target_dv01: float = 1e5
    dv01_neutral: bool = True


class CarryRollConfig(_Model):
    holding_tenor_band: list[float]
    horizon_days: int = 20
    top_k: int = 2
    roll_target: str = "tenor_minus_1y"


class RelativeValueConfig(_Model):
    zscore_window: int = 120
    entry_z: float = 1.5
    exit_z: float = 0.5
    max_position_per_tenor: float = 0.4


class StrategyConfig(_Model):
    steepener: SteepenerConfig
    carry_rolldown: CarryRollConfig
    relative_value: RelativeValueConfig


class RiskLimitsConfig(_Model):
    max_dv01: float = 5e5
    max_duration: float = 8.0
    max_krd_share: float = 0.5
    max_single_bond_weight: float = 0.3
    max_drawdown_stop: float = 0.05


class RiskConfig(_Model):
    dv01_basis: str = "full_price_per_100_face"
    dv01_unit: str = "CNY_per_bp_per_100_face"
    krd_method: str = "bootstrap_spot_then_shift_each_key_tenor"
    key_tenors_krd: list[float]
    convexity_basis: str = "full_price_per_100_face"
    confidence: list[float]
    var_method: Literal["historical", "parametric"] = "historical"
    var_window: int = 250
    var_backtest: str = "kupiec"
    limits: RiskLimitsConfig
    stress_scenarios: list[dict[str, Any]] = Field(default_factory=list)


class BenchmarkConfig(_Model):
    # 基准阶梯：按**市值**等权配置到可交易宇宙的各期限
    ladder_weights: dict[float, float] = Field(default_factory=dict)
    notional: float = 1e8
    index_name: str
    index_code: str
    index_code_1_3y: str | None = None
    index_code_7_10y: str | None = None
    use_index_benchmark: bool = True
    risk_free: str = "DR007"
    risk_free_fallback_const: float = 0.01318


class ReportConfig(_Model):
    language: Literal["zh", "en", "both"] = "zh"
    figures_format: Literal["png", "svg", "pdf"] = "png"
    dpi: int = 150


# --------------------------------------------------------------------------
# 顶层配置
# --------------------------------------------------------------------------
class Config(_Model):
    project: ProjectConfig
    paths: PathsConfig
    data: DataConfig
    backfill: BackfillConfig
    curve: CurveConfig
    pricing: PricingConfig
    costs: CostsConfig
    portfolio: PortfolioConfig
    constraints: ConstraintsConfig
    futures: FuturesConfig
    backtest: BacktestConfig
    strategy: StrategyConfig
    risk: RiskConfig
    benchmark: BenchmarkConfig
    report: ReportConfig

    # ---- 派生属性 -------------------------------------------------------
    @property
    def root(self) -> Path:
        return PROJECT_ROOT

    def canonical_json(self) -> str:
        """规范化 JSON（键排序、日期转 ISO），用于指纹计算。"""
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )

    def hash(self) -> str:
        """配置指纹（sha256 前 16 位）。参与 run_id，保证可复现。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()[:16]


def load_config(path: str | Path | None = None) -> Config:
    """从 YAML 读取并校验配置。异常信息包含具体字段路径。"""
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件顶层必须是映射(dict)，实际为 {type(raw).__name__}")
    try:
        return Config.model_validate(raw)
    except Exception as exc:  # pragma: no cover - 仅用于包装错误信息
        raise ValueError(f"配置校验失败 ({cfg_path}):\n{exc}") from exc


@lru_cache(maxsize=8)
def get_config(path: str | None = None) -> Config:
    """带缓存的配置读取。同一进程内多次调用只解析一次。"""
    return load_config(path)


def clear_config_cache() -> None:
    """测试用：清空配置缓存。"""
    get_config.cache_clear()
