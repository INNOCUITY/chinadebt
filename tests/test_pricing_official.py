"""财政部续发行公告外部配对案例（E/F）：memo §2.4。

证据边界（评审“定价与后续研究的证据边界”要求）
-----------------------------------------------
- A–D 是 **memo 自算算例**（黄金数来自 ``docs/00_market_conventions.md`` 的自算，
  不是独立外部对账）；D 的起息/结算日期还是测试自行假设。
- **本文件才是独立外部报价配对**：财政部续发行公告同时公布「续发行价格（元）」与
  「折合年收益率（%）」，是唯一公开的「官方全价—收益率」对照样本（memo §2.4）。
  30Y 续发行通知明确投标标位按**全价**计算，因此公告价格按全价口径读入。
- 公告数字的取舍：价格四舍五入到 **0.01 元**、折合年收益率四舍五入到
  **0.01 个百分点（= 1bp）**；因此反解收益率与公告值之间允许 ≤0.5bp 的偏差
  （memo 给出 0.2–0.5bp），测试容差按此设置。
- 公告未披露全部条款，凡无法确证的起息日/交易日一律在用例 docstring 里
  标「**假设**」，并锁定估值日/结算日。

数据来源
--------
- 案例 E：财政部国债业务公告 2026 年第 86 号（2026-05-25），26 附息国债 10 续发行，
  续发行价格 99.98 元、折合年收益率 1.73%：
  https://www.chinabond.com.cn/xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl/zqzl_gz/gz_jzsfx/jzsfx_fxjg_ath/202605/t20260525_855110860.html
- 案例 F：财政部国债业务公告 2026 年第 77 号（2026-05-14），26 附息国债 7 续发行，
  续发行价格 100.71 元、折合年收益率 1.59%：
  https://www.chinabond.com.cn/xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl/zqzl_gz/gz_jzsfx/jzsfx_fxjg_ath/202605/t20260514_855091720.html
- 全价口径旁证（30Y 续发行规定投标标位按中债 30Y 收益率均值 ±10% 对应的**全价**）：
  http://app.mof.gov.cn/mofmobile2026/czxw/202604/t20260401_4001890.htm
"""

from __future__ import annotations

from datetime import date

import pytest

from ficc.data.schema import BondStatic
from ficc.pricing.analytics import dv01
from ficc.pricing.bond import dirty_price_from_ytm, ytm_from_clean_price, ytm_from_dirty_price


# ---------------------------------------------------------------------------
# 案例 E：26 附息国债 10（10Y、1.72%、半年付）
# ---------------------------------------------------------------------------
def test_case_E_260010_announcement_is_full_price(near_par_bond: BondStatic) -> None:
    """案例 E：26 附息国债 10 续发行（公告 2026-05-25，99.98 元 / 1.73%）。

    数据来源：财政部国债业务公告 2026 年第 86 号（memo §2.4，链接见模块 docstring）。
    锁定日期：估值日/结算日 = 2026-05-25（公告日，memo §2.4 同款假设）；
              另测次一交易日 2026-05-26 以验证结论不依赖单日。
    起息日：2026-05-15、到期 2036-05-15（财政部发行通知，memo §2.3 已核）。
    舍入规则：公告价格到 0.01 元、收益率到 1bp → 反解与 1.73% 允许 ≤0.5bp 偏差。
    结论：按**全价** 99.98 反解得 1.7273%（5/25）/ 1.7278%（5/26），与公告
    1.73% 差 0.2–0.3bp；若误按**净价**代入得 1.7222%，差 0.8bp 且取整为 1.72%，
    与公告不符 → 公告价格是全价（memo §2.4 的结论）。
    """
    # 全价口径：反解收益率 ≈ 公告折合年收益率
    y_0525 = ytm_from_dirty_price(near_par_bond, date(2026, 5, 25), 99.98)
    y_0526 = ytm_from_dirty_price(near_par_bond, date(2026, 5, 26), 99.98)
    assert y_0525 == pytest.approx(1.7273, abs=5e-4)
    assert y_0526 == pytest.approx(1.7278, abs=5e-4)
    # 与公告 1.73% 的偏差在 0.2–0.3bp（memo）之内
    assert y_0525 - 1.73 == pytest.approx(-0.0027, abs=1e-3)
    assert abs(y_0525 - 1.73) < 0.005  # 0.5bp
    assert round(y_0525, 2) == 1.73  # 取整到 1bp 后与公告一致

    # 正算核对：1.73% 的全价与公告价 99.98 差约 0.27bp（价格差 2.5 分）
    dirty_at_announcement_yield = dirty_price_from_ytm(near_par_bond, date(2026, 5, 25), 1.73)
    price_gap = 99.98 - dirty_at_announcement_yield
    price_gap_bp = price_gap / dv01(near_par_bond, date(2026, 5, 25), 1.73)
    assert price_gap_bp == pytest.approx(0.27, abs=0.05)
    assert 0.2 < price_gap_bp < 0.3

    # 错误口径对照：把 99.98 当净价会得到 1.7222%，与公告差 0.8bp，且取整为 1.72%
    y_clean = ytm_from_clean_price(near_par_bond, date(2026, 5, 25), 99.98)
    assert y_clean == pytest.approx(1.7222, abs=5e-4)
    assert abs(y_clean - 1.73) > 0.005  # 0.5bp，超出公告舍入误差
    assert round(y_clean, 2) == 1.72  # 与公告 1.73% 不符 → 净价口径错误


