from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_env: str = Field(default="development", pattern="^(development|staging|production)$")
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    allow_public_bind: bool = False
    access_auth_enabled: bool = False
    access_invite_code: str = ""
    access_cookie_secret: str = ""
    access_cookie_secure: bool = False
    storage_root: Path = Path(".")
    database_url: str = "sqlite:///./data/research.db"
    artifacts_dir: Path = Path("./data/artifacts")
    logs_dir: Path = Path("./logs")
    backups_dir: Path = Path("./backups")
    max_pending_runs: int = Field(default=20, ge=1, le=1_000)
    worker_poll_interval: float = Field(default=1.0, ge=0)
    worker_step_delay: float = Field(default=1.0, ge=0)
    log_level: str = "INFO"
    checkpoint_database_path: Path = Path("./data/checkpoints.db")
    pi_runtime_mode: str = Field(default="mock", pattern="^(mock|live)$")
    pi_bridge_command: str = "node"
    pi_bridge_entry: Path = Path("./pi_bridge/dist/index.js")
    pi_bridge_start_timeout: float = Field(default=15.0, gt=0)
    pi_request_timeout: float = Field(default=120.0, gt=0)
    pi_bridge_max_restarts: int = Field(default=1, ge=0, le=1)
    pi_model_provider: str = ""
    pi_model: str = ""
    pi_api_key_env_name: str = ""
    agent_profile_dir: Path = Path("./app/profiles")
    max_agent_context_chars: int = Field(default=80_000, gt=0)
    max_agent_output_chars: int = Field(default=20_000, gt=0)
    max_tool_calls_per_node: int = Field(default=25, ge=0)
    tool_default_timeout: float = Field(default=30.0, gt=0)
    output_repair_attempts: int = Field(default=1, ge=0, le=1)
    market_data_mode: str = Field(default="mock", pattern="^(mock|live)$")
    market_data_provider: str = "akshare"
    market_data_lookback_days: int = Field(default=750, ge=120)
    market_data_min_bars: int = Field(default=120, ge=2)
    market_data_adjustment: str = "qfq"
    market_data_max_retries: int = Field(default=2, ge=0, le=5)
    technical_indicator_version: str = "tech_indicator_v1"
    kronos_mode: str = Field(default="mock", pattern="^(mock|live)$")
    kronos_model_name: str = ""
    kronos_device: str = "cpu"
    kronos_source_dir: Path = Path("./vendor/kronos")
    kronos_lookback: int = Field(default=120, ge=20, le=512)
    kronos_sample_count: int = Field(default=1, ge=1, le=5)
    kronos_prediction_length: int = Field(default=5, ge=1, le=120)
    kronos_timeout_seconds: float = Field(default=360, gt=0)
    kronos_queue_timeout_seconds: float = Field(default=420, gt=0)
    technical_research_profile: str = "technical_research"
    technical_assembly_profile: str = "technical_assembly"
    technical_workflow_version: str = "technical_v1"
    fundamental_data_mode: str = Field(default="mock", pattern="^(mock|live)$")
    fundamental_data_provider: str = "akshare"
    fundamental_data_max_retries: int = Field(default=2, ge=0, le=5)
    research_search_mode: str = Field(default="mock", pattern="^(mock|live)$")
    research_search_provider: str = ""
    research_search_providers: list[str] = Field(default_factory=list)
    research_search_api_key_env_name: str = ""
    research_search_max_results: int = Field(default=8, ge=1, le=20)
    research_source_timeout: float = Field(default=30, gt=0, le=120)
    research_max_source_chars: int = Field(default=20_000, ge=1_000, le=200_000)
    research_max_pdf_bytes: int = Field(default=20_000_000, ge=100_000, le=100_000_000)
    # Reader service: a JS-rendering extraction layer for the read path.
    # ``none`` keeps the legacy local download+extract path (offline/tests).
    # ``jina`` uses the keyless Jina Reader (rate-limited; key optional for
    # higher limits via ``research_reader_api_key_env_name``).
    # ``firecrawl`` uses the Firecrawl scrape endpoint (key required).
    research_reader: str = Field(default="none", pattern="^(none|jina|firecrawl)$")
    research_reader_api_key_env_name: str = ""
    research_reader_timeout: float = Field(default=30, gt=0, le=120)
    findkg_data_dir: Path = Path("./data/findkg/FinDKG")
    financial_metric_version: str = "financial_metric_v1"
    valuation_script_version: str = "valuation_v1"
    fundamental_lead_profile: str = "fundamental_lead"
    business_research_profile: str = "business_research"
    industry_research_profile: str = "industry_research"
    deep_research_profile: str = "deep_research"
    financial_research_profile: str = "financial_research"
    valuation_research_profile: str = "valuation_research"
    lead_synthesis_profile: str = "lead_synthesis"
    writer_planning_profile: str = "writer_planning"
    fundamental_writer_profile: str = "fundamental_writer"
    final_synthesis_profile: str = "final_synthesis"
    chart_data_extractor_profile: str = "chart_data_extractor"
    fundamental_workflow_version: str = "fundamental_v1"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        app_env = os.getenv("APP_ENV", "development")
        return cls(
            app_env=app_env,
            app_host=os.getenv("APP_HOST", "127.0.0.1"),
            app_port=int(os.getenv("PORT", os.getenv("APP_PORT", "8000"))),
            allow_public_bind=os.getenv("ALLOW_PUBLIC_BIND", "false").lower() in {"1", "true", "yes", "on"},
            access_auth_enabled=os.getenv("ACCESS_AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
            access_invite_code=os.getenv("ACCESS_INVITE_CODE", ""),
            access_cookie_secret=os.getenv("ACCESS_COOKIE_SECRET", ""),
            access_cookie_secure=os.getenv(
                "ACCESS_COOKIE_SECURE", "true" if app_env == "production" else "false"
            ).lower() in {"1", "true", "yes", "on"},
            storage_root=Path(os.getenv("STORAGE_ROOT", ".")),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/research.db"),
            artifacts_dir=Path(os.getenv("ARTIFACTS_DIR", "./data/artifacts")),
            logs_dir=Path(os.getenv("LOGS_DIR", "./logs")),
            backups_dir=Path(os.getenv("BACKUPS_DIR", "./backups")),
            max_pending_runs=int(os.getenv("MAX_PENDING_RUNS", "20")),
            worker_poll_interval=float(os.getenv("WORKER_POLL_INTERVAL", "1")),
            worker_step_delay=float(os.getenv("WORKER_STEP_DELAY", "1")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            checkpoint_database_path=Path(
                os.getenv("CHECKPOINT_DATABASE_PATH", "./data/checkpoints.db")
            ),
            pi_runtime_mode=os.getenv("PI_RUNTIME_MODE", "mock"),
            pi_bridge_command=os.getenv("PI_BRIDGE_COMMAND", "node"),
            pi_bridge_entry=Path(
                os.getenv("PI_BRIDGE_ENTRY", "./pi_bridge/dist/index.js")
            ),
            pi_bridge_start_timeout=float(
                os.getenv("PI_BRIDGE_START_TIMEOUT", "15")
            ),
            pi_request_timeout=float(os.getenv("PI_REQUEST_TIMEOUT", "120")),
            pi_bridge_max_restarts=int(os.getenv("PI_BRIDGE_MAX_RESTARTS", "1")),
            pi_model_provider=os.getenv("PI_MODEL_PROVIDER", ""),
            pi_model=os.getenv("PI_MODEL", ""),
            pi_api_key_env_name=os.getenv("PI_API_KEY_ENV_NAME", ""),
            agent_profile_dir=Path(
                os.getenv("AGENT_PROFILE_DIR", "./app/profiles")
            ),
            max_agent_context_chars=int(
                os.getenv("MAX_AGENT_CONTEXT_CHARS", "80000")
            ),
            max_agent_output_chars=int(
                os.getenv("MAX_AGENT_OUTPUT_CHARS", "20000")
            ),
            max_tool_calls_per_node=int(
                os.getenv("MAX_TOOL_CALLS_PER_NODE", "25")
            ),
            tool_default_timeout=float(os.getenv("TOOL_DEFAULT_TIMEOUT", "30")),
            output_repair_attempts=int(os.getenv("OUTPUT_REPAIR_ATTEMPTS", "1")),
            market_data_mode=os.getenv("MARKET_DATA_MODE", "mock"),
            market_data_provider=os.getenv("MARKET_DATA_PROVIDER", "akshare"),
            market_data_lookback_days=int(os.getenv("MARKET_DATA_LOOKBACK_DAYS", "750")),
            market_data_min_bars=int(os.getenv("MARKET_DATA_MIN_BARS", "120")),
            market_data_adjustment=os.getenv("MARKET_DATA_ADJUSTMENT", "qfq"),
            market_data_max_retries=int(os.getenv("MARKET_DATA_MAX_RETRIES", "2")),
            technical_indicator_version=os.getenv(
                "TECHNICAL_INDICATOR_VERSION", "tech_indicator_v1"
            ),
            kronos_mode=os.getenv("KRONOS_MODE", "mock"),
            kronos_model_name=os.getenv("KRONOS_MODEL_NAME", ""),
            kronos_device=os.getenv("KRONOS_DEVICE", "cpu"),
            kronos_source_dir=Path(
                os.getenv("KRONOS_SOURCE_DIR", "./vendor/kronos")
            ),
            kronos_lookback=int(os.getenv("KRONOS_LOOKBACK", "120")),
            kronos_sample_count=int(os.getenv("KRONOS_SAMPLE_COUNT", "1")),
            kronos_prediction_length=int(
                os.getenv("KRONOS_PREDICTION_LENGTH", "5")
            ),
            kronos_timeout_seconds=float(
                os.getenv("KRONOS_TIMEOUT_SECONDS", "360")
            ),
            kronos_queue_timeout_seconds=float(
                os.getenv("KRONOS_QUEUE_TIMEOUT_SECONDS", "420")
            ),
            technical_research_profile=os.getenv(
                "TECHNICAL_RESEARCH_PROFILE", "technical_research"
            ),
            technical_assembly_profile=os.getenv(
                "TECHNICAL_ASSEMBLY_PROFILE", "technical_assembly"
            ),
            technical_workflow_version=os.getenv(
                "TECHNICAL_WORKFLOW_VERSION", "technical_v1"
            ),
            fundamental_data_mode=os.getenv("FUNDAMENTAL_DATA_MODE", "mock"),
            fundamental_data_provider=os.getenv("FUNDAMENTAL_DATA_PROVIDER", "akshare"),
            fundamental_data_max_retries=int(os.getenv("FUNDAMENTAL_DATA_MAX_RETRIES", "2")),
            research_search_mode=os.getenv("RESEARCH_SEARCH_MODE", "mock"),
            research_search_provider=os.getenv("RESEARCH_SEARCH_PROVIDER", ""),
            research_search_providers=[
                token.strip()
                for token in os.getenv("RESEARCH_SEARCH_PROVIDERS", "").split(",")
                if token.strip()
            ],
            research_search_api_key_env_name=os.getenv("RESEARCH_SEARCH_API_KEY_ENV_NAME", ""),
            research_search_max_results=int(os.getenv("RESEARCH_SEARCH_MAX_RESULTS", "8")),
            research_source_timeout=float(os.getenv("RESEARCH_SOURCE_TIMEOUT", "30")),
            research_max_source_chars=int(os.getenv("RESEARCH_MAX_SOURCE_CHARS", "20000")),
            research_max_pdf_bytes=int(os.getenv("RESEARCH_MAX_PDF_BYTES", "20000000")),
            research_reader=os.getenv("RESEARCH_READER", "none"),
            research_reader_api_key_env_name=os.getenv("RESEARCH_READER_API_KEY_ENV_NAME", ""),
            research_reader_timeout=float(os.getenv("RESEARCH_READER_TIMEOUT", "30")),
            findkg_data_dir=Path(os.getenv("FINDKG_DATA_DIR", "./data/findkg/FinDKG")),
            financial_metric_version=os.getenv("FINANCIAL_METRIC_VERSION", "financial_metric_v1"),
            valuation_script_version=os.getenv("VALUATION_SCRIPT_VERSION", "valuation_v1"),
            fundamental_lead_profile=os.getenv("FUNDAMENTAL_LEAD_PROFILE", "fundamental_lead"),
            business_research_profile=os.getenv("BUSINESS_RESEARCH_PROFILE", "business_research"),
            industry_research_profile=os.getenv("INDUSTRY_RESEARCH_PROFILE", "industry_research"),
            deep_research_profile=os.getenv("DEEP_RESEARCH_PROFILE", "deep_research"),
            financial_research_profile=os.getenv("FINANCIAL_RESEARCH_PROFILE", "financial_research"),
            valuation_research_profile=os.getenv("VALUATION_RESEARCH_PROFILE", "valuation_research"),
            lead_synthesis_profile=os.getenv("LEAD_SYNTHESIS_PROFILE", "lead_synthesis"),
            writer_planning_profile=os.getenv("WRITER_PLANNING_PROFILE", "writer_planning"),
            fundamental_writer_profile=os.getenv("FUNDAMENTAL_WRITER_PROFILE", "fundamental_writer"),
            final_synthesis_profile=os.getenv("FINAL_SYNTHESIS_PROFILE", "final_synthesis"),
            chart_data_extractor_profile=os.getenv("CHART_DATA_EXTRACTOR_PROFILE", "chart_data_extractor"),
            fundamental_workflow_version=os.getenv("FUNDAMENTAL_WORKFLOW_VERSION", "fundamental_v1"),
        )
