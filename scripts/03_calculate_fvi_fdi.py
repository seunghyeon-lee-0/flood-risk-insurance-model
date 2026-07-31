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
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"
REPORT_DIR = PROJECT_ROOT / "reports"

INPUT_2020 = RAW_DIR / "district_indicators_2020.csv"
INPUT_2022 = RAW_DIR / "district_indicators_2022.csv"
FVI_REFERENCE_PATH = REFERENCE_DIR / "fvi_reference_scores.csv"
FDI_REFERENCE_PATH = REFERENCE_DIR / "fdi_reference_scores.csv"  # optional, may not exist

OUTPUT_SCORES_PATH = PROCESSED_DIR / "fvi_fdi_scores.csv"
OUTPUT_FVI_VALIDATION_PATH = PROCESSED_DIR / "fvi_validation_results.csv"
OUTPUT_FDI_VALIDATION_PATH = PROCESSED_DIR / "fdi_validation_results.csv"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("fvi_fdi")

# --------------------------------------------------------------------------- #
# 3. Constants
# --------------------------------------------------------------------------- #

EXPECTED_DISTRICT_COUNT = 25
VALIDATION_ATOL = 1e-6

DISTRICT_COL = "자치구"

# Group 1 - 과거 홍수 피해 규모 (raw column names -> AHP weight)
DAMAGE_GROUP_COLUMNS = {
    "flooded_household_ratio": 0.2595,       # 침수 가구 비율
    "damage_per_area": 0.2431,               # 단위면적당 피해액
    "affected_population_ratio": 0.4974,     # 피해 인구 비율
}

# Group 2 - 홍수에 취약한 지역 특성
REGIONAL_GROUP_COLUMNS = {
    "population_density": 0.2250,        # 인구 밀도
    "impervious_surface_ratio": 0.4157,  # 불투수면적 비율
    "household_density": 0.3592,         # 가구 밀도
}

# Group 3 - 홍수 대응 인프라 및 회복력 (FDI 전용, FVI에서는 제외)
INFRASTRUCTURE_GROUP_COLUMNS = {
    "sewer_length": 0.382,               # 하수관 길이
    "financial_independence": 0.339,     # 재정자립도
    "num_firefighters": 0.279,           # 소방공무원 수
}

# 상위 그룹(FVI) 원본 AHP 가중치 - 최종 변경 노트북의 재정규화 방식대로 사용
DAMAGE_GROUP_RAW_WEIGHT = 0.2321
REGIONAL_GROUP_RAW_WEIGHT = 0.4274
DAMAGE_GROUP_WEIGHT = DAMAGE_GROUP_RAW_WEIGHT / (DAMAGE_GROUP_RAW_WEIGHT + REGIONAL_GROUP_RAW_WEIGHT)
REGIONAL_GROUP_WEIGHT = REGIONAL_GROUP_RAW_WEIGHT / (DAMAGE_GROUP_RAW_WEIGHT + REGIONAL_GROUP_RAW_WEIGHT)

# 원본 컬럼명 -> 내부 계산용 컬럼명 매핑 (하드코딩된 한글 컬럼명을 한 곳에서만 관리)
RAW_COLUMN_MAP = {
    "flooded_households": "침수세대 수 (세대)",
    "population": "인구",
    "affected_population": "피해인구",
    "damage_per_area": "단위면적당피해규모 (천원/km²)",
    "population_density": "인구밀도 (명/㎢)",
    "impervious_surface_ratio": "불투수면적 비율(퍼센트)",
    "household_density": "가구 밀도(퍼센트)",
    "sewer_length": "하수관거 길이",
    "financial_independence": "재정자립도",
    "num_firefighters": "소방인력 수",
    "external_defense": "외수방어능력 (m)",
    "internal_defense": "내수방어능력 (m³/min)",
    "facility_capacity": "방어시설용량 (h)",
}

REQUIRED_RAW_COLUMNS = [DISTRICT_COL] + list(RAW_COLUMN_MAP.values())


# --------------------------------------------------------------------------- #
# 4. Input validation functions
# --------------------------------------------------------------------------- #

