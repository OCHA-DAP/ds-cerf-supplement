import os

import pandas as pd
import streamlit as st
import ocha_stratus as stratus

os.environ.setdefault("PGSSLMODE", "require")


@st.cache_data(ttl=86400, show_spinner=False)
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
        st.warning(f"Could not load storms from DB: {e}")
        return pd.DataFrame(columns=["sid", "name", "season"])
