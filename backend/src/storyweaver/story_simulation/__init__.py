"""非正史角色剧场领域包。"""

# 不在包初始化时导入 service：仓储也依赖 models，避免循环导入。
from .models import SimulationSession, SimulationState, SimulationTurn

__all__ = ["SimulationSession", "SimulationState", "SimulationTurn"]
