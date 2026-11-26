"""ML 与 RL 共用的因果市场特征。"""

from ficc.ml.asof import FeatureSpec

FUNDING_COLUMN = "FDR007"


def feature_specs() -> list[FeatureSpec]:
    """监督学习特征：水平/斜率/曲率、2s10s、carry、动量、EWMA 波动、资金面。"""
    return [
        FeatureSpec(name="level_5y", transform=lambda f: f["5.0"], inputs=("5.0",)),
        FeatureSpec(
            name="slope_2s10s",
            transform=lambda f: f["10.0"] - f["2.0"],
            inputs=("2.0", "10.0"),
        ),
        FeatureSpec(
            name="curvature_2_5_10",
            transform=lambda f: 2.0 * f["5.0"] - f["2.0"] - f["10.0"],
            inputs=("2.0", "5.0", "10.0"),
        ),
        FeatureSpec(
            name="carry_proxy_5y",
            transform=lambda f: f["5.0"] - f[FUNDING_COLUMN],
            inputs=("5.0", FUNDING_COLUMN),
        ),
        FeatureSpec(
            name="mom_20_5y",
            transform=lambda f: f["5.0"] - f["5.0"].shift(20),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="mom_60_5y",
            transform=lambda f: f["5.0"] - f["5.0"].shift(60),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="ewma_vol_20_5y",
            transform=lambda f: f["5.0"].diff().ewm(span=20, adjust=False).std(),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="fdr007_level",
            transform=lambda f: f[FUNDING_COLUMN],
            inputs=(FUNDING_COLUMN,),
        ),
        FeatureSpec(
            name="fdr007_change_5",
            transform=lambda f: f[FUNDING_COLUMN] - f[FUNDING_COLUMN].shift(5),
            inputs=(FUNDING_COLUMN,),
        ),
    ]
