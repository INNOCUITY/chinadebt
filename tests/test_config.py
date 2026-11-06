"""配置系统测试。

覆盖点：
1. 真实 `config/config.yaml` 能被完整校验（这是每次改配置的守门测试）。
2. 配置不可变、指纹确定。
3. 错误配置必须**报错**而不是静默通过（严格模式的意义）。
4. 路径解析与目录创建。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ficc.config import (
    PROJECT_ROOT,
    Config,
    clear_config_cache,
    get_config,
    load_config,
)


# ---------------------------------------------------------------------------
# 1. 真实配置
# ---------------------------------------------------------------------------
def test_real_config_loads(cfg: Config) -> None:
    """仓库里的 config.yaml 必须永远可加载。"""
    assert cfg.data.tenors == [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0]
    assert cfg.data.tenors == sorted(cfg.data.tenors)
    assert cfg.backtest.start_date == date(2019, 1, 28)
    assert cfg.backtest.annual_days == 250
    assert cfg.pricing.frequency_by_tenor["10Y-30Y"] == 2


def test_config_hash_is_deterministic(cfg: Config) -> None:
    """同一份配置反复计算指纹必须一致，且是 16 位十六进制。"""
    h1 = cfg.hash()
    clear_config_cache()
    h2 = get_config().hash()
    assert h1 == h2
    assert len(h1) == 16
    int(h1, 16)  # 必须能按十六进制解析


def test_config_hash_changes_with_content(cfg: Config) -> None:
    """参数变了指纹必须变——否则复现性无从谈起。"""
    raw = cfg.model_dump(mode="json")
    raw["costs"]["fee_bp"] = raw["costs"]["fee_bp"] + 0.5
    changed = Config.model_validate(raw)
    assert changed.hash() != cfg.hash()


def test_config_is_frozen(cfg: Config) -> None:
    with pytest.raises(ValidationError):
        cfg.backtest.annual_days = 252  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. 严格校验：错误配置必须报错
# ---------------------------------------------------------------------------
def _write_cfg(tmp_path: Path, mutate) -> Path:
    src = PROJECT_ROOT / "config" / "config.yaml"
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    mutate(raw)
    out = tmp_path / "config.yaml"
    out.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def test_unknown_key_rejected(tmp_path: Path) -> None:
    """拼写错误的键必须报错，而不是被静默忽略。"""

    def mutate(raw: dict) -> None:
        raw["backtest"]["annual_dayz"] = 250  # 拼错

    with pytest.raises(ValueError, match="annual_dayz"):
        load_config(_write_cfg(tmp_path, mutate))


def test_tenors_must_be_increasing(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["data"]["tenors"] = [1.0, 0.5, 2.0, 3.0]

    with pytest.raises(ValueError, match="递增"):
        load_config(_write_cfg(tmp_path, mutate))


def test_tenors_need_enough_points(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["data"]["tenors"] = [1.0, 2.0, 3.0]

    with pytest.raises(ValueError, match="至少需要 4 个期限点"):
        load_config(_write_cfg(tmp_path, mutate))


def test_backtest_date_range_validated(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        raw["backtest"]["end_date"] = "2018-01-01"  # 早于 start_date

    with pytest.raises(ValueError, match="必须晚于"):
        load_config(_write_cfg(tmp_path, mutate))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "not_exists.yaml")


# ---------------------------------------------------------------------------
# 3. 路径解析
# ---------------------------------------------------------------------------
def test_paths_resolve_to_project_root(cfg: Config) -> None:
    p = cfg.paths.abs("processed")
    assert p.is_absolute()
    assert p == PROJECT_ROOT / "data" / "processed"


def test_paths_unknown_key_raises(cfg: Config) -> None:
    with pytest.raises(KeyError, match="未知路径键"):
        cfg.paths.abs("nope")


def test_paths_ensure_creates_dirs(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure() 必须幂等地建出所有目录（这里重定向到 tmp，避免污染仓库）。"""
    from ficc.config import PathsConfig

    paths = PathsConfig(
        raw=str(tmp_path / "raw"),
        interim=str(tmp_path / "interim"),
        processed=str(tmp_path / "processed"),
        metadata=str(tmp_path / "metadata"),
        artifacts=str(tmp_path / "artifacts"),
        reports=str(tmp_path / "reports"),
    )
    paths.ensure()
    paths.ensure()  # 幂等
    for key in ("raw", "interim", "processed", "metadata", "artifacts", "reports"):
        assert Path(getattr(paths, key)).is_dir()


# ---------------------------------------------------------------------------
# 4. 报告语言（需求方要求中英双语）
# ---------------------------------------------------------------------------
def test_report_language_supports_bilingual(cfg: Config) -> None:
    assert cfg.report.language in {"zh", "en", "both"}
