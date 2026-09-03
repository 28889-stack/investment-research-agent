from app.fundamental.report import _FINANCIAL_REPORT_STYLE
from app.technical.report import TECHNICAL_REPORT_STYLE
from app.fundamental.visuals import COLORS


def _assert_institutional_vision_style(css: str, shell: str) -> None:
    compact = css.replace(" ", "").replace("\n", "").lower()
    assert "--system-blue:#0a84ff" in compact
    assert "--vision-purple:#7b61ff" in compact
    assert "--text:#202124" in compact
    assert f".{shell}{{max-width:1160px" in compact
    assert "padding:48px56px80px" in compact
    assert "font-variant-numeric:tabular-nums" in compact
    assert "font-size:32px" in compact
    assert "font-size:25px" in compact
    assert "backdrop-filter:blur(" in compact
    assert "radial-gradient(" in compact
    assert "box-shadow:" in compact
    assert "border-radius:" in compact
    assert ".chart-component" in compact
    assert "@mediaprint" in compact
    assert "break-inside:avoid" in compact
    assert "backdrop-filter:none" in compact
    assert "box-shadow:none" in compact


def test_fundamental_html_uses_institutional_research_visual_system() -> None:
    _assert_institutional_vision_style(_FINANCIAL_REPORT_STYLE, "report-shell")


def test_technical_html_uses_same_institutional_research_visual_system() -> None:
    _assert_institutional_vision_style(TECHNICAL_REPORT_STYLE, "technical-report-shell")


def test_chart_palette_uses_one_primary_and_low_saturation_support_colors() -> None:
    assert COLORS == ("#163A5F", "#6F8294", "#202124", "#AAB2BB")
