"""曲线层：``Curve`` 对象 + Nelson-Siegel / Svensson 拟合 + 残差诊断。

对外只暴露下面这些名字（pipeline 按此调用）::

    from ficc.curve import Curve, fit_curve, fit_ns, fit_nss, fit_panel, residual_summary

设计要点见各模块 docstring：

- :mod:`ficc.curve.curve`：不可变 ``Curve``，支持 linear / hermite / ns / nss 求值、
  远期、平行/陡平/蝶式冲击；
- :mod:`ficc.curve.ns` / :mod:`ficc.curve.nss`：参数化拟合，τ 网格粗搜 + 有界精修；
- :mod:`ficc.curve.fitting`：单日 ``fit_curve``、面板 ``fit_panel``、残差 ``residual_summary``；
- :mod:`ficc.curve.factors`：**推荐因子口径** ``observable_factors``（level/slope/curvature
  直接由观测收益率构造，无参数识别性问题）与 ``factor_jump_sensitivity``。

**残差与 RMSE 的单位统一为 bp**（``1bp = 0.01%``）；曲线与参数的收益率单位是百分数。
"""

from __future__ import annotations

from ficc.curve.curve import Curve
from ficc.curve.factors import factor_jump_sensitivity, observable_factors
from ficc.curve.fitting import fit_curve, fit_panel, residual_summary
from ficc.curve.ns import NSResult, fit_ns, ns_rate
from ficc.curve.nss import NSSResult, fit_nss, nss_rate

__all__ = [
    "Curve",
    "NSResult",
    "NSSResult",
    "factor_jump_sensitivity",
    "fit_curve",
    "fit_ns",
    "fit_nss",
    "fit_panel",
    "ns_rate",
    "nss_rate",
    "observable_factors",
    "residual_summary",
]