def load_year_csv(path: Path, year_label: str) -> pd.DataFrame:
    """지정된 연도의 인코딩된 자치구 CSV를 로드한다.

    Raises:
        FileNotFoundError: 입력 파일이 존재하지 않는 경우.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"[{year_label}] 입력 파일을 찾을 수 없습니다: {path}"
        )
    df = pd.read_csv(path, encoding="utf-8")
    logger.info("Loaded %s input: %s (%d rows)", year_label, path.name, len(df))
    return df


def validate_input_frame(df: pd.DataFrame, year_label: str) -> None:
    """필수 컬럼, 자치구 개수, 중복, 결측치/inf를 검증한다.

    Raises:
        ValueError: 필수 컬럼 누락, 자치구 수 불일치, 자치구 중복 시.
    """
    missing_columns = [col for col in REQUIRED_RAW_COLUMNS if col not in df.columns]
    if missing_columns:
        raise ValueError(f"[{year_label}] 누락된 필수 컬럼: {missing_columns}")

    if df[DISTRICT_COL].duplicated().any():
        duplicated = df.loc[df[DISTRICT_COL].duplicated(), DISTRICT_COL].tolist()
        raise ValueError(f"[{year_label}] 자치구 중복 발견: {duplicated}")

    if len(df) != EXPECTED_DISTRICT_COUNT:
        raise ValueError(
            f"[{year_label}] 자치구 수가 {EXPECTED_DISTRICT_COUNT}개가 아닙니다 (실제 {len(df)}개)"
        )

    numeric_columns = [c for c in REQUIRED_RAW_COLUMNS if c != DISTRICT_COL]
    numeric_df = df[numeric_columns]
    if numeric_df.isnull().any().any():
        null_cols = numeric_df.columns[numeric_df.isnull().any()].tolist()
        logger.warning("[%s] 결측치가 있는 컬럼: %s", year_label, null_cols)

    if np.isinf(numeric_df.to_numpy(dtype=float)).any():
        raise ValueError(f"[{year_label}] 입력 데이터에 inf 값이 포함되어 있습니다")


# --------------------------------------------------------------------------- #
# 5. Normalization functions
# --------------------------------------------------------------------------- #

def safe_sum_normalize(series: pd.Series) -> pd.Series:
    """합계 기준 정규화. 합이 0이면 전부 0을 반환해 0/0 NaN을 방지한다."""
    total = series.sum()
    if total == 0:
        return series * 0
    return series / total


def safe_max_normalize(series: pd.Series) -> pd.Series:
    """최댓값 기준 정규화 (0~1 스케일). 최댓값이 0이면 전부 0을 반환한다."""
    max_value = series.max()
    if max_value == 0:
        return series * 0
    return series / max_value


def min_max_normalize(series: pd.Series) -> pd.Series:
    """Min-Max 정규화 (0~1 스케일). 값이 모두 동일하면 전부 0을 반환한다."""
    value_range = series.max() - series.min()
    if value_range == 0:
        return series * 0
    return (series - series.min()) / value_range


# --------------------------------------------------------------------------- #
# 6. FVI calculation functions
# --------------------------------------------------------------------------- #

def build_fvi_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """FVI에 필요한 6개 세부 지표를 계산하고 합계 정규화한다.

    사용하는 지표는 과거피해 그룹(침수가구비율, 단위면적당피해규모, 피해인구비율)과
    지역특성 그룹(인구밀도, 불투수면적비율, 가구밀도)뿐이며, 인프라 그룹은 포함하지 않는다.
    """
    out = df[[DISTRICT_COL]].copy()

    flooded_household_ratio = df[RAW_COLUMN_MAP["flooded_households"]] / df[RAW_COLUMN_MAP["population"]]
    affected_population_ratio = df[RAW_COLUMN_MAP["affected_population"]] / df[RAW_COLUMN_MAP["population"]]

    out["flooded_household_ratio_norm"] = safe_sum_normalize(flooded_household_ratio)
    out["damage_per_area_norm"] = safe_sum_normalize(df[RAW_COLUMN_MAP["damage_per_area"]])
    out["affected_population_ratio_norm"] = safe_sum_normalize(affected_population_ratio)

    out["population_density_norm"] = safe_sum_normalize(df[RAW_COLUMN_MAP["population_density"]])
    out["impervious_surface_ratio_norm"] = safe_sum_normalize(df[RAW_COLUMN_MAP["impervious_surface_ratio"]])
    out["household_density_norm"] = safe_sum_normalize(df[RAW_COLUMN_MAP["household_density"]])

    return out


def compute_fvi(df: pd.DataFrame) -> pd.DataFrame:
    """정본(canonical) FVI 공식을 계산한다 (최종 변경 노트북 방식).

    계산 순서:
        1. 세부 지표 정규화 (build_fvi_indicator_frame)
        2. 과거피해 그룹 내부 AHP 가중합
        3. 지역특성 그룹 내부 AHP 가중합
        4. 재정규화된 상위 그룹 가중치 적용
        5. 두 그룹 결합
        6. 25개 자치구 전체 합 기준 최종 정규화 (sum(fvi) ≈ 1)
    """
    indicators = build_fvi_indicator_frame(df)

    damage_score = (
        indicators["flooded_household_ratio_norm"] * DAMAGE_GROUP_COLUMNS["flooded_household_ratio"]
        + indicators["damage_per_area_norm"] * DAMAGE_GROUP_COLUMNS["damage_per_area"]
        + indicators["affected_population_ratio_norm"] * DAMAGE_GROUP_COLUMNS["affected_population_ratio"]
    )
    regional_score = (
        indicators["population_density_norm"] * REGIONAL_GROUP_COLUMNS["population_density"]
        + indicators["impervious_surface_ratio_norm"] * REGIONAL_GROUP_COLUMNS["impervious_surface_ratio"]
        + indicators["household_density_norm"] * REGIONAL_GROUP_COLUMNS["household_density"]
    )

    combined_score = damage_score * DAMAGE_GROUP_WEIGHT + regional_score * REGIONAL_GROUP_WEIGHT

    result = df[[DISTRICT_COL]].copy()
    result["damage_score"] = damage_score
    result["regional_score"] = regional_score
    result["fvi"] = safe_sum_normalize(combined_score)
    return result


# --------------------------------------------------------------------------- #
# 7. FDI calculation functions
# --------------------------------------------------------------------------- #

def compute_fdi_ahp_weighted(df: pd.DataFrame) -> pd.DataFrame:
    """FDI 정본 후보: AHP 가중 홍수대응인프라 점수.

    하수관 길이 / 재정자립도 / 소방공무원 수를 합계 정규화한 뒤
    AHP 가중치(0.382 / 0.339 / 0.279)로 가중합한다.
    (기존 '최종 변경' 노트북은 이 자리에 하수관 길이 대신 방어용량을 대입한 변형이
    존재했으나, 이번 정본 구현은 사용자 결정에 따라 원래 AHP 그룹 구성인
    하수관 길이를 그대로 사용한다 — 근거는 검증 보고서 14~16번 항목 참고.)
    """
    sewer_norm = safe_sum_normalize(df[RAW_COLUMN_MAP["sewer_length"]])
    financial_norm = safe_sum_normalize(df[RAW_COLUMN_MAP["financial_independence"]])
    firefighters_norm = safe_sum_normalize(df[RAW_COLUMN_MAP["num_firefighters"]])

    fdi = (
        sewer_norm * INFRASTRUCTURE_GROUP_COLUMNS["sewer_length"]
        + financial_norm * INFRASTRUCTURE_GROUP_COLUMNS["financial_independence"]
        + firefighters_norm * INFRASTRUCTURE_GROUP_COLUMNS["num_firefighters"]
    )

    result = df[[DISTRICT_COL]].copy()
    result["fdi"] = fdi
    return result


def compute_fdi_baseline_simple_average(df: pd.DataFrame) -> pd.DataFrame:
    """FDI 기각/베이스라인 후보: 단순평균 방어용량 (최최종 노트북 방식).

    외수방어능력, 내수방어능력, 방어시설용량을 각각 최댓값으로 정규화한 뒤
    가중치 없이 단순 평균한다. 정본으로 채택되지 않았으나 비교용으로 보존한다.
    """
    external_norm = safe_max_normalize(df[RAW_COLUMN_MAP["external_defense"]])
    internal_norm = safe_max_normalize(df[RAW_COLUMN_MAP["internal_defense"]])
    facility_norm = safe_max_normalize(df[RAW_COLUMN_MAP["facility_capacity"]])

    result = df[[DISTRICT_COL]].copy()
    result["fdi_baseline"] = (external_norm + internal_norm + facility_norm) / 3
    return result


# --------------------------------------------------------------------------- #
# 8. Reference validation functions
# --------------------------------------------------------------------------- #

def load_reference_csv(path: Path) -> Optional[pd.DataFrame]:
    """참조 CSV가 존재하면 로드하고, 없으면 None을 반환한다 (검증 전용, 계산 입력 아님)."""
    if not path.exists():
        logger.warning("참조 파일 없음 (건너뜀): %s", path)
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def validate_against_reference(
    computed: pd.DataFrame,
    reference: pd.DataFrame,
    computed_col: str,
    reference_col: str,
    year_label: str,
) -> pd.DataFrame:
    """계산값과 참조값을 자치구 기준으로 병합하고 오차/순위/상관계수를 계산한다.

    참조값과 불일치하더라도 계산값을 임의로 보정하지 않는다.
    """
    merged = computed[[DISTRICT_COL, computed_col]].merge(
        reference[[DISTRICT_COL, reference_col]], on=DISTRICT_COL, how="outer"
    )
    merged["abs_error"] = (merged[computed_col] - merged[reference_col]).abs()
    merged["validation_passed"] = merged["abs_error"] <= VALIDATION_ATOL

    max_error = merged["abs_error"].max()
    mean_error = merged["abs_error"].mean()
    rank_computed = merged[computed_col].rank(ascending=False)
    rank_reference = merged[reference_col].rank(ascending=False)
    rank_match = bool((rank_computed == rank_reference).all())
    spearman_corr = merged[computed_col].corr(merged[reference_col], method="spearman")

    logger.info(
        "[%s] max_abs_error=%.3e mean_abs_error=%.3e rank_match=%s spearman=%.6f all_passed=%s",
        year_label, max_error, mean_error, rank_match, spearman_corr, bool(merged["validation_passed"].all()),
    )

    try:
        np.testing.assert_allclose(
            merged[computed_col].to_numpy(dtype=float),
            merged[reference_col].to_numpy(dtype=float),
            atol=VALIDATION_ATOL,
        )
    except AssertionError as exc:
        logger.warning("[%s] 참조값과 atol=%.0e 이내로 일치하지 않습니다: %s", year_label, VALIDATION_ATOL, exc)

    return merged


# --------------------------------------------------------------------------- #
# 9. Output export functions
# --------------------------------------------------------------------------- #

def ensure_output_dirs() -> None:
    """출력 폴더가 없으면 생성한다."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def export_scores(scores_df: pd.DataFrame, path: Path) -> None:
    """최종 FVI/FDI 점수 CSV를 저장한다 (검증 관련 컬럼은 포함하지 않음)."""
    scores_df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Saved scores: %s", path)


