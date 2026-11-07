"""``Curve`` 对象的单元测试：求值、插值/外推、远期、冲击与校验。

测试口径
--------
- 收益率单位百分数，期限单位年；
- 冲击幅度单位 bp（``100bp = 1.00%``）；
- 全部断言都是**确定性的**，不依赖随机数与网络。
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from datetime import datetime as _datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.config import get_config
from ficc.curve._common import ns_loadings, ns_rate, nss_rate
from ficc.curve.bias import bias_verdict, hac_t_statistics, run_bias
from ficc.curve.curve import Curve
from ficc.curve.products import build_fit_products, read_fit_meta, validate_fit_meta
from ficc.curve.reporting import build_reports, long_frame_to_panel
from ficc.data.schema import CANONICAL_TENORS, DataValidationError

TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
RATES = np.array([1.50, 1.55, 1.62, 1.72, 1.82, 1.98, 2.10, 2.22, 2.30, 2.34, 2.42])
AS_OF = date(2024, 6, 28)

# 一条形态"正常"（正斜率、微凸）的 NSS 曲线参数
NSS_TRUE = np.array([3.20, -1.80, 1.50, -0.80, 1.80, 7.0])
NSS_PARAMS = {
    "b0": 3.20,
    "b1": -1.80,
    "b2": 1.50,
    "b3": -0.80,
    "tau1": 1.80,
    "tau2": 7.0,
}


@pytest.fixture
def linear_curve() -> Curve:
    return Curve(as_of=AS_OF, tenors=TENORS, rates=RATES, method="linear")


@pytest.fixture
def hermite_curve() -> Curve:
    return Curve(as_of=AS_OF, tenors=TENORS, rates=RATES, method="hermite")


@pytest.fixture
def nss_curve() -> Curve:
    rates = nss_rate(NSS_TRUE, TENORS)
    return Curve(as_of=AS_OF, tenors=TENORS, rates=rates, method="nss", params=NSS_PARAMS)


# ---------------------------------------------------------------------------
# 构造与校验
# ---------------------------------------------------------------------------
def test_rate_on_nodes_is_exact(linear_curve: Curve, hermite_curve: Curve) -> None:
    """节点上的求值必须严格等于存储的 rates（不能有插值误差）。"""
    for curve in (linear_curve, hermite_curve):
        np.testing.assert_allclose(curve.rate(TENORS), RATES, rtol=0, atol=0)


def test_rate_scalar_and_array_shapes(hermite_curve: Curve) -> None:
    assert isinstance(hermite_curve.rate(5.0), float)
    out = hermite_curve.rate(np.array([1.0, 5.0, 10.0]))
    assert isinstance(out, np.ndarray)
    assert out.shape == (3,)


def test_rate_at_zero_tenor_is_finite(hermite_curve: Curve, nss_curve: Curve) -> None:
    """t→0 不能出现 0/0；参数化曲线在 0 处应等于 b0+b1。"""
    assert np.isfinite(linear(hermite_curve.rate(1e-9)))
    assert np.isfinite(hermite_curve.rate(0.0))
    assert nss_curve.rate(0.0) == pytest.approx(NSS_PARAMS["b0"] + NSS_PARAMS["b1"], abs=1e-8)


def linear(x: float) -> float:
    return float(x)


def test_interpolated_curve_is_monotone(linear_curve: Curve, hermite_curve: Curve) -> None:
    """节点间单调：给定单调上升的节点收益率，插值后仍单调不减。"""
    dense = np.linspace(TENORS[0], TENORS[-1], 500)
    for curve in (linear_curve, hermite_curve):
        values = np.asarray(curve.rate(dense))
        assert np.all(np.diff(values) >= -1e-12)


def test_hermite_does_not_overshoot(hermite_curve: Curve) -> None:
    """单调三次 Hermite 不应在节点间冲出节点值范围（PCHIP 的性质）。"""
    dense = np.linspace(TENORS[0], TENORS[-1], 2000)
    values = np.asarray(hermite_curve.rate(dense))
    assert values.min() >= RATES.min() - 1e-9
    assert values.max() <= RATES.max() + 1e-9


def test_extrapolation_never_negative(linear_curve: Curve, hermite_curve: Curve) -> None:
    """两端外推用端点斜率线性延伸，且不得产生负利率。"""
    for curve in (linear_curve, hermite_curve):
        short = np.asarray(curve.rate(np.array([0.001, 0.01, 0.1])))
        long = np.asarray(curve.rate(np.array([40.0, 50.0, 100.0])))
        assert np.all(short >= 0.0)
        assert np.all(long >= 0.0)
        # 外推是线性延伸（不是常数外推）：斜率应延续端点区间斜率
        slope = (RATES[1] - RATES[0]) / (TENORS[1] - TENORS[0])
        expected = RATES[0] + slope * (0.01 - TENORS[0])
        assert curve.rate(0.01) == pytest.approx(expected, rel=1e-12)


def test_invalid_construction() -> None:
    with pytest.raises(ValueError, match="严格递增"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0, 2.0]), rates=np.array([1.0, 2.0, 3.0]))
    with pytest.raises(ValueError, match="长度不一致"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0]), rates=np.array([1.0]))
    with pytest.raises(ValueError, match="NaN"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0]), rates=np.array([1.0, np.nan]))
    with pytest.raises(ValueError, match="method"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0]), rates=np.array([1.0, 2.0]), method="cubic")
    with pytest.raises(ValueError, match="至少需要 2 个"):
        Curve(as_of=AS_OF, tenors=np.array([1.0]), rates=np.array([1.0]))
    with pytest.raises(ValueError, match="正数"):
        Curve(as_of=AS_OF, tenors=np.array([-1.0, 2.0]), rates=np.array([1.0, 2.0]))
    with pytest.raises(TypeError, match="as_of"):
        Curve(as_of="2024-06-28", tenors=np.array([1.0, 2.0]), rates=np.array([1.0, 2.0]))


def test_datetime_as_of_is_converted() -> None:
    curve = Curve(as_of=datetime(2024, 6, 28, 17, 30), tenors=TENORS, rates=RATES, method="linear")
    assert curve.as_of == date(2024, 6, 28)
    assert isinstance(curve.as_of, date) and not isinstance(curve.as_of, datetime)


# ---------------------------------------------------------------------------
# 远期
# ---------------------------------------------------------------------------
def test_forward_formula() -> None:
    curve = Curve(as_of=AS_OF, tenors=np.array([5.0, 10.0]), rates=np.array([2.0, 2.2]))
    expected = (2.2 * 10.0 - 2.0 * 5.0) / (10.0 - 5.0)
    assert curve.forward(5.0, 10.0) == pytest.approx(expected, rel=1e-12)
    assert curve.forward(0.0, 5.0) == pytest.approx(2.0, rel=1e-12)


def test_forward_validation(linear_curve: Curve) -> None:
    with pytest.raises(ValueError, match="t2"):
        linear_curve.forward(5.0, 5.0)
    with pytest.raises(ValueError, match="t2"):
        linear_curve.forward(10.0, 5.0)
    with pytest.raises(ValueError, match="t1"):
        linear_curve.forward(-1.0, 5.0)


# ---------------------------------------------------------------------------
# 平行移动与情景冲击
# ---------------------------------------------------------------------------
def test_shift_parallel_exact(linear_curve: Curve) -> None:
    shifted = linear_curve.shift(25.0)
    np.testing.assert_allclose(shifted.rate(TENORS), RATES + 0.25, atol=1e-12)
    # shift 按"平移量"语义接受负数（与 shock 的"幅度非负"区分，R10）
    down = linear_curve.shift(-25.0)
    np.testing.assert_allclose(down.rate(TENORS), RATES - 0.25, atol=1e-12)
    # 原曲线不可变
    np.testing.assert_allclose(linear_curve.rate(TENORS), RATES, atol=0)


def test_shock_parallel_100bp(linear_curve: Curve, hermite_curve: Curve) -> None:
    """shock("parallel", 100) 后所有点 +1.00%。"""
    for curve in (linear_curve, hermite_curve):
        shocked = curve.shock("parallel", 100.0)
        delta = np.asarray(shocked.rate(TENORS)) - np.asarray(curve.rate(TENORS))
        np.testing.assert_allclose(delta, 1.00, atol=1e-12)


def test_shock_steepen_directions(linear_curve: Curve, nss_curve: Curve) -> None:
    """陡峭化：短端向下、长端向上（长短端方向必须正确）。"""
    a = linear_curve.shock("steepen", 25.0)
    short_delta, long_delta = (
        linear_curve.rate(0.25) - a.rate(0.25),
        a.rate(30.0) - linear_curve.rate(30.0),
    )
    assert short_delta > 0.0  # 原曲线短端更高 → 冲击后更低
    assert long_delta > 0.0  # 长端更高
    b = nss_curve.shock("steepen", 25.0)
    assert b.rate(30.0) > nss_curve.rate(30.0)
    assert b.rate(0.25) < nss_curve.rate(0.25)


def test_shock_flatten_is_opposite(linear_curve: Curve) -> None:
    steep = linear_curve.shock("steepen", 25.0)
    flat = linear_curve.shock("flatten", 25.0)
    np.testing.assert_allclose(
        steep.rate(TENORS) - linear_curve.rate(TENORS),
        -(flat.rate(TENORS) - linear_curve.rate(TENORS)),
        atol=1e-12,
    )


def test_shock_butterfly_belly_vs_wings(linear_curve: Curve, nss_curve: Curve) -> None:
    """蝶式：腹部向上、两端向下。"""
    for curve, belly_tenor in ((linear_curve, 2.0), (nss_curve, 2.0)):
        shocked = curve.shock("butterfly", 25.0)
        belly = shocked.rate(belly_tenor) - curve.rate(belly_tenor)
        short_end = shocked.rate(0.25) - curve.rate(0.25)
        long_end = shocked.rate(30.0) - curve.rate(30.0)
        assert belly > 0.0
        assert short_end < 0.0
        assert long_end < 0.0


def test_shock_parametric_matches_interpolation(nss_curve: Curve) -> None:
    """NSS 曲线上的冲击落在 {1, L1, L2} 张成的空间内，节点变化应可精确复现。"""
    steep = nss_curve.shock("steepen", 25.0)
    l1, _ = ns_loadings(TENORS, NSS_PARAMS["tau1"])
    expected = 0.25 * (0.5 - l1)
    np.testing.assert_allclose(steep.rate(TENORS) - nss_curve.rate(TENORS), expected, atol=1e-10)


def test_shock_invalid_input(linear_curve: Curve) -> None:
    with pytest.raises(ValueError, match="kind"):
        linear_curve.shock("twist", 25.0)
    with pytest.raises(ValueError, match="bp"):
        linear_curve.shock("parallel", np.nan)
    # R10：bp 是"幅度"，必须非负；方向由 kind 表达，下游不要再取负号
    with pytest.raises(ValueError, match="幅度"):
        linear_curve.shock("flatten", -25.0)


def test_shock_negative_bp_raises_all_kinds(linear_curve: Curve) -> None:
    for kind in ("parallel", "steepen", "flatten", "butterfly"):
        with pytest.raises(ValueError, match="幅度"):
            linear_curve.shock(kind, -1.0)


# ---------------------------------------------------------------------------
# 输出与便捷构造
# ---------------------------------------------------------------------------
def test_to_frame(linear_curve: Curve) -> None:
    frame = linear_curve.to_frame()
    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == ["tenor", "ytm_pct"]
    np.testing.assert_allclose(frame["tenor"].to_numpy(), TENORS)
    np.testing.assert_allclose(frame["ytm_pct"].to_numpy(), RATES)


def test_ns_curve_analytic_evaluation() -> None:
    """method="ns" 时 rate() 用参数解析求值。"""
    from ficc.curve._common import ns_rate

    params = {"b0": 3.0, "b1": -1.5, "b2": 1.2, "tau": 2.0}
    true = np.array([3.0, -1.5, 1.2, 2.0])
    curve = Curve(
        as_of=AS_OF,
        tenors=TENORS,
        rates=ns_rate(true, TENORS),
        method="ns",
        params=params,
    )
    np.testing.assert_allclose(curve.rate(TENORS), ns_rate(true, TENORS), atol=1e-12)
    # R09：参数化曲线缺参数必须抛异常，禁止静默退化成线性插值
    with pytest.raises(ValueError, match="参数不完整"):
        Curve(as_of=AS_OF, tenors=TENORS, rates=RATES, method="ns")
    with pytest.raises(ValueError, match="参数不完整"):
        Curve(
            as_of=AS_OF,
            tenors=TENORS,
            rates=RATES,
            method="nss",
            params={"b0": 3.0, "b1": -1.0},
        )


def test_from_params_accepts_ns_tau1_alias() -> None:
    """R09：NS 面板导出 tau1，from_params(method="ns") 必须接受该别名并规范化。"""
    params = {"b0": 3.0, "b1": -1.5, "b2": 1.2, "tau1": 2.0}
    curve = Curve.from_params(AS_OF, params, tenors=TENORS, method="ns")
    assert curve.params["tau"] == pytest.approx(2.0)
    expected = ns_rate(np.array([3.0, -1.5, 1.2, 2.0]), TENORS)
    np.testing.assert_allclose(curve.rate(TENORS), expected, atol=1e-10)
    # shock 也必须可用（参数完整）
    shocked = curve.shock("parallel", 100.0)
    np.testing.assert_allclose(shocked.rate(TENORS) - curve.rate(TENORS), 1.0, atol=1e-10)


def test_from_params_rejects_incomplete_params() -> None:
    with pytest.raises(ValueError, match="参数不完整"):
        Curve.from_params(AS_OF, {"b0": 3.0, "b1": -1.0}, tenors=TENORS, method="nss")
    with pytest.raises(ValueError, match=r"未知参数化方法|仅支持"):
        Curve.from_params(AS_OF, {"b0": 3.0}, tenors=TENORS, method="linear")


def test_from_params_builds_curve() -> None:
    curve = Curve.from_params(AS_OF, NSS_PARAMS, tenors=TENORS, method="nss")
    np.testing.assert_allclose(curve.rate(TENORS), nss_rate(NSS_TRUE, TENORS), atol=1e-12)
    assert curve.method == "nss"
    assert curve.params["tau2"] == pytest.approx(7.0)


def test_curve_is_immutable(linear_curve: Curve) -> None:
    with pytest.raises(FrozenInstanceError):
        linear_curve.method = "nss"  # type: ignore[misc]


# ===========================================================================
# R12/R13/R19/R20：拟合产品指纹、报告/偏差入口的 cfg+边界、重复校验、描述性判词
# ===========================================================================
_CANON = [float(t) for t in CANONICAL_TENORS]


def _custom_cfg(tmp_path):
    """自定义 cfg：reports 路径指向 tmp，用于断言不写默认目录。"""
    cfg = get_config()
    paths = cfg.paths.model_copy(update={"reports": str(tmp_path / "cfg_reports_default")})
    return cfg.model_copy(update={"paths": paths})


def _real_long_frame(path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    frame = raw[raw["term_years"].isin(_CANON)][["date", "term_years", "yield_pct"]].copy()
    frame.columns = ["date", "tenor", "ytm_pct"]
    frame["date"] = pd.to_datetime(frame["date"])
    frame["tenor"] = frame["tenor"].astype(float)
    return frame


def _synthetic_panel(n_days: int = 120) -> pd.DataFrame:
    """合成 NSS 曲线面板（含小噪声），用于快速产品/入口测试。"""
    rng = np.random.default_rng(2024)
    params = np.array([3.0, -1.5, 1.0, -0.5, 1.6, 6.0])
    rows = []
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    for _ in range(n_days):
        shifted = params.copy()
        shifted[0] += rng.normal(0.0, 0.002)
        shifted[1] += rng.normal(0.0, 0.002)
        rows.append(nss_rate(shifted, np.asarray(_CANON)) + rng.normal(0.0, 0.002, len(_CANON)))
    return pd.DataFrame(np.vstack(rows), index=dates, columns=pd.Index(_CANON, name="tenor"))


# ---------------------------------------------------------------------------
# R20：重复记录先校验一致性（冲突报错、行序无关）
# ---------------------------------------------------------------------------
def test_long_frame_to_panel_dedupes_consistent_duplicates() -> None:
    long = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-28"] * 11),
            "tenor": _CANON,
            "ytm_pct": [1.5 + 0.03 * t for t in _CANON],
        }
    )
    duplicated = pd.concat([long, long], ignore_index=True)
    panel = long_frame_to_panel(duplicated)
    assert panel.shape == (1, 11)
    assert panel.attrs["rows_removed_duplicates"] == 11
    # 行序反转结果一致
    reversed_panel = long_frame_to_panel(pd.concat([long.iloc[::-1], long], ignore_index=True))
    pd.testing.assert_frame_equal(panel, reversed_panel)


def test_long_frame_to_panel_rejects_conflicting_duplicates() -> None:
    long = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-28"] * 11),
            "tenor": _CANON,
            "ytm_pct": [1.5 + 0.03 * t for t in _CANON],
        }
    )
    conflict = pd.DataFrame(
        {"date": [pd.Timestamp("2024-06-28")], "tenor": [10.0], "ytm_pct": [99.0]}
    )
    with pytest.raises(DataValidationError, match=r"冲突|不一致|拒绝静默去重"):
        long_frame_to_panel(pd.concat([long, conflict], ignore_index=True))
    # 行序反转也必须同样报错（不能依赖顺序）
    with pytest.raises(DataValidationError):
        long_frame_to_panel(pd.concat([conflict, long], ignore_index=True))


# ---------------------------------------------------------------------------
# R13：reporting.build_reports 接受 cfg / panel / 边界 / out_dir
# ---------------------------------------------------------------------------
def test_build_reports_respects_cfg_and_sample_boundary(tmp_path, real_spike_csv) -> None:
    cfg = _custom_cfg(tmp_path)
    long = _real_long_frame(real_spike_csv)
    panel = long_frame_to_panel(long[long["date"] <= pd.Timestamp("2021-06-30")])
    out_dir = tmp_path / "reports_out"
    artifacts = build_reports(cfg=cfg, panel=panel, end="2020-12-31", out_dir=out_dir)
    # 所有产物都在 out_dir 下；自定义 cfg 的默认 reports 目录不得被创建
    for key, path in artifacts.items():
        assert Path(path).is_relative_to(out_dir), f"{key} 写到了 {path}"
    assert not (tmp_path / "cfg_reports_default").exists()
    # 即期代表日与 PCA 全部受截止日约束
    spot = pd.read_csv(artifacts["spot_curve_compare"], parse_dates=["date"])
    assert spot["date"].max() <= pd.Timestamp("2020-12-31")
    explained = pd.read_csv(artifacts["pca_explained"])
    assert (pd.to_datetime(explained["panel_end"]) <= pd.Timestamp("2020-12-31")).all()
    crosscheck = pd.read_csv(artifacts["factor_crosscheck"])
    assert (pd.to_datetime(crosscheck["panel_end"]) <= pd.Timestamp("2020-12-31")).all()
    # 验收映射列存在，未知因子不得冒充达标
    assert "meets_target" in crosscheck.columns and "target_pc" in crosscheck.columns
    diagnostics = pd.read_csv(artifacts["orthogonalization_diagnostics"])
    assert {"levels_max_abs_corr", "changes_max_abs_corr"}.issubset(diagnostics.columns)
    manifest = pd.read_csv(artifacts["manifest"])
    assert {"artifact", "path", "panel_start", "panel_end", "config_hash"}.issubset(
        manifest.columns
    )
    assert (pd.to_datetime(manifest["panel_end"]) <= pd.Timestamp("2020-12-31")).all()


# ---------------------------------------------------------------------------
# R12：拟合产品 + meta 指纹（build_fit_products / validate_fit_meta）
# ---------------------------------------------------------------------------
def test_build_fit_products_meta_contract(tmp_path) -> None:
    cfg = _custom_cfg(tmp_path)
    panel = _synthetic_panel(n_days=120)
    out_dir = tmp_path / "fit"
    artifacts = build_fit_products(cfg, panel, method="nss", out_dir=out_dir, include_summary=True)
    for mode in ("free", "anchored"):
        for kind in ("params", "residuals", "fitted"):
            key = f"{kind}_nss_{mode}"
            assert key in artifacts and artifacts[key].exists()
    meta = read_fit_meta(artifacts["meta"])
    for field in (
        "config_hash",
        "method",
        "weights",
        "start",
        "end",
        "n_days",
        "generated_at",
        "files",
    ):
        assert field in meta
    assert meta["config_hash"] == cfg.hash()
    assert meta["method"] == "nss"
    assert meta["weights"] == str(cfg.curve.weights)
    assert meta["n_days"] == len(panel)
    assert "free" in meta["files"] and "anchored" in meta["files"]
    assert "summary" in meta["files"] and "bad_dates" in meta["files"]
    # validate_fit_meta 正向通过
    validated = validate_fit_meta(cfg, artifacts["residuals_nss_free"])
    assert validated["config_hash"] == meta["config_hash"]
    # 当前默认配置 hash 不同 → 必须报错（不静默消费他人生成的残差）
    with pytest.raises(ValueError, match="config_hash"):
        validate_fit_meta(get_config(), artifacts["residuals_nss_free"])
    # summary 覆盖本 method
    summary = pd.read_csv(artifacts["summary"])
    assert len(summary) > 0 and "fit_mode" in summary.columns


def test_bias_run_requires_and_validates_meta(tmp_path) -> None:
    cfg = _custom_cfg(tmp_path)
    residuals_dir = tmp_path / "resid"
    out_dir = tmp_path / "bias_out"
    residuals_dir.mkdir()
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2015-01-05", periods=600)
    # 构造 AR(1) 正均值残差（10Y 均值 +4bp，其余接近 0）
    values = np.zeros((len(dates), len(_CANON)))
    state = 4.0
    for i in range(len(dates)):
        state = 0.9 * state + 0.1 * 4.0 + rng.normal(0.0, 1.0)
        values[i, :] = rng.normal(0.0, 2.0, len(_CANON))
        values[i, _CANON.index(10.0)] = state
    residuals = pd.DataFrame(values, index=dates, columns=pd.Index(_CANON, name="tenor"))
    residuals_path = residuals_dir / "curve_fit_residuals_nss_free.csv"
    residuals.to_csv(residuals_path, encoding="utf-8-sig")
    meta = {
        "config_hash": cfg.hash(),
        "method": "nss",
        "weights": str(cfg.curve.weights),
        "start": dates.min().date().isoformat(),
        "end": dates.max().date().isoformat(),
        "n_days": len(dates),
        "generated_at": _datetime.now(UTC).isoformat(),
        "files": {"free": {"residuals": str(residuals_path), "params": "", "fitted": ""}},
    }
    meta_path = residuals_dir / "curve_fit_nss_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    artifacts = run_bias(cfg=cfg, residuals_csv=residuals_path, out_dir=out_dir)
    for key in ("persistence", "segments", "conclusion", "figure"):
        assert artifacts[key].exists()
    persistence = pd.read_csv(artifacts["persistence"])
    assert {"hac_t_lag20", "hac_t_lag60", "hac_t_lag250"}.issubset(persistence.columns)
    row = persistence.set_index("tenor").loc[10.0]
    assert abs(row["hac_t_lag250"]) < abs(row["t_stat"])
    # 描述性判词：不得出现经济/交易结论词
    md = artifacts["conclusion"].read_text(encoding="utf-8")
    for section in (
        "## 证据",
        "## 限制",
        "## 未检验项",
        "economic_interpretation = unidentified",
        "tradability = untested",
    ):
        assert section in md
    for banned in ("可交易", "负 carry"):
        assert banned not in md

    # meta 与当前配置不一致 → 报错
    tampered = dict(meta)
    tampered["config_hash"] = "deadbeef"
    meta_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="config_hash"):
        run_bias(cfg=cfg, residuals_csv=residuals_path, out_dir=out_dir)
    # 日期范围与 meta 不符 → 报错
    tampered = dict(meta)
    tampered["start"] = "2000-01-01"
    meta_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="日期范围"):
        run_bias(cfg=cfg, residuals_csv=residuals_path, out_dir=out_dir)
    # 缺 meta → 报错
    meta_path.unlink()
    with pytest.raises(ValueError, match="meta"):
        run_bias(cfg=cfg, residuals_csv=residuals_path, out_dir=out_dir)


def test_bias_verdict_fields_are_descriptive() -> None:
    """R19：bias_verdict 输出固定描述性枚举，不生成交易判词。"""
    dates = pd.bdate_range("2015-01-05", periods=400)
    rng = np.random.default_rng(3)
    residuals = pd.DataFrame(
        rng.normal(0.0, 2.0, (len(dates), len(_CANON))),
        index=dates,
        columns=pd.Index(_CANON, name="tenor"),
    )
    residuals[10.0] = 4.0 + rng.normal(0.0, 1.0, len(dates))
    from ficc.curve.bias import rolling_regime, segment_table, tenor_persistence_table

    persistence = tenor_persistence_table(residuals)
    segments = segment_table(residuals[10.0])
    regime = rolling_regime(residuals[10.0], window=60)
    verdict = bias_verdict(persistence, segments, regime)
    assert verdict["economic_interpretation"] == "unidentified"
    assert verdict["tradability"] == "untested"
    assert isinstance(verdict["persistent_model_relative_residual"], bool)
    assert verdict["mean_reverting_or_drifting"] in {
        "mean_reverting_around_constant_mean",
        "mean_reverting_around_time_varying_mean",
        "cannot_reject_unit_root",
    }
    for section in ("evidence", "limitations", "untested"):
        assert isinstance(verdict[section], list) and verdict[section]
    joined = " ".join(verdict["evidence"] + verdict["limitations"] + verdict["untested"])
    assert "可交易" not in joined and "负 carry" not in joined
    # HAC t 与普通 t 并列且不同
    stats_ = hac_t_statistics(residuals[10.0])
    assert stats_["hac_t_lag20"] != stats_["hac_t_lag250"]
