"""LLM 与应用配置的稳定异常类型。"""


class ConfigurationError(ValueError):
    """运行配置无效。"""


class ModelError(RuntimeError):
    """模型调用失败。"""
