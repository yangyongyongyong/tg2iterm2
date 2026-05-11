"""模块配置加载器，支持为每个模块单独配置 Cursor CLI 模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModuleConfig:
    """单个模块的 CLI 配置。"""

    model: str = "auto"
    timeout: int = 180


@dataclass
class ModulesConfig:
    """所有模块的配置集合。"""

    modules: dict[str, ModuleConfig] = field(default_factory=dict)
    default: ModuleConfig = field(default_factory=ModuleConfig)

    def get(self, module_name: str) -> ModuleConfig:
        """获取指定模块的配置，不存在则返回默认配置。"""
        return self.modules.get(module_name, self.default)


def load_modules_config(config_path: Path | str | None = None) -> ModulesConfig:
    """从 YAML 文件加载模块配置。

    Args:
        config_path: 配置文件路径，默认为项目目录下的 modules.yaml

    Returns:
        ModulesConfig 实例
    """
    if config_path is None:
        config_path = Path(__file__).parent / "modules.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        return ModulesConfig()

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return ModulesConfig()

    # 解析默认配置
    default_data = data.pop("default", {}) or {}
    default_config = ModuleConfig(
        model=str(default_data.get("model", "auto")),
        timeout=int(default_data.get("timeout", 180)),
    )

    # 解析各模块配置
    modules = {}
    for name, cfg in data.items():
        if isinstance(cfg, dict):
            modules[name] = ModuleConfig(
                model=str(cfg.get("model", default_config.model)),
                timeout=int(cfg.get("timeout", default_config.timeout)),
            )

    return ModulesConfig(modules=modules, default=default_config)
