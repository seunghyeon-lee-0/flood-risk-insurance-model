from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 2. Path configuration
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"
REPORT_DIR = PROJECT_ROOT / "reports"

INPUT_2020 = RAW_DIR / "district_indicators_2020.csv"
INPUT_2022 = RAW_DIR / "district_indicators_2022.csv"
REFERENCE_PATH = REFERENCE_DIR / "baseline_pcd_reference_scores.csv"
OUTPUT_PATH = PROCESSED_DIR / "baseline_pcd_scores.csv"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("baseline_pcd")

# --------------------------------------------------------------------------- #
# 3. Constants
# --------------------------------------------------------------------------- #

EXPECTED_DISTRICT_COUNT = 25
VALIDATION_ATOL = 1e-6
DISTRICT_COL = "자치구"

FPO_WEIGHTS = {
    "population": ("인구", 0.500),
    "asset": ("자산", 0.125),
    "urbanization_rate": ("도시화율", 0.250),
    "infra_density": ("사회기반시설밀도 (시설 수/㎢)", 0.125),
}
FRI_WEIGHT = 0.5
EXTERNAL_DEFENSE_COL = "외수방어능력 (m)"
INTERNAL_DEFENSE_COL = "내수방어능력 (m³/min)"
FACILITY_CAPACITY_COL = "방어시설용량 (h)"

REQUIRED_COLUMNS = [DISTRICT_COL, EXTERNAL_DEFENSE_COL, INTERNAL_DEFENSE_COL, FACILITY_CAPACITY_COL] + [
    col for col, _ in FPO_WEIGHTS.values()
]


# --------------------------------------------------------------------------- #
# 4. Input validation functions
# --------------------------------------------------------------------------- #

def load_year_csv(path: Path, year_label: str) -> pd.DataFrame:
    """지정된 연도의 자치구 지표 CSV를 로드한다."""
    if not path.exists():
        raise FileNotFoundError(f"[{year_label}] 입력 파일을 찾을 수 없습니다: {path}")
    df = pd.read_csv(path, encoding="utf-8")
    logger.info("Loaded %s input: %s (%d rows)", year_label, path.name, len(df))
    return df


