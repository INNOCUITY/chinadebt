"""ML 接口层：as-of 数据视图、特征、远期标签、walk-forward 切分。

本包**只提供接口与校验**，不做训练、不做绩效比较、不做参数搜索。

导出
----
- :class:`AsOfView` / :func:`build_asof_view`：只暴露 ``≤ as_of - lag`` 的观测；
- :class:`FeatureSpec` / :func:`build_current_features`：**当前决策时点**的单行特征
  （旧名 :func:`build_features` 兼容保留，但只返回当前行并告警）；
- :func:`build_training_matrix`：**逐决策时点**构造训练矩阵（每个 t 单独 as-of 视图，
  禁止整段历史算完再截断）；
- :func:`assert_per_row_causality`：逐样本因果性校验（修改未来数据后 t 行必须不变）；
- :class:`ForwardLabelSpec` / :class:`LabelRecord` / :func:`forward_returns`：
  带 ``available_after`` 的离线标签生成器；
- :class:`WalkForwardSpec` / :class:`Split` / :func:`generate_splits` /
  :func:`validate_no_leakage`：purge/embargo 由标签持有期约束的切分与泄漏检测。

研究边界：2019–2025 已被研究，不能命名为 untouched holdout；测试集只用于最后评估。
"""

from __future__ import annotations

from ficc.ml.asof import (
    AsOfView,
    FeatureSpec,
    assert_per_row_causality,
    build_asof_view,
    build_current_features,
    build_features,
    build_training_matrix,
)
from ficc.ml.labels import (
    EntryConvention,
    ForwardLabelSpec,
    LabelRecord,
    ReturnType,
    forward_returns,
)
from ficc.ml.walkforward import (
    LeakageError,
    Split,
    WalkForwardSpec,
    generate_splits,
    validate_no_leakage,
)

__all__ = [
    "AsOfView",
    "EntryConvention",
    "FeatureSpec",
    "ForwardLabelSpec",
    "LabelRecord",
    "LeakageError",
    "ReturnType",
    "Split",
    "WalkForwardSpec",
    "assert_per_row_causality",
    "build_asof_view",
    "build_current_features",
    "build_features",
    "build_training_matrix",
    "forward_returns",
    "generate_splits",
    "validate_no_leakage",
]