def export_validation_results(validation_df: pd.DataFrame, path: Path) -> None:
    """검증 결과 CSV를 별도 파일로 저장한다."""
    validation_df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Saved validation results: %s", path)


# --------------------------------------------------------------------------- #
# 10. main()
# --------------------------------------------------------------------------- #

def main() -> None:
    """FVI/FDI 계산 파이프라인 전체를 실행한다."""
    ensure_output_dirs()

    # --- Load & validate inputs ---
    df_2020 = load_year_csv(INPUT_2020, "2020")
    df_2022 = load_year_csv(INPUT_2022, "2022")
    validate_input_frame(df_2020, "2020")
    validate_input_frame(df_2022, "2022")

    # --- FVI ---
    fvi_2020 = compute_fvi(df_2020)
    fvi_2022 = compute_fvi(df_2022)

    assert np.isclose(fvi_2020["fvi"].sum(), 1.0, atol=VALIDATION_ATOL), (
        f"2020 FVI 합계가 1에 근접하지 않습니다: {fvi_2020['fvi'].sum()}"
    )
    assert np.isclose(fvi_2022["fvi"].sum(), 1.0, atol=VALIDATION_ATOL), (
        f"2022 FVI 합계가 1에 근접하지 않습니다: {fvi_2022['fvi'].sum()}"
    )

    # --- FDI (canonical: AHP-weighted; baseline: simple average, kept for comparison) ---
    fdi_2020 = compute_fdi_ahp_weighted(df_2020)
    fdi_2022 = compute_fdi_ahp_weighted(df_2022)
    fdi_baseline_2020 = compute_fdi_baseline_simple_average(df_2020)
    fdi_baseline_2022 = compute_fdi_baseline_simple_average(df_2022)

    # 하수관/재정자립도/소방인력 수가 연도별로 실제 값이 달라, FDI는 연도별로 유지한다
    # (검증: encoded_2020/2022 세 컬럼 전부 자치구별 값이 다름 - 정적 데이터가 아님)
    scores_df = (
        fvi_2020[[DISTRICT_COL, "fvi"]].rename(columns={"fvi": "fvi_2020"})
        .merge(fvi_2022[[DISTRICT_COL, "fvi"]].rename(columns={"fvi": "fvi_2022"}), on=DISTRICT_COL, how="outer")
        .merge(fdi_2020[[DISTRICT_COL, "fdi"]].rename(columns={"fdi": "fdi_2020"}), on=DISTRICT_COL, how="outer")
        .merge(fdi_2022[[DISTRICT_COL, "fdi"]].rename(columns={"fdi": "fdi_2022"}), on=DISTRICT_COL, how="outer")
        .rename(columns={DISTRICT_COL: "district"})
    )
    export_scores(scores_df, OUTPUT_SCORES_PATH)

    # --- FVI reference validation ---
    fvi_reference = load_reference_csv(FVI_REFERENCE_PATH)
    fvi_2020_passed = fvi_2022_passed = None
    if fvi_reference is not None:
        ref_2020 = fvi_reference[[DISTRICT_COL if DISTRICT_COL in fvi_reference.columns else "district",
                                   "fvi_2020_reference"]].rename(columns={
            (DISTRICT_COL if DISTRICT_COL in fvi_reference.columns else "district"): DISTRICT_COL
        })
        ref_2022 = fvi_reference[[DISTRICT_COL if DISTRICT_COL in fvi_reference.columns else "district",
                                   "fvi_2022_reference"]].rename(columns={
            (DISTRICT_COL if DISTRICT_COL in fvi_reference.columns else "district"): DISTRICT_COL
        })

        validation_2020 = validate_against_reference(
            fvi_2020, ref_2020, "fvi", "fvi_2020_reference", "FVI 2020"
        )
        validation_2022 = validate_against_reference(
            fvi_2022, ref_2022, "fvi", "fvi_2022_reference", "FVI 2022"
        )

        fvi_validation_df = validation_2020.rename(
            columns={"fvi": "fvi_2020", "abs_error": "fvi_2020_abs_error"}
        ).merge(
            validation_2022.rename(columns={"fvi": "fvi_2022", "abs_error": "fvi_2022_abs_error"})[
                [DISTRICT_COL, "fvi_2022", "fvi_2022_reference", "fvi_2022_abs_error", "validation_passed"]
            ],
            on=DISTRICT_COL, how="outer", suffixes=("_2020", "_2022"),
        ).rename(columns={DISTRICT_COL: "district"})

        fvi_validation_df["validation_passed"] = (
            fvi_validation_df["validation_passed_2020"] & fvi_validation_df["validation_passed_2022"]
        )
        fvi_validation_df = fvi_validation_df[
            ["district", "fvi_2020", "fvi_2020_reference", "fvi_2020_abs_error",
             "fvi_2022", "fvi_2022_reference", "fvi_2022_abs_error", "validation_passed"]
        ]
        export_validation_results(fvi_validation_df, OUTPUT_FVI_VALIDATION_PATH)

        fvi_2020_passed = bool(validation_2020["validation_passed"].all())
        fvi_2022_passed = bool(validation_2022["validation_passed"].all())
        fvi_2020_max_error = validation_2020["abs_error"].max()
        fvi_2022_max_error = validation_2022["abs_error"].max()
    else:
        fvi_2020_max_error = fvi_2022_max_error = float("nan")

    # --- FDI reference validation (optional - reference likely absent) ---
    fdi_reference = load_reference_csv(FDI_REFERENCE_PATH)
    fdi_status = "unverified"
    if fdi_reference is not None:
        fdi_combined = pd.concat([
            fdi_2020.assign(year=2020), fdi_2022.assign(year=2022),
        ], ignore_index=True)
        fdi_validation_df = validate_against_reference(
            fdi_combined, fdi_reference, "fdi", "fdi_reference", "FDI"
        )
        export_validation_results(fdi_validation_df, OUTPUT_FDI_VALIDATION_PATH)
        fdi_status = "verified" if bool(fdi_validation_df["validation_passed"].all()) else "verification_failed"
    else:
        logger.warning(
            "FDI 참조값이 없어 검증을 수행하지 않았습니다. "
            "정본 후보(AHP 가중 홍수대응인프라 점수)를 unverified 상태로 사용합니다."
        )

    # --- Summary log ---
    logger.info("FVI/FDI calculation completed.")
    logger.info("Districts processed: %d", len(scores_df))
    logger.info("2020 FVI max absolute error: %s", fvi_2020_max_error)
    logger.info("2022 FVI max absolute error: %s", fvi_2022_max_error)
    logger.info("2020 FVI validation passed: %s", fvi_2020_passed)
    logger.info("2022 FVI validation passed: %s", fvi_2022_passed)
    logger.info("FDI validation status: %s", fdi_status)
    logger.info("Output saved to: %s", OUTPUT_SCORES_PATH)


if __name__ == "__main__":
    main()