def validate_input_frame(df: pd.DataFrame, year_label: str) -> None:
    """필수 컬럼과 자치구 수를 검증한다."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"[{year_label}] 누락된 필수 컬럼: {missing}")
    if df[DISTRICT_COL].duplicated().any():
        raise ValueError(f"[{year_label}] 자치구 중복 발견")
    if len(df) != EXPECTED_DISTRICT_COUNT:
        raise ValueError(f"[{year_label}] 자치구 수가 {EXPECTED_DISTRICT_COUNT}개가 아닙니다 (실제 {len(df)}개)")
    if np.isinf(df[REQUIRED_COLUMNS[1:]].to_numpy(dtype=float)).any():
        raise ValueError(f"[{year_label}] 입력 데이터에 inf 값이 포함되어 있습니다")


# --------------------------------------------------------------------------- #
# 5. Normalization functions
# --------------------------------------------------------------------------- #

def sum_normalize(series: pd.Series) -> pd.Series:
    """합계 기준 정규화."""
    total = series.sum()
    return series / total if total != 0 else series * 0


# --------------------------------------------------------------------------- #
# 6. FPO / FRI / PFD (baseline damage-potential matrix) functions
# --------------------------------------------------------------------------- #

def compute_fpo(df: pd.DataFrame) -> pd.Series:
    """FPO(홍수피해 잠재성) = 인구/자산/도시화율/사회기반시설밀도의 가중합(각 합계 정규화)."""
    fpo = pd.Series(0.0, index=df.index)
    for col, weight in FPO_WEIGHTS.values():
        fpo += sum_normalize(df[col]) * weight
    return fpo


def compute_fri(df: pd.DataFrame) -> pd.Series:
    """FRI(위험성) = (1 - 외수방어능력 정규화) * 0.5 - 외수 방어력이 낮을수록 취약성이 커짐."""
    external_defense_norm = sum_normalize(df[EXTERNAL_DEFENSE_COL])
    return (1 - external_defense_norm) * FRI_WEIGHT


def compute_pfd(fpo: pd.Series, fri: pd.Series) -> pd.Series:
    """PFD(홍수피해잠재능) = sqrt(FPO) * sqrt(FRI)."""
    return (fpo ** 0.5) * (fri ** 0.5)


# --------------------------------------------------------------------------- #
# 7. Protection Capacity (X축) function
# --------------------------------------------------------------------------- #

def compute_protection_capacity(df: pd.DataFrame) -> pd.Series:
    """Protection Capacity = 0.5 * 내수방어능력_정규화 + 0.5 * 방어시설용량_정규화."""
    internal_norm = sum_normalize(df[INTERNAL_DEFENSE_COL])
    facility_norm = sum_normalize(df[FACILITY_CAPACITY_COL])
    return 0.5 * (internal_norm + facility_norm)


def classify_quadrant(protection_capacity: pd.Series, pfd: pd.Series) -> pd.Series:
    """평균값 기준으로 4분면(Mess/Dangerous/Safe/Well-Protected)을 분류한다."""
    x_mean, y_mean = protection_capacity.mean(), pfd.mean()

    def _classify(x: float, y: float) -> str:
        if x >= x_mean and y >= y_mean:
            return "Mess"
        if x < x_mean and y >= y_mean:
            return "Dangerous"
        if x < x_mean and y < y_mean:
            return "Safe"
        return "Well-Protected"

    return pd.Series(
        [_classify(x, y) for x, y in zip(protection_capacity, pfd)], index=protection_capacity.index
    )


def compute_baseline_pcd(df: pd.DataFrame) -> pd.DataFrame:
    """한 연도의 데이터로부터 FPO/FRI/PFD/Protection Capacity/사분면을 모두 계산한다."""
    result = df[[DISTRICT_COL]].copy()
    fpo = compute_fpo(df)
    fri = compute_fri(df)
    pfd = compute_pfd(fpo, fri)
    protection_capacity = compute_protection_capacity(df)

    result["fpo"] = fpo
    result["fri"] = fri
    result["pfd"] = pfd
    result["protection_capacity"] = protection_capacity
    result["quadrant"] = classify_quadrant(protection_capacity, pfd)
    return result


# --------------------------------------------------------------------------- #
# 8. Reference validation functions
# --------------------------------------------------------------------------- #

def load_reference_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        logger.warning("참조 파일 없음 (건너뜀): %s", path)
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def validate_against_reference(computed: pd.DataFrame, reference: pd.DataFrame, year: int) -> pd.DataFrame:
    """계산값(fpo/fri/pfd)과 참조값을 자치구 기준으로 병합하고 오차를 계산한다."""
    ref_year = reference.loc[reference["year"] == year, ["district", "fpo", "fri", "pfd"]]
    merged = computed.rename(columns={DISTRICT_COL: "district"}).merge(
        ref_year, on="district", how="outer", suffixes=("", "_reference")
    )
    for col in ["fpo", "fri", "pfd"]:
        merged[f"{col}_abs_error"] = (merged[col] - merged[f"{col}_reference"]).abs()
    merged["validation_passed"] = (
        (merged["fpo_abs_error"] <= VALIDATION_ATOL)
        & (merged["fri_abs_error"] <= VALIDATION_ATOL)
        & (merged["pfd_abs_error"] <= VALIDATION_ATOL)
    )
    max_pfd_error = merged["pfd_abs_error"].max()
    logger.info(
        "[%d] max_pfd_abs_error=%.3e all_passed=%s", year, max_pfd_error, bool(merged["validation_passed"].all())
    )
    return merged


# --------------------------------------------------------------------------- #
# 9. Output export functions
# --------------------------------------------------------------------------- #

def ensure_output_dirs() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def export_scores(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Saved baseline PCD scores: %s", path)


# --------------------------------------------------------------------------- #
# 10. main()
# --------------------------------------------------------------------------- #

def main() -> None:
    """기존 PCD Matrix(FPO/FRI/PFD/Protection Capacity) 계산 파이프라인을 실행한다."""
    ensure_output_dirs()

    df_2020 = load_year_csv(INPUT_2020, "2020")
    df_2022 = load_year_csv(INPUT_2022, "2022")
    validate_input_frame(df_2020, "2020")
    validate_input_frame(df_2022, "2022")

    pcd_2020 = compute_baseline_pcd(df_2020).assign(year=2020)
    pcd_2022 = compute_baseline_pcd(df_2022).assign(year=2022)
    combined = pd.concat([pcd_2020, pcd_2022], ignore_index=True).rename(columns={DISTRICT_COL: "district"})
    export_scores(combined, OUTPUT_PATH)

    reference = load_reference_csv(REFERENCE_PATH)
    all_passed = None
    if reference is not None:
        validation_2020 = validate_against_reference(pcd_2020, reference, 2020)
        validation_2022 = validate_against_reference(pcd_2022, reference, 2022)
        all_passed = bool(validation_2020["validation_passed"].all() and validation_2022["validation_passed"].all())

    logger.info("Baseline PCD calculation completed.")
    logger.info("Districts processed: %d (2020) / %d (2022)", len(pcd_2020), len(pcd_2022))
    logger.info("Validation passed: %s", all_passed)
    logger.info("Output saved to: %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
