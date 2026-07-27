"""AI architecture-critic config block (#1036 Part 2 / #963).

The state-based whole-system critic gets its OWN AI config — a third
independent workload alongside ai_summary (reviews a change) and
ai_onboarding (generates HCL). These assert the defaults (off, opt-in) and
that the block is genuinely separate so flipping one never touches another.
"""

from terrapod.config import (
    AIArchitectureAuthConfig,
    AIArchitectureConfig,
    settings,
)


class TestAIArchitectureConfigDefaults:
    def test_disabled_by_default(self):
        # Master switch off — the critic surface must self-gate until enabled.
        assert AIArchitectureConfig().enabled is False

    def test_defaults(self):
        cfg = AIArchitectureConfig()
        # The code default is empty — enabling requires explicit config (the
        # recommended model string lives in values.yaml, not the Pydantic
        # default), same as ai_onboarding.
        assert cfg.model == ""
        assert cfg.api_base == ""
        assert cfg.max_output_tokens == 16384
        # Higher than the summariser's timeout — a bigger completion.
        assert cfg.request_timeout_seconds == 180
        assert cfg.daily_token_budget == 0  # unlimited unless set
        assert cfg.context == ""
        assert isinstance(cfg.auth, AIArchitectureAuthConfig)
        assert cfg.auth.aws_session_name == "terrapod-ai-architecture"
        assert cfg.auth.aws_region == "us-east-1"
        assert cfg.auth.api_key == ""

    def test_mounted_on_settings_separately_from_other_ai_blocks(self):
        # Three independent AI workloads — flipping one must not touch another.
        assert hasattr(settings, "ai_architecture")
        assert settings.ai_architecture is not settings.ai_summary
        assert settings.ai_architecture is not settings.ai_onboarding
        assert settings.ai_architecture.auth is not settings.ai_summary.auth
