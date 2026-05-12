from .planning_boi import AGENT_SPEC as PLANNING_BOI
from .signal_boi import AGENT_SPEC as SIGNAL_BOI
from .risk_boi import AGENT_SPEC as RISK_BOI
from .execution_boi import AGENT_SPEC as EXECUTION_BOI
from .vibe_code_boi import AGENT_SPEC as VIBE_CODE_BOI
from .gen_research_boi import AGENT_SPEC as GEN_RESEARCH_BOI

SQUAD = {
    'Planning Boi': PLANNING_BOI,
    'Signal Boi': SIGNAL_BOI,
    'Risk Boi': RISK_BOI,
    'Execution Boi': EXECUTION_BOI,
    'Vibe Code Boi': VIBE_CODE_BOI,
    'Gen Research Boi': GEN_RESEARCH_BOI,
}