# ---------------------------------------------------------------------------
# 案例 F：26 附息国债 7（7Y、1.66%、年付）
# ---------------------------------------------------------------------------
def test_case_F_260007_announcement_pairing() -> None:
    """案例 F：26 附息国债 7 续发行（公告 2026-05-14，100.71 元 / 1.59%）。

    数据来源：财政部国债业务公告 2026 年第 77 号（memo §2.4，链接见模块 docstring）。
    锁定日期：估值日/结算日 = 2026-05-14（公告日，memo §2.4 同款假设）。
    条款：7Y、票面 1.66%、年付（memo 给定）。
    起息日**假设**：2026-03-25、到期 2033-03-25。公告未披露该券起息日；
        该假设由 memo 的两个数字反推而来——在此假设下
        y(全价 100.71) = 1.584993%、P(1.59%) = 100.677590，分别与 memo 的
        1.5850% / 100.6776 一致到 4 位小数。**起息日未经公告独立核实**，
        换别的起息日会让绝对数值小幅漂移（但结论不变）。
    舍入规则：公告价格到 0.01 元、收益率到 1bp → 反解与 1.59% 允许 ≤0.5bp 偏差；
        按 1.59% 正算 100.6776 与公告 100.71 差 3.2 分 ≈ 0.48bp × DV01，
        可由收益率只给到 1bp 精度解释（memo：“公告价格四舍五入到分所致”）。
    """
    bond = BondStatic(
        code="260007.IB",
        issue_date=date(2026, 3, 25),  # 假设值（见 docstring）
        maturity_date=date(2033, 3, 25),  # 假设值
        coupon_pct=1.66,
        freq=1,
    )
    settle = date(2026, 5, 14)

    # 按全价 100.71 反解 ≈ memo 的 1.5850%，与公告 1.59% 差 0.5bp
    ytm = ytm_from_dirty_price(bond, settle, 100.71)
    assert ytm == pytest.approx(1.5850, abs=5e-4)
    assert 1.59 - ytm == pytest.approx(0.005, abs=1e-3)
    assert abs(ytm - 1.59) <= 0.006  # ≈0.5bp（实际 0.501bp，公告按 1bp 取整）

    # 按公告收益率 1.59% 正算全价 = 100.6776（memo 给出的对照值）
    dirty_159 = dirty_price_from_ytm(bond, settle, 1.59)
    assert dirty_159 == pytest.approx(100.6776, abs=1e-3)

    # 公告价差 3.2 分 ≈ 0.5bp × DV01（收益率按 1bp 取整带来的价格误差）
    price_gap = 100.71 - dirty_159
    assert price_gap == pytest.approx(0.0324, abs=1e-3)
    assert price_gap == pytest.approx(0.5 * dv01(bond, settle, 1.59), abs=5e-3)
