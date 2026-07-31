from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
from sklearn.impute import KNNImputer

# --------------------------------------------------------------------------- #
# 2. Path configuration
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORT_DIR = PROJECT_ROOT / "reports"

SURVEY_PATH = RAW_DIR / "ahp_survey_final.csv"
OUTPUT_WEIGHTS_PATH = PROCESSED_DIR / "ahp_weights.csv"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ahp_weights")

# --------------------------------------------------------------------------- #
# 3. Constants
# --------------------------------------------------------------------------- #

EXPECTED_RESPONDENT_COUNT = 11
CR_THRESHOLD = 0.2  # 일관성 비율(CR) 임계값 - 이 이상이면 응답 제외

SCALE_MAP = {1: 1 / 5, 2: 1 / 4, 3: 1 / 3, 4: 1 / 2, 5: 1, 6: 2, 7: 3, 8: 4, 9: 5}
AHP_SCALE_VALUES = {"1/5": 1 / 5, "1/4": 1 / 4, "1/3": 1 / 3, "1/2": 1 / 2,
                     "1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
RI_TABLE = {1: 0, 2: 0, 3: 0.58, 4: 0.9, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}

GROUP_1_INDICES = [0, 1, 2]   # 과거 홍수 피해 규모 내부 비교
GROUP_2_INDICES = [3, 4, 5]   # 홍수에 취약한 지역 특성 내부 비교
GROUP_3_INDICES = [6, 7, 8]   # 홍수 대응 인프라 및 회복력 내부 비교
GROUP_TOTAL_INDICES = [9, 10, 11]  # 3개 그룹 간 상대 중요도 비교

GROUP_1_ITEMS = ["침수 가구 비율", "단위면적당 피해액", "피해 인구 비율"]
GROUP_2_ITEMS = ["인구 밀도", "불투수면적 비율", "가구 밀도"]
GROUP_3_ITEMS = ["하수관 길이", "재정자립도", "소방공무원 수"]
GROUP_NAMES = ["과거 홍수 피해 규모", "홍수에 취약한 지역 특성", "홍수 대응 인프라 및 회복력"]


# --------------------------------------------------------------------------- #
# 4. Input validation functions
# --------------------------------------------------------------------------- #

def load_survey(path: Path) -> pd.DataFrame:
    """AHP 설문 원자료를 로드한다.

    Raises:
        FileNotFoundError: 설문 파일이 없는 경우.
    """
    if not path.exists():
        raise FileNotFoundError(f"AHP 설문 원자료를 찾을 수 없습니다: {path}")
    df = pd.read_csv(path, encoding="utf-8")
    logger.info("Loaded AHP survey: %s (%d respondents)", path.name, len(df))
    return df


def validate_survey(df: pd.DataFrame) -> None:
    """응답자 수를 검증한다 (경고만, 강한 실패 아님 - 설문 특성상 유동적일 수 있음)."""
    if len(df) != EXPECTED_RESPONDENT_COUNT:
        logger.warning(
            "예상 응답자 수(%d)와 실제 응답자 수(%d)가 다릅니다",
            EXPECTED_RESPONDENT_COUNT, len(df),
        )
    if np.isinf(df.select_dtypes(include=[float, int]).to_numpy(dtype=float, na_value=0)).any():
        raise ValueError("설문 데이터에 inf 값이 포함되어 있습니다")


# --------------------------------------------------------------------------- #
# 5. Normalization / cleaning functions
# --------------------------------------------------------------------------- #

def extract_unique_question_columns(df_raw: pd.DataFrame) -> list[str]:
    """숫자+마침표로 시작하는 질문 컬럼만 추출하고, 동일 질문의 중복 컬럼을 제거한다."""
    question_columns = [col for col in df_raw.columns if re.match(r"^\d+\.", col.strip())]
    seen: Dict[str, str] = {}
    for col in question_columns:
        key = col.strip().split("?")[0].strip()
        if key not in seen:
            seen[key] = col
    return list(seen.values())


def convert_ahp_scale(value: object) -> float:
    """설문 응답값(1~9 척도 또는 분수 문자열)을 AHP 비율 척도로 변환한다."""
    if pd.isna(value):
        return np.nan
    text = str(value).strip()
    try:
        return SCALE_MAP.get(int(text), np.nan)
    except ValueError:
        try:
            if "/" in text:
                num, denom = text.split("/")
                return float(num) / float(denom)
            return float(text)
        except ValueError:
            return np.nan


def round_to_ahp_scale(value: float) -> str:
    """보간된 실수값을 가장 가까운 AHP 9단계 척도 문자열로 반올림한다."""
    if pd.isna(value):
        return np.nan
    closest = min(AHP_SCALE_VALUES.items(), key=lambda item: abs(item[1] - value))
    return closest[0]


def build_cleaned_responses(df_raw: pd.DataFrame) -> pd.DataFrame:
    """질문 컬럼 추출 -> 척도 변환 -> KNN 결측치 보간 -> AHP 스케일 재수렴까지 수행한다."""
    unique_columns = extract_unique_question_columns(df_raw)
    df_questions = df_raw[unique_columns].copy()
    df_converted = df_questions.applymap(convert_ahp_scale)

    imputer = KNNImputer(n_neighbors=3)
    df_filled = pd.DataFrame(
        imputer.fit_transform(df_converted), columns=df_converted.columns, index=df_converted.index
    )
    df_rounded = df_filled.applymap(round_to_ahp_scale)
    df_numeric = df_rounded.applymap(lambda x: AHP_SCALE_VALUES.get(x, np.nan))
    return df_numeric


# --------------------------------------------------------------------------- #
# 6. AHP eigenvector / consistency functions
# --------------------------------------------------------------------------- #

def build_pairwise_matrix(comparisons: pd.Series, n_items: int) -> np.ndarray:
    """상위삼각 비교값으로부터 대칭 쌍대비교행렬을 구성한다."""
    matrix = np.ones((n_items, n_items))
    idx = 0
    comparisons = list(comparisons)
    for i in range(n_items):
        for j in range(i + 1, n_items):
            value = comparisons[idx]
            matrix[i, j] = value
            matrix[j, i] = 1 / value
            idx += 1
    return matrix


def ahp_weights_from_pairwise_matrix(matrix: np.ndarray) -> Tuple[np.ndarray, float, float]:
    """쌍대비교행렬의 주고유벡터로부터 가중치, CI, CR을 계산한다."""
    eigvals, eigvecs = np.linalg.eig(matrix)
    max_index = np.argmax(eigvals.real)
    max_eigval = eigvals[max_index].real
    weights = eigvecs[:, max_index].real
    weights = weights / weights.sum()

    n = matrix.shape[0]
    CI = (max_eigval - n) / (n - 1)
    RI = RI_TABLE[n]
    CR = CI / RI if RI != 0 else 0.0
    return weights, CI, CR


def compute_group_weights(df_numeric: pd.DataFrame) -> Dict[str, Tuple[np.ndarray, float, float]]:
    """그룹별(1,2,3,전체) 평균 응답 기반 가중치·CI·CR을 계산한다."""
    avg_values = df_numeric.mean()
    results = {}
    for label, indices in [
        ("group_1", GROUP_1_INDICES), ("group_2", GROUP_2_INDICES),
        ("group_3", GROUP_3_INDICES), ("group_total", GROUP_TOTAL_INDICES),
    ]:
        matrix = build_pairwise_matrix(avg_values.iloc[indices], 3)
        results[label] = ahp_weights_from_pairwise_matrix(matrix)
    return results


def compute_per_respondent_cr(df_numeric: pd.DataFrame) -> pd.DataFrame:
    """응답자별 그룹 CR과 전체 그룹간 CR을 계산한다."""
    records = []
    for idx, row in df_numeric.iterrows():
        try:
            g1 = build_pairwise_matrix(row.iloc[GROUP_1_INDICES], 3)
            g2 = build_pairwise_matrix(row.iloc[GROUP_2_INDICES], 3)
            g3 = build_pairwise_matrix(row.iloc[GROUP_3_INDICES], 3)
            g_total = build_pairwise_matrix(row.iloc[GROUP_TOTAL_INDICES], 3)
            _, _, cr1 = ahp_weights_from_pairwise_matrix(g1)
            _, _, cr2 = ahp_weights_from_pairwise_matrix(g2)
            _, _, cr3 = ahp_weights_from_pairwise_matrix(g3)
            _, _, cr_total = ahp_weights_from_pairwise_matrix(g_total)
            records.append({"respondent": idx, "cr_group1": cr1, "cr_group2": cr2,
                             "cr_group3": cr3, "cr_total": cr_total})
        except Exception:  # noqa: BLE001 - 개별 응답자 실패는 전체 파이프라인을 막지 않음
            records.append({"respondent": idx, "cr_group1": np.nan, "cr_group2": np.nan,
                             "cr_group3": np.nan, "cr_total": np.nan})
    return pd.DataFrame(records)


def compute_valid_only_weights(
    df_numeric: pd.DataFrame, cr_per_respondent: pd.DataFrame, threshold: float = CR_THRESHOLD
) -> Dict[str, Tuple[np.ndarray, float, float]]:
    """각 그룹(1,2,3,전체)마다 해당 그룹 CR이 threshold 미만인 응답자만 사용해 가중치를 재계산한다.

    원본 노트북은 이 단계의 결과 변수(`w1_valid` 등)를 사용하지만, 그 값을 만드는
    코드 자체는 저장된 노트북에 남아있지 않다 (`valid_ids`가 정의 없이 참조됨).
    이 함수는 CR 기준 필터링이라는 서술된 방법론을 재구성한 것이며, 그룹마다
    자신의 CR 컬럼으로 개별 필터링한다 (그룹 전체용 하나의 필터를 모든 행렬에
    공용으로 적용하는 방식보다 다운스트림 그룹 가중치 상수와 더 가깝게 재현됨 -
    `reports/01_ahp_validation_report.md` 참고).
    """
    group_cr_columns = {
        "group_1": "cr_group1", "group_2": "cr_group2",
        "group_3": "cr_group3", "group_total": "cr_total",
    }
    group_indices = {
        "group_1": GROUP_1_INDICES, "group_2": GROUP_2_INDICES,
        "group_3": GROUP_3_INDICES, "group_total": GROUP_TOTAL_INDICES,
    }

    results = {}
    for label, cr_col in group_cr_columns.items():
        valid_respondents = cr_per_respondent.loc[cr_per_respondent[cr_col] < threshold, "respondent"]
        df_valid = df_numeric.loc[valid_respondents]
        logger.info(
            "[%s] CR(%s) < %.2f 기준 유효 응답자: %d / %d",
            label, cr_col, threshold, len(df_valid), len(df_numeric),
        )
        matrix = build_pairwise_matrix(df_valid.mean().iloc[group_indices[label]], 3)
        results[label] = ahp_weights_from_pairwise_matrix(matrix)
    return results


# --------------------------------------------------------------------------- #
# 7. Final weight combination
# --------------------------------------------------------------------------- #

def combine_final_weights(group_results: Dict[str, Tuple[np.ndarray, float, float]]) -> Dict[str, float]:
    """그룹 내부 가중치 × 그룹 간 중요도를 곱해 9개 세부 지표의 최종 AHP 가중치를 만든다."""
    w1, _, _ = group_results["group_1"]
    w2, _, _ = group_results["group_2"]
    w3, _, _ = group_results["group_3"]
    w_total, _, _ = group_results["group_total"]

    final_weights: Dict[str, float] = {}
    for name, weight in zip(GROUP_1_ITEMS, w1):
        final_weights[name] = float(weight * w_total[0])
    for name, weight in zip(GROUP_2_ITEMS, w2):
        final_weights[name] = float(weight * w_total[1])
    for name, weight in zip(GROUP_3_ITEMS, w3):
        final_weights[name] = float(weight * w_total[2])
    return final_weights


# --------------------------------------------------------------------------- #
# 8. Reference / comparison functions
# --------------------------------------------------------------------------- #

# 03_calculate_fvi_fdi.py / 02_calculate_baseline_pcd.py에 하드코딩되어 실제로
# 사용되고 있는 가중치 상수 (원본 "새로운 매트릭스 제작기 (최최종).ipynb"의 값).
DOWNSTREAM_HARDCODED_WEIGHTS = {
    "침수 가구 비율": 0.2595, "단위면적당 피해액": 0.2431, "피해 인구 비율": 0.4974,
    "인구 밀도": 0.2250, "불투수면적 비율": 0.4157, "가구 밀도": 0.3592,
    "하수관 길이": 0.3824, "재정자립도": 0.3390, "소방공무원 수": 0.2786,
}
DOWNSTREAM_GROUP_WEIGHTS = {"과거 홍수 피해 규모": 0.2321, "홍수에 취약한 지역 특성": 0.4274,
                            "홍수 대응 인프라 및 회복력": 0.3405}


def compare_with_downstream_constants(
    group_results: Dict[str, Tuple[np.ndarray, float, float]]
) -> pd.DataFrame:
    """이 스크립트로 재계산한 '그룹 내부' 가중치를 다운스트림 하드코딩 상수와 비교한다.

    `03_calculate_fvi_fdi.py`의 GROUP1/2/3_WEIGHTS, GROUP_WEIGHTS 상수는 그룹 내부
    가중치(w1/w2/w3, 각 그룹 합=1)와 그룹간 가중치(w_total, 합=1)를 '그대로' 사용하며,
    이 둘을 곱한 합성값(9개 지표 최종 AHP 가중치, `combine_final_weights`의 결과)은
    FVI 계산에는 쓰이지 않는다. 따라서 다운스트림 상수와 비교할 때는 반드시
    그룹 내부/그룹간 raw 가중치를 사용해야 한다.
    """
    rows = []
    for group_key, items in [("group_1", GROUP_1_ITEMS), ("group_2", GROUP_2_ITEMS), ("group_3", GROUP_3_ITEMS)]:
        weights = group_results[group_key][0]
        for name, recomputed in zip(items, weights):
            reference = DOWNSTREAM_HARDCODED_WEIGHTS.get(name, np.nan)
            rows.append({
                "indicator": name, "recomputed_weight": float(recomputed),
                "hardcoded_reference_weight": reference,
                "abs_diff": abs(float(recomputed) - reference) if not np.isnan(reference) else np.nan,
            })
    w_total = group_results["group_total"][0]
    for name, recomputed in zip(GROUP_NAMES, w_total):
        reference = DOWNSTREAM_GROUP_WEIGHTS.get(name, np.nan)
        rows.append({
            "indicator": f"[group] {name}", "recomputed_weight": float(recomputed),
            "hardcoded_reference_weight": reference,
            "abs_diff": abs(float(recomputed) - reference) if not np.isnan(reference) else np.nan,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 9. Output export functions
# --------------------------------------------------------------------------- #

def ensure_output_dirs() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def export_weights(final_weights: Dict[str, float], group_results: Dict[str, Tuple[np.ndarray, float, float]],
                    path: Path) -> None:
    """그룹 내부 가중치(level=group_internal), 그룹간 가중치(level=group_total),
    9개 지표 합성 최종 가중치(level=combined_final, 참고용 - FVI 계산에는 미사용)를 모두 저장한다."""
    rows = []
    for group_key, items in [("group_1", GROUP_1_ITEMS), ("group_2", GROUP_2_ITEMS), ("group_3", GROUP_3_ITEMS)]:
        for name, weight in zip(items, group_results[group_key][0]):
            rows.append({"indicator": name, "weight": float(weight), "level": "group_internal", "group": group_key})

    w_total = group_results["group_total"][0]
    for name, weight in zip(GROUP_NAMES, w_total):
        rows.append({"indicator": name, "weight": float(weight), "level": "group_total", "group": "group_total"})

    for name, weight in final_weights.items():
        rows.append({"indicator": name, "weight": weight, "level": "combined_final", "group": ""})

    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Saved AHP weights: %s", path)


# --------------------------------------------------------------------------- #
# 10. main()
# --------------------------------------------------------------------------- #

def main() -> None:
    """AHP 가중치 계산 파이프라인 전체를 실행한다."""
    ensure_output_dirs()

    df_raw = load_survey(SURVEY_PATH)
    validate_survey(df_raw)

    df_numeric = build_cleaned_responses(df_raw)

    full_sample_results = compute_group_weights(df_numeric)
    cr_per_respondent = compute_per_respondent_cr(df_numeric)
    valid_only_results = compute_valid_only_weights(df_numeric, cr_per_respondent)

    final_weights = combine_final_weights(valid_only_results)
    export_weights(final_weights, valid_only_results, OUTPUT_WEIGHTS_PATH)

    comparison = compare_with_downstream_constants(valid_only_results)
    comparison_path = PROCESSED_DIR / "ahp_weights_vs_downstream_constants.csv"
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")

    max_diff = comparison["abs_diff"].max()
    logger.info("AHP weight calculation completed.")
    logger.info("Respondents processed: %d (valid after CR filter: see log above)", len(df_numeric))
    logger.info("Max abs diff vs downstream hardcoded weights: %.4f", max_diff)
    logger.info("Output saved to: %s", OUTPUT_WEIGHTS_PATH)
    logger.info("Comparison saved to: %s", comparison_path)


if __name__ == "__main__":
    main()
