from __future__ import annotations

import aiosqlite

from ._connection import _Store


class GuidanceRepositoryImpl(_Store):
    async def get_guidance(
        self,
        channel_id: int,
    ) -> tuple[str | None, str | None]:
        rows = await self._fetch(
            """
            SELECT scope, content
            FROM signal_guidance
            WHERE
                scope = 'global'
                OR (
                    scope = 'channel'
                    AND channel_id = ?
                )
            """,
            (channel_id,),
            many=True,
        )

        global_guidance = None
        channel_guidance = None

        for scope, content in rows:
            if scope == "global":
                global_guidance = content
            else:
                channel_guidance = content

        return global_guidance, channel_guidance

    async def set_guidance(
        self,
        content: str,
        *,
        channel_id: int | None = None,
    ) -> None:
        content = content.strip()

        if not content:
            raise ValueError("Guidance must not be empty")

        scope = "global" if channel_id is None else "channel"

        async with aiosqlite.connect(self._database_path) as db:
            if scope == "global":
                await db.execute(
                    """
                    DELETE FROM signal_guidance
                    WHERE scope = 'global'
                    """
                )
            else:
                await db.execute(
                    """
                    DELETE FROM signal_guidance
                    WHERE
                        scope = 'channel'
                        AND channel_id = ?
                    """,
                    (channel_id,),
                )

            await db.execute(
                """
                INSERT INTO signal_guidance(
                    scope,
                    channel_id,
                    content,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    CURRENT_TIMESTAMP
                )
                """,
                (
                    scope,
                    channel_id,
                    content,
                ),
            )

            await db.commit()
