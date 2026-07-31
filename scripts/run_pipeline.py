from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("run_pipeline")

SCRIPTS_DIR = Path(__file__).resolve().parent

PIPELINE_STAGES = [
    "01_calculate_ahp_weights.py",
    "02_calculate_baseline_pcd.py",
    "03_calculate_fvi_fdi.py",
    "04_classify_temporal_risk.py",
    "05_calculate_final_risk_scores.py",
]


def _load_module(script_name: str) -> types.ModuleType:
    """숫자로 시작하는 파일명은 일반 import가 안 되므로 파일 경로로 직접 로드한다."""
    path = SCRIPTS_DIR / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> None:
    """01~05단계를 순서대로 실행한다."""
    for stage in PIPELINE_STAGES:
        logger.info("=" * 70)
        logger.info("Running stage: %s", stage)
        logger.info("=" * 70)
        module = _load_module(stage)
        module.main()

    logger.info("=" * 70)
    logger.info("Pipeline completed: all 5 stages ran successfully.")
    logger.info("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
