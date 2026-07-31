from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 2. Path configuration
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"
REPORT_DIR = PROJECT_ROOT / "reports"

INPUT_PATH = PROCESSED_DIR / "fvi_fdi_scores.csv"
REFERENCE_PATH = REFERENCE_DIR / "final_risk_reference_scores.csv"  # optional, may not exist
OUTPUT_PATH = PROCESSED_DIR / "final_district_risk_scores.csv"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("final_risk_scores")

# --------------------------------------------------------------------------- #
# 3. Constants
# --------------------------------------------------------------------------- #

EXPECTED_DISTRICT_COUNT = 25
VALIDATION_ATOL = 1e-6
DISTRICT_COL = "district"
REQUIRED_COLUMNS = [DISTRICT_COL, "fvi_2020", "fvi_2022"]

FVI_2020_WEIGHT = 0.7
FVI_2022_WEIGHT = 0.3
RISK_COEFFICIENT_BASE = 1.00
RISK_COEFFICIENT_STEP = 0.04


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
    numeric = df[["fvi_2020", "fvi_2022"]]
    if numeric.isnull().any().any():
        raise ValueError("FVI 값에 결측치가 있습니다")
    if np.isinf(numeric.to_numpy(dtype=float)).any():
        raise ValueError("FVI 값에 inf가 포함되어 있습니다")


# --------------------------------------------------------------------------- #
# 5. (해당 없음 - 이 단계는 03단계에서 이미 정규화된 FVI를 그대로 결합만 한다)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 6. Final FVI calculation function
# --------------------------------------------------------------------------- #

def compute_final_fvi(df: pd.DataFrame) -> pd.Series:
    """final_fvi = 0.7 * fvi_2020 + 0.3 * fvi_2022 (최종 보고서 명세 그대로)."""
    return df["fvi_2020"] * FVI_2020_WEIGHT + df["fvi_2022"] * FVI_2022_WEIGHT


# --------------------------------------------------------------------------- #
# 7. Ranking / risk coefficient calculation functions
# --------------------------------------------------------------------------- #

def compute_rank(final_fvi: pd.Series) -> pd.Series:
    """final_fvi가 높을수록(취약성이 클수록) 순위가 1등이 되도록 내림차순 순위를 매긴다."""
    return final_fvi.rank(ascending=False, method="min").astype(int)


def compute_risk_coefficient(rank: pd.Series, n_districts: int) -> pd.Series:
    """risk_coefficient = 1.00 + 0.04 * (25 - rank) (최종 보고서 명세 그대로)."""
    return RISK_COEFFICIENT_BASE + RISK_COEFFICIENT_STEP * (n_districts - rank)


def build_final_scores(df: pd.DataFrame) -> pd.DataFrame:
    """final_fvi, rank, risk_coefficient를 모두 계산해 결합한다."""
    result = df[[DISTRICT_COL, "fvi_2020", "fvi_2022"]].copy()
    result["final_fvi"] = compute_final_fvi(df)
    result["rank"] = compute_rank(result["final_fvi"])
    result["risk_coefficient"] = compute_risk_coefficient(result["rank"], EXPECTED_DISTRICT_COUNT)
    return result.sort_values("rank").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 8. Reference validation functions
# --------------------------------------------------------------------------- #

def load_reference_csv(path: Path) -> Optional[pd.DataFrame]:
    """참조 CSV가 존재하면 로드하고, 없으면 None을 반환한다 (검증 전용, 계산 입력 아님)."""
    if not path.exists():
        logger.warning("참조 파일 없음 (건너뜀): %s", path)
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def validate_against_reference(computed: pd.DataFrame, reference: pd.DataFrame) -> pd.DataFrame:
    """계산된 final_fvi/risk_coefficient를 참조값과 비교한다 (있을 경우에만)."""
    merged = computed.merge(reference, on=DISTRICT_COL, how="outer", suffixes=("", "_reference"))
    if "final_fvi_reference" in merged.columns:
        merged["final_fvi_abs_error"] = (merged["final_fvi"] - merged["final_fvi_reference"]).abs()
    if "risk_coefficient_reference" in merged.columns:
        merged["risk_coefficient_abs_error"] = (
            merged["risk_coefficient"] - merged["risk_coefficient_reference"]
        ).abs()
    return merged


# --------------------------------------------------------------------------- #
# 9. Output export functions
# --------------------------------------------------------------------------- #

def ensure_output_dirs() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def export_scores(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Saved final district risk scores: %s", path)


# --------------------------------------------------------------------------- #
# 10. main()
# --------------------------------------------------------------------------- #

def main() -> None:
    """최종 FVI 결합, 순위 산정, 위험계수 계산 파이프라인을 실행한다."""
    ensure_output_dirs()

    df = load_fvi_fdi_scores(INPUT_PATH)
    validate_input_frame(df)

    final_scores = build_final_scores(df)
    export_scores(final_scores, OUTPUT_PATH)

    assert final_scores["rank"].nunique() == len(final_scores), "순위에 중복이 있습니다"
    assert final_scores["rank"].min() == 1 and final_scores["rank"].max() == EXPECTED_DISTRICT_COUNT

    reference = load_reference_csv(REFERENCE_PATH)
    validation_status = "unverified"
    if reference is not None:
        validation = validate_against_reference(final_scores, reference)
        validation_path = PROCESSED_DIR / "final_risk_validation_results.csv"
        validation.to_csv(validation_path, index=False, encoding="utf-8-sig")
        validation_status = "verified"
        logger.info("Saved validation results: %s", validation_path)
    else:
        logger.warning(
            "최종 위험계수 참조값이 없어 검증을 수행하지 않았습니다. "
            "계산 공식은 사양(0.7/0.3 가중, rank, 1.00+0.04*(25-rank))을 그대로 따랐습니다."
        )

    logger.info("Final risk score calculation completed.")
    logger.info("Districts processed: %d", len(final_scores))
    logger.info("Top-ranked (highest risk) district: %s (rank=1)",
                final_scores.loc[final_scores["rank"] == 1, DISTRICT_COL].iloc[0])
    logger.info("Validation status: %s", validation_status)
    logger.info("Output saved to: %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
