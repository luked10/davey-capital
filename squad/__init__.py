from .planning_agent import AGENT_SPEC as PLANNING_AGENT
from .signal_agent import AGENT_SPEC as SIGNAL_AGENT
from .risk_agent import AGENT_SPEC as RISK_AGENT
from .execution_agent import AGENT_SPEC as EXECUTION_AGENT
from .vibe_code_agent import AGENT_SPEC as VIBE_CODE_AGENT
from .gen_research_agent import AGENT_SPEC as GEN_RESEARCH_AGENT

SQUAD = {
    'Planning Agent': PLANNING_AGENT,
    'Signal Agent': SIGNAL_AGENT,
    'Risk Agent': RISK_AGENT,
    'Execution Agent': EXECUTION_AGENT,
    'Vibe Code Agent': VIBE_CODE_AGENT,
    'Gen Research Agent': GEN_RESEARCH_AGENT,
}
