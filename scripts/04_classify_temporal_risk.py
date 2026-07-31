from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 2. Path configuration
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORT_DIR = PROJECT_ROOT / "reports"

INPUT_PATH = PROCESSED_DIR / "fvi_fdi_scores.csv"
OUTPUT_PATH = PROCESSED_DIR / "temporal_risk_classification.csv"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("temporal_risk_classification")

# --------------------------------------------------------------------------- #
# 3. Constants
# --------------------------------------------------------------------------- #

EXPECTED_DISTRICT_COUNT = 25
DISTRICT_COL = "district"
REQUIRED_COLUMNS = [DISTRICT_COL, "fvi_2020", "fvi_2022", "fdi_2020", "fdi_2022"]

# 위험 유형 이름 (최종 보고서 명세)
STABLE = "Stable"
LATENT_RISK = "Latent-Risk"
CHRONIC_RISK = "Chronic-Risk"
MISCLASSIFIED = "Misclassified"


# --------------------------------------------------------------------------- #
# 4. Input validation functions
# --------------------------------------------------------------------------- #

def load_fvi_fdi_scores(path: Path) -> pd.DataFrame:
    """FVI/FDI 점수 CSV를 로드한다."""
    if not path.exists():
        raise FileNotFoundError(
            f"FVI/FDI 점수 파일을 찾을 수 없습니다: {path} (먼저 03_calculate_fvi_fdi.py를 실행하세요)"
        )
    df = pd.read_csv(path, encoding="utf-8-sig")
    logger.info("Loaded FVI/FDI scores: %s (%d rows)", path.name, len(df))
    return df


def validate_input_frame(df: pd.DataFrame) -> None:
    """필수 컬럼, 자치구 수, 중복, NaN/inf를 검증한다."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"누락된 필수 컬럼: {missing}")
    if df[DISTRICT_COL].duplicated().any():
        raise ValueError("자치구 중복 발견")
    if len(df) != EXPECTED_DISTRICT_COUNT:
        raise ValueError(f"자치구 수가 {EXPECTED_DISTRICT_COUNT}개가 아닙니다 (실제 {len(df)}개)")
    numeric = df[["fvi_2020", "fvi_2022", "fdi_2020", "fdi_2022"]]
    if numeric.isnull().any().any():
        raise ValueError("FVI/FDI 값에 결측치가 있습니다")
    if np.isinf(numeric.to_numpy(dtype=float)).any():
        raise ValueError("FVI/FDI 값에 inf가 포함되어 있습니다")


# --------------------------------------------------------------------------- #
# 5. Risk-flag functions (연도별 quadrant 판정)
# --------------------------------------------------------------------------- #

def flag_high_risk(fvi: pd.Series, fdi: pd.Series) -> pd.Series:
    """해당 연도 평균 기준, FVI가 평균 이상이면서 FDI가 평균 미만인 자치구를 '고위험'으로 표시한다.

    이는 기존 PCD/FVI-FDI 매트릭스의 'Dangerous' 사분면(취약성 높음 + 방어력 낮음)과
    동일한 정의이며, 이 저장소의 02/03 스크립트가 이미 사용하는 사분면 분류 규약을
    그대로 연도별 FVI-FDI 매트릭스에 적용한 것이다.
    """
    return (fvi >= fvi.mean()) & (fdi < fdi.mean())


# --------------------------------------------------------------------------- #
# 6. Temporal transition classification functions
# --------------------------------------------------------------------------- #

def classify_transition(risk_2020: bool, risk_2022: bool) -> str:
    """2020->2022 위험 상태 전이를 4가지 유형 중 하나로 분류한다.

    정의 (이번 구현에서 새로 확정한 규칙 - 근거는 검증 보고서 참고):
        - Stable:        2020, 2022 모두 고위험 아님 (지속적으로 안전)
        - Chronic-Risk:  2020, 2022 모두 고위험 (지속적 고위험)
        - Latent-Risk:   2020 고위험 아님 -> 2022 고위험 (새로 부상하는 위험)
        - Misclassified: 2020 고위험 -> 2022 고위험 아님 (이전 고위험 판정과 불일치,
                          재검토가 필요한 사례로 취급)
    """
    if not risk_2020 and not risk_2022:
        return STABLE
    if risk_2020 and risk_2022:
        return CHRONIC_RISK
    if not risk_2020 and risk_2022:
        return LATENT_RISK
    return MISCLASSIFIED  # risk_2020 and not risk_2022


def build_classification(df: pd.DataFrame) -> pd.DataFrame:
    """FVI/FDI 점수로부터 연도별 위험 플래그와 최종 전이 유형을 계산한다."""
    result = df[[DISTRICT_COL, "fvi_2020", "fvi_2022", "fdi_2020", "fdi_2022"]].copy()
    result["high_risk_2020"] = flag_high_risk(df["fvi_2020"], df["fdi_2020"])
    result["high_risk_2022"] = flag_high_risk(df["fvi_2022"], df["fdi_2022"])
    result["risk_transition_type"] = [
        classify_transition(r20, r22) for r20, r22 in zip(result["high_risk_2020"], result["high_risk_2022"])
    ]
    return result


# --------------------------------------------------------------------------- #
# 7. (해당 없음 - FDI 관련 별도 계산 함수 없음, 03단계 산출값을 그대로 사용)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 8. Reference validation functions
# --------------------------------------------------------------------------- #

# 이 분류 단계에 대한 발표자료/보고서 참조값은 프로젝트 전체에서 발견되지 않았다
# (원본 노트북에는 계산 로직 자체가 없었다). 따라서 참조값 검증은 수행하지 않는다.


# --------------------------------------------------------------------------- #
# 9. Output export functions
# --------------------------------------------------------------------------- #

def ensure_output_dirs() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def export_classification(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Saved temporal risk classification: %s", path)


# --------------------------------------------------------------------------- #
# 10. main()
# --------------------------------------------------------------------------- #

def main() -> None:
    """2020->2022 위험 전이 분류 파이프라인을 실행한다."""
    ensure_output_dirs()

    df = load_fvi_fdi_scores(INPUT_PATH)
    validate_input_frame(df)

    classification = build_classification(df)
    export_classification(classification, OUTPUT_PATH)

    counts = classification["risk_transition_type"].value_counts().to_dict()
    logger.info("Temporal risk classification completed.")
    logger.info("Districts processed: %d", len(classification))
    logger.info("Transition type counts: %s", counts)
    logger.info("Output saved to: %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
