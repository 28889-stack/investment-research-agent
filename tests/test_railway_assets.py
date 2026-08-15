from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_docker_image_runs_the_single_service_and_includes_live_kronos_runtime() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "requirements-kronos.txt" in dockerfile
    assert "config/kronos-source.json" in dockerfile
    assert 'CMD ["python", "-m", "app.service"]' in dockerfile
    assert "HF_HOME=/app/data/huggingface" in dockerfile


def test_railway_configuration_declares_a_web_healthcheck() -> None:
    railway_config = (PROJECT_ROOT / "railway.toml").read_text(encoding="utf-8")

    assert 'healthcheckPath = "/api/health"' in railway_config
