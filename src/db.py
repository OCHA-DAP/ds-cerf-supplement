import logging
import os
from functools import lru_cache

import pandas as pd
import ocha_stratus as stratus

os.environ.setdefault("PGSSLMODE", "require")
logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def load_storms() -> pd.DataFrame:
    """Returns DataFrame with sid, name, season from storms.ibtracs_storms."""
    try:
        engine = stratus.get_engine()
        with engine.connect() as conn:
            return pd.read_sql(
                "SELECT sid, name, season FROM storms.ibtracs_storms "
                "WHERE name IS NOT NULL ORDER BY season DESC, name",
                conn,
            )
    except Exception as e:
        logger.warning(f"Could not load storms from DB: {e}")
        return pd.DataFrame(columns=["sid", "name", "season"])
