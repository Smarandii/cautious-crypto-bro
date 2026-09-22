import asyncio
import sqlite3

from cautious_crypto_bro.storage import (
    MIGRATION_4_TO_5_SQL,
    MIGRATION_5_TO_6_SQL,
    IntentStore,
)


def create_legacy_policy(
    db: sqlite3.Connection,
) -> None:
    db.executescript(
        """
        CREATE TABLE execution_policy (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            trading_capital_usdt TEXT NOT NULL,
            risk_per_trade_pct TEXT NOT NULL,
            range_order_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO execution_policy(
            id,
            trading_capital_usdt,
            risk_per_trade_pct,
            range_order_count
        )
        VALUES (
            1,
            '6800',
            '2',
            5
        );

        CREATE TABLE execution_exit_policy (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            minimum_reward_bps TEXT NOT NULL,
            basic_r_multiple TEXT NOT NULL,
            basic_close_pct TEXT NOT NULL,
            medium_r_multiple TEXT NOT NULL,
            medium_close_pct TEXT NOT NULL,
            high_r_multiple TEXT NOT NULL,
            high_close_pct TEXT NOT NULL,
            updated_at TEXT NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO execution_exit_policy(
            id,
            minimum_reward_bps,
            basic_r_multiple,
            basic_close_pct,
            medium_r_multiple,
            medium_close_pct,
            high_r_multiple,
            high_close_pct
        )
        VALUES (
            1,
            '20',
            '0.5',
            '25',
            '1',
            '35',
            '2',
            '40'
        );
        """
    )


def assert_v7(
    database,
) -> None:
    with sqlite3.connect(database) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]

        policy_columns = {
            row[1]
            for row in db.execute(
                """
                PRAGMA table_info(
                    execution_policy
                )
                """
            )
        }

        exit_table = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE
                type = 'table'
                AND name =
                    'execution_exit_policy'
            """
        ).fetchone()

        risk = db.execute(
            """
            SELECT risk_per_trade_pct
            FROM execution_policy
            WHERE id = 1
            """
        ).fetchone()

        strategy_columns = {
            row[1]
            for row in db.execute(
                """
                PRAGMA table_info(
                    position_strategies
                )
                """
            )
        }

    assert version == 7

    assert policy_columns == {
        "id",
        "risk_per_trade_pct",
        "updated_at",
    }

    assert exit_table is None
    assert risk == ("2",)

    assert "protected_stop_loss" in strategy_columns
    assert "trailing_distance" in strategy_columns


def migrate_from(
    tmp_path,
    version: int,
) -> None:
    database = tmp_path / f"legacy-v{version}.sqlite3"

    with sqlite3.connect(database) as db:
        create_legacy_policy(db)

        if version >= 5:
            db.executescript(MIGRATION_4_TO_5_SQL)

        if version >= 6:
            db.executescript(MIGRATION_5_TO_6_SQL)

        db.execute(f"PRAGMA user_version = {version}")

    asyncio.run(IntentStore(database).initialize())

    assert_v7(database)


def test_schema_migrates_v4_to_v7(
    tmp_path,
) -> None:
    migrate_from(
        tmp_path,
        4,
    )


def test_schema_migrates_v5_to_v7(
    tmp_path,
) -> None:
    migrate_from(
        tmp_path,
        5,
    )


def test_schema_migrates_v6_to_v7(
    tmp_path,
) -> None:
    migrate_from(
        tmp_path,
        6,
    )
