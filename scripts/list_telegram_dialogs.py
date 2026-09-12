import asyncio
import os

from telethon import TelegramClient


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_name = os.environ.get(
        "TELEGRAM_SESSION_NAME",
        "/state/cautious_crypto_bro",
    )

    client = TelegramClient(
        session_name,
        api_id,
        api_hash,
    )

    await client.start()

    print()
    print(
        f"{'ID':>18}  "
        f"{'TYPE':<16}  "
        f"{'USERNAME':<30}  "
        f"NAME"
    )
    print("-" * 100)

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        username = getattr(entity, "username", None)

        print(
            f"{dialog.id:>18}  "
            f"{type(entity).__name__:<16}  "
            f"{('@' + username) if username else '-':<30}  "
            f"{dialog.name}"
        )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
