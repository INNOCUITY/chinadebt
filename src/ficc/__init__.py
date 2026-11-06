"""FICC 国债收益率曲线量化研究与风险管理系统。

顶层包。仅暴露版本号与配置入口，避免导入副作用。
"""

from __future__ import annotations

__version__ = "0.1.0"

from ficc.config import Config, get_config

__all__ = ["Config", "__version__", "get_config"]
